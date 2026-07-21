import re
import time
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag


class WebCrawler:
    """ウェブサイトを巡回し、構成、CMS、問い合わせ項目、階層、用途などを解析するクローラー。"""

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout
        self.headers = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")}

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

        # 拡張子チェックの緩和（phpや動的パラメータも通す）
        if any(parsed_abs.path.lower().endswith(ext) for ext in [".pdf", ".jpg", ".jpeg", ".png", ".gif", ".zip"]):
            return False

        return abs_domain == base_domain

    def _detect_cms(self, html: str) -> str:
        html_lower = html.lower()
        if "wp-content" in html_lower or "wp-includes" in html_lower:
            return "WordPress"
        if "basercms" in html_lower or "bc-" in html_lower:
            return "baserCMS"
        return ""

    def _extract_purpose_and_features(self, html: str) -> str:
        """HTMLから優先順位（description > title > h1）に従って文字列をそのまま抽出する"""
        if not html:
            return ""

        soup = BeautifulSoup(html, "html.parser")

        # 1. 最優先: meta description (または og:description) の文字列
        desc_tag = soup.find("meta", attrs={"name": "description"}) or soup.find(
            "meta", attrs={"property": "og:description"}
        )
        if desc_tag:
            desc_text = desc_tag.get("content", "").strip()
            if desc_text:
                return desc_text

        # 2. 第2優先: title タグの中身
        if soup.title and soup.title.string:
            title_text = soup.title.string.strip()
            if title_text:
                return title_text

        # 3. 第3優先: h1 タグの中の文字列
        h1_tag = soup.find("h1")
        if h1_tag:
            h1_text = h1_tag.get_text(strip=True)
            if h1_text:
                return h1_text

        # いずれも取得できなかった場合のみ空文字を返す
        return ""

    def _clean_menu_text(self, text: str) -> str:
        """交通アクセスAccess のような英日混在から不要な英語やスペースを綺麗にする"""
        text = re.sub(r"\s+", "", text)
        # 日本語の後ろにくっついている英語（Access等）をカット
        match = re.match(r"^([ぁ-んァ-ヶー一-龠々]+)[A-Za-z]+$", text)
        if match:
            return match.group(1)
        return text

    def _extract_form_fields(self, html: str) -> str:
        """<form>の中から検索窓を除外し、有効な入力項目のラベルを抽出する"""
        soup = BeautifulSoup(html, "html.parser")
        forms = soup.find_all("form")

        if not forms:
            return ""

        fields: list[str] = []
        has_valid_form = False  # 本物の問い合わせフォームが存在したかどうかのフラグ

        for form in forms:
            form_id = form.get("id", "").lower()
            form_class = "".join(form.get("class", [])).lower()
            form_action = form.get("action", "").lower()

            # 検索フォーム（サイト内検索窓）を完全に除外
            if "search" in form_id or "search" in form_class or "search" in form_action:
                continue

            # フォーム内の入力要素（ボタン系や非表示は除外）を確認
            inputs = form.find_all(["input", "textarea", "select"])
            valid_inputs = []
            for inp in inputs:
                itype = inp.get("type", "").lower()
                if itype in ["hidden", "submit", "button", "image", "reset"]:
                    continue
                valid_inputs.append(inp)

            # 条件：検索窓ではなく、有効な入力要素が1つ以上あるフォームを本物とみなす
            if len(valid_inputs) >= 1:
                has_valid_form = True

                # フォーム内の項目ラベル（th、label）を探索
                labels = form.find_all(["th", "label"])
                for lbl in labels:
                    txt = lbl.get_text(strip=True).replace("※", "").replace("必須", "")
                    # 前後のあらゆる空白文字（半角/全角/特殊空白）を安全に除去
                    txt = re.sub(r"^[ \s \xa0]+|[ \s \xa0]+$", "", txt)

                    if txt and len(txt) < 20 and txt not in fields:
                        fields.append(txt)

                # placeholder からも補填
                for inp in valid_inputs:
                    ph = inp.get("placeholder", "")
                    ph = re.sub(r"^[ \s \xa0]+|[ \s \xa0]+$", "", ph)

                    if ph and len(ph) < 20 and ph not in fields:
                        fields.append(ph)

        # 本物のフォームがあるのに項目名が1つも取り出せなかった場合のみ、空欄にする
        if has_valid_form and not fields:
            return ""

        # 正常に取得できた項目を改行区切りで返す（無関係なページでは空文字になるため、本物のページで上書き可能になる）
        return "\n".join(fields)

    def _extract_breadcrumbs_depth(self, soup: BeautifulSoup) -> int:
        """パンくずリストから実際の最大階層数を計算する"""
        # 一般的なパンくずのクラス名やID、構造化データを探索
        bc_elements = soup.find_all(class_=re.compile(r"breadcrumb|topicpath", re.I)) or soup.find_all(id=re.compile(r"breadcrumb|topicpath", re.I))
        if not bc_elements:
            bc_elements = soup.find_all(["ol", "ul"], class_=lambda x: x and ("nav" not in x.lower() and "menu" not in x.lower()))

        max_bc = 0
        for bc in bc_elements:
            items = bc.find_all(["li", "span", "a"])
            # 有効な階層テキストを持つ要素の数
            item_count = len(set([i.get_text(strip=True) for i in items if i.get_text(strip=True)]))
            # トップページを除いた階層数（「トップ > 会社概要」なら2アイテムなので1階層扱い、実質URL深度の同期用に調整）
            if item_count > 1:
                max_bc = max(max_bc, item_count - 1)
        return max_bc

    import re

    def crawl_and_analyze(self, start_url: str) -> tuple[int | str, int, str, str, str, str, str]:
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
        queue: list[tuple[str, int]] = []
        is_over_100 = False

        max_depth = 0
        contact_fields = ""
        global_nav_menus: list[str] = []
        site_purpose = ""
        cms_name = ""
        html_src = ""  # HTMLソース受け渡し用

        page_timeout = 3.0  # 巡回漏れを防ぐためタイムアウトを少し緩和

        def normalize_url(url: str) -> str:
            """URLの重複を排除するための正規化クレンジング"""
            u = url.split("#")[0].split("?")[0].rstrip("/")
            return re.sub(r"/index\.(html|php)$", "", u)

        try:
            with httpx.Client(
                headers=self.headers,
                timeout=page_timeout,
                follow_redirects=True,
                verify=False,
            ) as client:
                # 初期ページの接続試行
                try:
                    response = client.get(primary_url)
                    response.raise_for_status()
                    queue.append((str(response.url), 0))
                except Exception:
                    if fallback_url:
                        try:
                            response = client.get(fallback_url)
                            response.raise_for_status()
                            queue.append((str(response.url), 0))
                        except Exception:
                            return (0, 0, "", "", "", "", "")
                    else:
                        return (0, 0, "", "", "", "", "")

                # クロール主処理
                while queue:
                    # 100ページ制限に達していたら即時打ち切り
                    if len(visited) >= 100:
                        is_over_100 = True
                        break

                    # 全体タイムアウトチェック
                    if time.time() - start_time > self.timeout:
                        break

                    current_url, depth = queue.pop(0)
                    norm_current = normalize_url(current_url)

                    # すでに訪問済みならスキップ
                    if norm_current in visited:
                        continue

                    try:
                        # 訪問済みに即時登録（エラー時も何度も同じURLを叩かないためのガード）
                        visited.add(norm_current)

                        response = client.get(current_url)
                        if response.status_code != 200:
                            continue

                        current_html = response.text
                        soup = BeautifulSoup(current_html, "html.parser")

                        # トップページのHTMLソースを保存
                        if len(visited) == 1:
                            html_src = current_html

                        # 階層判定：パンくずリストがあれば最優先、なければURLの深さをフォールバック
                        bc_depth = self._extract_breadcrumbs_depth(soup)
                        current_depth = bc_depth if bc_depth > 0 else depth
                        max_depth = max(max_depth, current_depth)

                        detected = self._detect_cms(current_html)
                        if detected != "" and cms_name == "":
                            cms_name = detected

                        if len(visited) == 1:
                            site_purpose = self._extract_purpose_and_features(current_html)

                            # 構成列（グロナビ）：探索順序を厳格化（まず単体のnavを最優先にする）
                            nav = (
                                soup.find("nav")
                                or soup.find(id=re.compile(r"nav|menu|global", re.I))
                                or soup.find(class_=re.compile(r"nav|menu|global", re.I))
                                or soup.find("header")
                                or soup.find("footer")
                            )

                            if nav and isinstance(nav, Tag):
                                # ロゴや見出しに加え、言語・サイズ・配色設定ブロックを丸ごと除外
                                for skip_el in nav.find_all(
                                    ["h1", "h2", "h3", "span", "div", "ul"],
                                    class_=re.compile(
                                        r"logo|title|site-name|setting|language|choose|option",
                                        re.I,
                                    ),
                                ):
                                    skip_el.decompose()

                                for item in nav.find_all(["li", "a"]):
                                    menu_text = item.get_text(strip=True)

                                    # 画像ナビ（alt や data-label）の救済
                                    if not menu_text:
                                        img = item.find("img")
                                        if img and isinstance(img, Tag):
                                            menu_text = img.get("alt", "") or img.get("data-label", "")

                                    menu_text = self._clean_menu_text(str(menu_text))

                                    # テキスト内容によるフィルタリング
                                    if any(
                                        k in menu_text
                                        for k in [
                                            "について",
                                            "株式会社",
                                            "有限会社",
                                            "機構",
                                            "法人",
                                        ]
                                    ):
                                        continue

                                    if any(
                                        lang in menu_text.lower()
                                        for lang in [
                                            "language",
                                            "english",
                                            "日本語",
                                            "中国語",
                                            "中國語",
                                            "한국어",
                                        ]
                                    ):
                                        continue

                                    if menu_text and len(menu_text) < 15 and menu_text not in global_nav_menus:
                                        global_nav_menus.append(menu_text)

                        # 問い合わせページの判定
                        is_contact_url = any(
                            k in current_url.lower()
                            for k in [
                                "contact",
                                "inquiry",
                                "otoiawase",
                                "entry",
                                "support",
                                "form",
                                "mail",
                            ]
                        )
                        has_contact_text = False
                        contact_link_tag = soup.find(
                            "a",
                            string=re.compile(r"問い合わせ|問合せ|相談|コンタクト|送信", re.I),
                        )
                        if contact_link_tag:
                            has_contact_text = True

                        if (is_contact_url or has_contact_text) and not contact_fields:
                            contact_fields = self._extract_form_fields(current_html)

                        # 内部リンクの探索
                        for link in soup.find_all("a", href=True):
                            href = link["href"]
                            if self._is_valid_internal_link(current_url, href, base_domain_clean):
                                abs_href = urljoin(current_url, href)
                                norm_abs = normalize_url(abs_href)

                                # キューに入れる前にも訪問済み・タスク上限チェックを行う（無駄なキュー肥大化を防止）
                                if norm_abs not in visited and len(visited) < 100:
                                    path_depth = len([p for p in urlparse(abs_href).path.split("/") if p])
                                    queue.append((abs_href, path_depth))

                        time.sleep(0.04)

                    except httpx.RequestError:
                        continue

        except Exception as e:
            print(f"[Warning] クローラー内で予期せぬエラーが発生しました: {e}")

        site_structure = "\n".join(global_nav_menus[:10])
        final_page_count = "100ページ以上" if is_over_100 or len(visited) >= 100 else len(visited)

        return (
            final_page_count,
            max_depth,
            contact_fields,
            site_structure,
            site_purpose,
            html_src,
            cms_name,
        )
