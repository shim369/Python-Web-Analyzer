import logging
import posixpath
import re
import time
from collections import deque
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)


class WebCrawler:
    """ウェブサイトを巡回し、構成、CMS、問い合わせ項目、階層、用途などを解析するクローラー。

    v2: iframe内フォーム解析、JS遷移検知、label紐付け強化、div/dlフォーム対応、
    優先度キューのdeque化、logging化などを反映。
    """

    # 必須マーク・記号として除去する表記のバリエーション
    REQUIRED_MARK_PATTERNS = [
        r"【必須】",
        r"（必須）",
        r"\(必須\)",
        r"\*必須",
        r"※必須",
        r"\(\s*[Rr]equired\s*\)",
        r"（\s*[Rr]equired\s*）",
        r"必須",
        r"[Rr]equired",
        r"[Rr]equire",
        r"※",
        r"＊",
        r"\*",
        r"■",
        r"●",
        r"◆",
    ]
    _REQUIRED_MARK_RE = re.compile("|".join(REQUIRED_MARK_PATTERNS))
    _EMPTY_PARENS_RE = re.compile(r"[\(（]\s*[\)）]")

    # お問い合わせページ判定用キーワード(日英混在)
    CONTACT_KEYWORDS = [
        "contact",
        "inquiry",
        "otoiawase",
        "entry",
        "support",
        "form",
        "mail",
        "help",
        "お問い合わせ",
        "お問合せ",
        "資料請求",
        "相談",
        "応募",
        "採用",
        "エントリー",
        "メール",
        "フォーム",
    ]

    # JSフレームワーク検出用マーカー(CSR/SPA判定に利用)
    JS_FRAMEWORK_MARKERS = [
        "__nuxt",
        "nuxt.config",
        "vue.js",
        "data-v-",
        "react",
        "_reactlistening",
        "__next",
        "next/static",
        "ng-version",
        "angular",
        "svelte-",
        "alpinejs",
        "x-data=",
        "vite/client",
        "remix",
        "astro-island",
    ]

    # フォームらしきコンテナ(form要素が無い場合の代替検出用)
    _FORM_LIKE_CLASS_RE = re.compile(r"form|contact|inquiry|entry", re.I)
    _LABEL_LIKE_CLASS_RE = re.compile(r"label|title|item-?label|form-?label|field-?name", re.I)

    # location遷移をJSで行うパターン(onclick / インラインscript両対応)
    _JS_LOCATION_RE = re.compile(r"(?:location\.href|window\.location(?:\.href)?)\s*=\s*['\"]([^'\"]+)['\"]")

    def __init__(
        self,
        timeout: float = 30.0,
        page_timeout: float = 5.0,
        verify_ssl: bool = False,
        max_iframe_depth: int = 2,
        render_js: bool = False,
    ) -> None:
        """
        Args:
            timeout: クロール全体にかけられる時間予算(秒)。
            page_timeout: 個々のHTTPリクエストのタイムアウト(秒)。
            verify_ssl: SSL証明書検証を行うか。社内システムなどオレオレ証明書が
                多い環境ではFalseのままにしておくと巡回漏れが減るが、
                中間者攻撃のリスクとのトレードオフになるため呼び出し側で明示する。
            max_iframe_depth: iframe内フォームを再帰的に追いかける最大深度。
            render_js: TrueならPlaywrightでレンダリング後のHTMLを取得する
                (未インストール時は自動的にhttpx取得へフォールバックする)。
        """
        self.timeout = timeout
        self.page_timeout = page_timeout
        self.verify_ssl = verify_ssl
        self.max_iframe_depth = max_iframe_depth
        self.render_js = render_js

        self.headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
            "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"),
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        }

    # ------------------------------------------------------------------
    # 基本ユーティリティ
    # ------------------------------------------------------------------

    def _get_clean_domain(self, url: str) -> str:
        parsed = urlparse(url)
        netloc = parsed.netloc or parsed.path
        domain = netloc.split(":")[0]
        return domain.replace("www.", "")

    def _is_valid_internal_link(self, current_url: str, href: str, base_domain: str) -> bool:
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            return False
        abs_url = urljoin(current_url, href)
        parsed_abs = urlparse(abs_url)
        abs_domain = parsed_abs.netloc.replace("www.", "").split(":")[0]

        if any(
            parsed_abs.path.lower().endswith(ext)
            for ext in [
                ".mp4",
                ".avi",
                ".mov",
                ".wmv",
                ".flv",
                ".webm",  # 動画
                ".jpg",
                ".jpeg",
                ".png",
                ".gif",
                ".webp",
                ".svg",
                ".ico",  # 画像
                ".pdf",
                ".zip",
                ".tar",
                ".gz",
                ".rar",
                ".7z",  # 圧縮・ドキュメント
                ".mp3",
                ".wav",  # 音声
            ]
        ):
            return False

        return abs_domain == base_domain

    def _detect_cms(self, html: str) -> str:
        html_lower = html.lower()
        if "wp-content" in html_lower or "wp-includes" in html_lower:
            return "WordPress"
        if "basercms" in html_lower or "bc-" in html_lower:
            return "baserCMS"
        return ""

    def _detect_js_framework(self, html: str) -> str:
        """SPA/CSRフレームワークの利用有無を検知する(フォーム取得漏れの原因切り分け用)。"""
        html_lower = html.lower()
        for marker in self.JS_FRAMEWORK_MARKERS:
            if marker in html_lower:
                return marker
        return ""

    def _extract_purpose_and_features(self, html: str) -> str:
        """HTMLから優先順位(description > title > h1)に従って文字列をそのまま抽出する"""
        if not html:
            return ""

        soup = BeautifulSoup(html, "html.parser")

        desc_tag = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
        if desc_tag and isinstance(desc_tag, Tag):
            content_attr = desc_tag.get("content", "")
            desc_text = ("".join(content_attr) if isinstance(content_attr, list) else str(content_attr)).strip()
            if desc_text:
                return desc_text

        if soup.title and soup.title.string:
            title_text = soup.title.string.strip()
            if title_text:
                return title_text

        h1_tag = soup.find("h1")
        if h1_tag and isinstance(h1_tag, Tag):
            h1_text = h1_tag.get_text(strip=True)
            if h1_text:
                return h1_text

        return ""

    def _clean_menu_text(self, text: str) -> str:
        """交通アクセスAccess のような英日混在から不要な英語やスペースを綺麗にする"""
        text = re.sub(r"\s+", "", text)
        match = re.match(r"^([ぁ-んァ-ヶー一-龠々]+)[A-Za-z]+$", text)
        if match:
            return match.group(1)
        return text

    def _has_repeating_path_pattern(self, path: str) -> bool:
        """カレンダーページ等が生む無限リンクトラップを検知する。

        `/a/a/a` のような単純な繰り返しに加え、`/a/b/a/b/a/b` のような
        2セグメント単位の交互パターンも検知する。
        """
        if re.search(r"([^/]+)/\1/\1", path):
            return True

        segments = [p for p in path.split("/") if p]
        for chunk_size in (2, 3):
            if len(segments) < chunk_size * 3:
                continue
            for i in range(len(segments) - chunk_size * 3 + 1):
                a = segments[i : i + chunk_size]
                b = segments[i + chunk_size : i + chunk_size * 2]
                c = segments[i + chunk_size * 2 : i + chunk_size * 3]
                if a == b == c:
                    return True
        return False

    # ------------------------------------------------------------------
    # HTML取得・デコード
    # ------------------------------------------------------------------

    def _decode_response(self, response: httpx.Response) -> str:
        """レスポンスの文字コードを判定してデコードする(meta charset優先、Shift_JIS系はcp932に正規化)。"""
        raw_content_head = response.content[:2048].decode("ascii", errors="ignore")
        meta_charset = re.search(r'charset=["\']?([a-zA-Z0-9_-]+)', raw_content_head, re.IGNORECASE)

        if meta_charset:
            encoding = meta_charset.group(1)
        else:
            encoding = response.charset_encoding if response.charset_encoding else "utf-8"

        if encoding.lower() in ["shift_jis", "shift-jis", "sjis"]:
            encoding = "cp932"

        try:
            return response.content.decode(encoding, errors="replace")
        except Exception:
            return response.content.decode("utf-8", errors="replace")

    def _fetch_rendered_html(self, url: str) -> str:
        """Playwrightが利用可能ならレンダリング後のHTMLを取得する。未導入時は空文字を返す。"""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.warning("Playwright未インストールのためJSレンダリングをスキップします: %s", url)
            return ""

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(user_agent=self.headers["User-Agent"])
                page.goto(url, timeout=self.page_timeout * 1000)
                html = page.content()
                browser.close()
                return html
        except Exception as e:
            logger.warning("Playwrightによる取得に失敗しました(%s): %s", url, e)
            return ""

    # ------------------------------------------------------------------
    # JS遷移リンクの検知
    # ------------------------------------------------------------------

    def _extract_js_links(self, html: str) -> list[str]:
        """onclick="location.href='...'" やインラインscript内のwindow.location代入からリンクを拾う。"""
        return self._JS_LOCATION_RE.findall(html)

    # ------------------------------------------------------------------
    # フォーム項目抽出
    # ------------------------------------------------------------------

    def _remove_required_marks(self, text: str) -> str:
        text = self._REQUIRED_MARK_RE.sub("", text)
        text = self._EMPTY_PARENS_RE.sub("", text)
        return re.sub(r"^[\s\xa0\n\r]+|[\s\xa0\n\r]+$", "", text)

    def _get_label_for_input(self, inp: Tag, form: Tag, soup: BeautifulSoup) -> str:
        """input要素に対応するラベル文字列を、複数の手がかりから解決する。

        優先順位:
        1. aria-labelledby が指す要素のテキスト
        2. aria-label 属性そのもの
        3. <label for="id"> の紐付け
        4. 直近の祖先(tr/dt/div等)から拾えるラベルらしきテキスト
        """
        labelledby = inp.get("aria-labelledby")
        if labelledby:
            target = soup.find(id=str(labelledby))
            if target and isinstance(target, Tag):
                txt = target.get_text(strip=True)
                if txt:
                    return txt

        aria_label = inp.get("aria-label")
        if aria_label:
            txt = str(aria_label).strip()
            if txt:
                return txt

        input_id = inp.get("id")
        if input_id:
            label_tag = form.find("label", attrs={"for": str(input_id)})
            if label_tag and isinstance(label_tag, Tag):
                txt = label_tag.get_text(strip=True)
                if txt:
                    return txt

        # 祖先を辿ってラベルらしきテキストを探す(dt/th/label-likeクラスのdiv/span等)
        for ancestor in inp.parents:
            if not isinstance(ancestor, Tag) or ancestor is form:
                break

            # dt/dd, th/td のように「ラベルが直前の兄弟要素」になっているケース
            # (dtはddの祖先ではなく兄弟なので、ancestor自身の直前の兄弟も確認する)
            prev_sibling = ancestor.find_previous_sibling(["dt", "th"])
            if prev_sibling and isinstance(prev_sibling, Tag):
                txt = prev_sibling.get_text(strip=True)
                if txt:
                    return txt

            sibling_label = ancestor.find(
                ["label", "th", "dt", "legend", "span", "strong", "p"],
                class_=self._LABEL_LIKE_CLASS_RE,
            )
            if sibling_label and isinstance(sibling_label, Tag):
                txt = sibling_label.get_text(strip=True)
                if txt:
                    return txt
            # class指定が無いケース: dt/th/legendであればそのままテキストを使う
            plain_label = ancestor.find(["th", "dt", "legend"])
            if plain_label and isinstance(plain_label, Tag):
                txt = plain_label.get_text(strip=True)
                if txt:
                    return txt

        return ""

    def _find_form_containers(self, soup: BeautifulSoup) -> list[Tag]:
        """<form>タグに加え、role="form"やdata-form、divベースの疑似フォームも拾う。"""
        containers: list[Tag] = list(soup.find_all("form"))
        containers.extend(soup.find_all(attrs={"role": "form"}))
        containers.extend(soup.find_all(attrs={"data-form": True}))

        if not containers:
            # <form>タグが存在しない場合のみ、divベースの疑似フォームを探索する
            for candidate in soup.find_all("div", class_=self._FORM_LIKE_CLASS_RE):
                if candidate.find(["input", "textarea", "select"]):
                    containers.append(candidate)

        # 重複除去(同一Tagが複数条件にヒットする場合がある)
        seen_ids = set()
        unique_containers = []
        for c in containers:
            if id(c) not in seen_ids:
                seen_ids.add(id(c))
                unique_containers.append(c)
        return unique_containers

    def _extract_form_fields(
        self,
        html: str,
        base_url: str,
        client: httpx.Client | None = None,
        depth: int = 0,
    ) -> tuple[str, bool]:
        """フォーム内の入力項目ラベルを抽出する。iframe内フォームは再帰的に解析する。"""

        fields: list[str] = []
        has_attachment = False
        html_lower = html.lower()
        if "hbspt.forms.create" in html_lower or "hsforms.net" in html_lower:
            return "外部埋め込みフォーム検出(HubSpot)", False

        if "tayori.com" in html_lower:
            return "外部埋め込みフォーム検出(Tayori)", False

        soup = BeautifulSoup(html, "html.parser")
        containers = self._find_form_containers(soup)

        for form in containers:
            form_id = str(form.get("id", "")).lower()
            class_attr = form.get("class")
            form_class = ("".join([str(c) for c in class_attr]) if isinstance(class_attr, list) else str(class_attr or "")).lower()
            form_action = str(form.get("action", "")).lower()

            if "search" in form_id or "search" in form_class or "search" in form_action:
                continue

            inputs = form.find_all(["input", "textarea", "select"])
            valid_inputs = []
            for inp in inputs:
                if not isinstance(inp, Tag):
                    continue
                type_attr = inp.get("type", "")
                itype = ("".join(type_attr) if isinstance(type_attr, list) else str(type_attr)).lower()
                if itype in ["hidden", "submit", "button", "image", "reset"]:
                    continue
                valid_inputs.append(inp)

            if not valid_inputs:
                continue

            # 1. th/label/dt/td/legend/span/strong/p + for=/aria-labelledby の紐付けから取得
            for inp in valid_inputs:
                if inp.name == "input" and str(inp.get("type", "")).lower() == "file":
                    has_attachment = True

                txt = self._get_label_for_input(inp, form, soup)

            # 2. 上記で拾いきれなかった場合、th/label/dt/tdの総当たりでバックアップ
            if not fields:
                labels = form.find_all(["th", "label", "dt", "td", "legend"])
                for lbl in labels:
                    if not isinstance(lbl, Tag):
                        continue
                    txt = self._remove_required_marks(lbl.get_text(strip=True))
                    if txt and len(txt) < 25 and txt not in fields:
                        fields.append(txt)

            # 3. それでも拾えない場合、placeholder/aria-label/title/nameを候補として利用
            if not fields:
                for inp in valid_inputs:
                    for attr in ("placeholder", "aria-label", "title", "name"):
                        val = inp.get(attr)
                        if not val:
                            continue
                        txt = self._remove_required_marks(str(val).strip())
                        if txt and len(txt) < 25 and txt not in fields:
                            fields.append(txt)
                        break

        # iframe内フォームの再帰解析
        if client is not None and depth < self.max_iframe_depth:
            for iframe in soup.find_all("iframe"):
                src = iframe.get("src")
                if not src:
                    continue
                abs_src = urljoin(base_url, str(src))
                if not abs_src.startswith(("http://", "https://")):
                    continue
                try:
                    resp = client.get(abs_src, timeout=self.page_timeout)
                    resp.raise_for_status()
                except Exception:
                    continue
                iframe_html = self._decode_response(resp)
                iframe_fields, iframe_has_attachment = self._extract_form_fields(iframe_html, abs_src, client, depth + 1)

                has_attachment |= iframe_has_attachment

                if iframe_fields:
                    for line in iframe_fields.split("\n"):
                        if line and line not in fields:
                            fields.append(line)

        return "\n".join(fields), has_attachment

    # ------------------------------------------------------------------
    # メインクロール処理
    # ------------------------------------------------------------------

    def crawl_and_analyze(self, start_url: str) -> tuple[int | str, int | str, str, str, str, str, str, bool]:
        """ウェブサイトを巡回し、100ページに達した時点で打ち切る。"""
        if not start_url.startswith(("http://", "https://")):
            primary_url = f"https://{start_url}"
            fallback_url = f"http://{start_url}"
        else:
            primary_url = start_url
            fallback_url = start_url.replace("https://", "http://") if start_url.startswith("https://") else ""

        base_domain_clean = self._get_clean_domain(primary_url)
        start_time = time.time()

        visited: set[str] = set()
        queued_urls: set[str] = set()  # dequeの線形走査を避けるための重複チェック用
        queue: deque[tuple[str, int]] = deque()
        is_over_100 = False

        max_depth = 0
        contact_fields = ""
        has_attachment = False
        global_nav_menus: list[str] = []
        site_purpose = ""
        cms_name = ""
        html_src = ""

        def normalize_url(url: str) -> str:
            parsed = urlparse(url)
            clean_path = posixpath.normpath(parsed.path)
            if clean_path == ".":
                clean_path = "/"
            clean_path = re.sub(r"/index\.(html|php)$", "", clean_path)
            if clean_path.endswith("/") and clean_path != "/":
                clean_path = clean_path.rstrip("/")
            return parsed._replace(path=clean_path, query="", fragment="").geturl()

        def enqueue(url: str, path_depth: int, priority: bool) -> None:
            if url in visited or url in queued_urls or len(visited) >= 100:
                return
            queued_urls.add(url)
            if priority:
                queue.appendleft((url, path_depth))
            else:
                queue.append((url, path_depth))

        try:
            with httpx.Client(
                headers=self.headers,
                timeout=self.page_timeout,
                follow_redirects=True,
                verify=self.verify_ssl,
            ) as client:
                try:
                    response = client.get(primary_url)
                    response.raise_for_status()
                    first_url = str(response.url)
                    first_html = self._decode_response(response)
                    queue.append((str(response.url), 0))
                    queued_urls.add(str(response.url))
                except Exception:
                    if fallback_url:
                        try:
                            response = client.get(fallback_url)
                            response.raise_for_status()
                            queue.append((str(response.url), 0))
                            queued_urls.add(str(response.url))
                        except Exception:
                            return (0, 0, "", "", "", "", "", False)
                    else:
                        return (0, 0, "", "", "", "", "", False)

                previous_url = ""
                first_html = ""
                first_url = ""

                while queue:
                    if len(visited) >= 100:
                        is_over_100 = True
                        break
                    if time.time() - start_time > self.timeout:
                        break

                    current_url, depth = queue.popleft()
                    queued_urls.discard(current_url)
                    norm_current = normalize_url(current_url)

                    if norm_current in visited:
                        continue

                    try:
                        visited.add(norm_current)
                        parsed_current = urlparse(norm_current)

                        if self._has_repeating_path_pattern(parsed_current.path):
                            continue

                        req_headers = dict(self.headers)
                        if previous_url:
                            req_headers["Referer"] = previous_url

                        if current_url == first_url and first_html:
                            current_html = first_html
                        else:
                            response = client.get(current_url, headers=req_headers)
                            if response.status_code != 200:
                                continue

                            current_html = self._decode_response(response)

                        # render_js指定時、初回ページのみPlaywrightでの再取得を試みる
                        if self.render_js and len(visited) == 1:
                            rendered = self._fetch_rendered_html(current_url)
                            if rendered:
                                current_html = rendered

                        previous_url = current_url
                        soup = BeautifulSoup(current_html, "html.parser")

                        if len(visited) == 1:
                            html_src = current_html

                        # 階層判定(トップページを深度1として扱う)
                        path_segments = [p for p in parsed_current.path.split("/") if p]
                        current_depth = len(path_segments)
                        if path_segments and path_segments[-1] in ["index.html", "index.php", "index.htm"]:
                            current_depth = max(0, current_depth - 1)
                        current_depth += 1  # トップページ自体を深度1として数える
                        max_depth = max(max_depth, current_depth)

                        detected = self._detect_cms(current_html)
                        if detected and not cms_name:
                            cms_name = detected

                        if len(visited) == 1:
                            site_purpose = self._extract_purpose_and_features(current_html)

                            nav = (
                                soup.find("nav")
                                or soup.find(id=re.compile(r"nav|menu|global", re.I))
                                or soup.find(class_=re.compile(r"nav|menu|global", re.I))
                                or soup.find("header")
                                or soup.find("footer")
                            )

                            if nav and isinstance(nav, Tag):
                                for skip_el in nav.find_all(
                                    ["h1", "h2", "h3", "span", "div", "ul"],
                                    class_=re.compile(r"logo|title|site-name|setting|language|choose|option", re.I),
                                ):
                                    skip_el.decompose()

                                for item in nav.find_all(["li", "a"]):
                                    menu_text = item.get_text(strip=True)
                                    if not menu_text:
                                        img = item.find("img")
                                        if img and isinstance(img, Tag):
                                            menu_text = img.get("alt", "") or img.get("data-label", "")
                                    menu_text = self._clean_menu_text(str(menu_text))

                                    if any(k in menu_text for k in ["about", "について", "株式会社", "有限会社", "機構", "法人"]):
                                        continue
                                    if any(lang in menu_text.lower() for lang in ["language", "english", "日本語", "中国語", "中國語", "한국어"]):
                                        continue
                                    if menu_text and len(menu_text) < 15 and menu_text not in global_nav_menus:
                                        global_nav_menus.append(menu_text)

                        # お問い合わせページの判定と解析(iframe再帰込み)
                        is_contact_url = any(k.lower() in current_url.lower() for k in self.CONTACT_KEYWORDS)
                        contact_link_tag = soup.find("a", string=re.compile(r"問い合わせ|問合せ|相談|コンタクト|送信", re.I))
                        has_contact_text = contact_link_tag is not None

                        if (is_contact_url or has_contact_text) and not contact_fields:
                            contact_fields, has_attachment = self._extract_form_fields(
                                current_html,
                                current_url,
                                client,
                                depth=0,
                            )

                            if not contact_fields:
                                framework = self._detect_js_framework(current_html)
                                if framework:
                                    logger.info("フォーム未検出、JSフレームワーク疑い(%s): %s", framework, current_url)

                        # 通常の内部リンク探索
                        for link in soup.find_all("a", href=True):
                            href = link["href"]
                            if self._is_valid_internal_link(current_url, href, base_domain_clean):
                                abs_href = urljoin(current_url, href)
                                norm_abs = normalize_url(abs_href)
                                parsed_abs = urlparse(norm_abs)
                                path_depth = len([p for p in parsed_abs.path.split("/") if p])
                                is_priority = any(k.lower() in norm_abs.lower() for k in ("contact", "inquiry", "otoiawase", "form"))
                                enqueue(norm_abs, path_depth, is_priority)

                        # JS遷移(onclick="location.href=..."やインラインscript)によるリンクも回収
                        for raw_href in self._extract_js_links(current_html):
                            if self._is_valid_internal_link(current_url, raw_href, base_domain_clean):
                                abs_href = urljoin(current_url, raw_href)
                                norm_abs = normalize_url(abs_href)
                                parsed_abs = urlparse(norm_abs)
                                path_depth = len([p for p in parsed_abs.path.split("/") if p])
                                is_priority = any(k.lower() in norm_abs.lower() for k in ("contact", "inquiry", "otoiawase", "form"))
                                enqueue(norm_abs, path_depth, is_priority)

                        time.sleep(0.04)

                    except httpx.RequestError:
                        continue

        except Exception as e:
            logger.warning("クローラー内で予期せぬエラーが発生しました: %s", e)

        site_structure = "\n".join(global_nav_menus[:10])
        final_page_count = "100ページ以上" if is_over_100 or len(visited) >= 100 else len(visited)

        display_depth: int | str = max_depth
        if max_depth > 10:
            display_depth = "要確認"

        return (
            final_page_count,
            display_depth,
            contact_fields,
            site_structure,
            site_purpose,
            html_src,
            cms_name,
            has_attachment,
        )
