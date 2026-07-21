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
        html_lower = html.lower()
        # 物件検索などの独自システム検知ワード
        if any(k in html_lower for k in ["物件検索", "空室検索", "不動産検索"]):
            return "物件検索サイト"
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
        """<form>の中に1つでも入力要素があれば抽出し、検索窓や無関係なテキストは無視する"""
        soup = BeautifulSoup(html, "html.parser")
        forms = soup.find_all("form")

        if not forms:
            return ""

        fields: list[str] = []

        for form in forms:
            # 追加：検索フォーム（サイト内検索窓）を完全に除外するガード
            form_id = form.get("id", "").lower()
            form_class = "".join(form.get("class", [])).lower()
            form_action = form.get("action", "").lower()

            if "search" in form_id or "search" in form_class or "search" in form_action:
                continue  # 検索キーワード用のフォームなのでスキップ

            # フォーム内の入力要素（ボタン系や非表示は除外）を確認
            inputs = form.find_all(["input", "textarea", "select"])
            valid_inputs = []
            for inp in inputs:
                itype = inp.get("type", "").lower()
                if itype in ["hidden", "submit", "button", "image", "reset"]:
                    continue
                valid_inputs.append(inp)

            # 条件：フォームの中に1つでも有効な入力要素があれば問い合わせページとみなす
            if len(valid_inputs) >= 1:
                # フォーム内の項目ラベル（th、label、またはplaceholder）を探索
                labels = form.find_all(["th", "label"])
                for lbl in labels:
                    txt = lbl.get_text(strip=True).replace("※", "").replace("必須", "")
                    if txt and len(txt) < 20 and txt not in fields:
                        fields.append(txt)

                # placeholder からも補填
                for inp in valid_inputs:
                    ph = inp.get("placeholder", "")
                    if ph and len(ph) < 20 and ph not in fields:
                        fields.append(ph)

        # 項目がうまく取れなかった場合の最低限のフォールバック
        if not fields and any(form.find_all(["input", "textarea"]) for form in forms):
            return "お問い合わせ内容"

        # 改行区切りで縦並びにする
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

        try:
            with httpx.Client(
                headers=self.headers,
                timeout=page_timeout,
                follow_redirects=True,
                verify=False,
            ) as client:
                try:
                    response = client.get(primary_url)
                    queue.append((str(response.url), 0))
                except Exception:
                    if fallback_url:
                        try:
                            response = client.get(fallback_url)
                            queue.append((str(response.url), 0))
                        except Exception:
                            return (0, 0, "", "", "", "", "")
                    else:
                        return (0, 0, "", "", "", "", "")

                while queue:
                    if len(visited) >= 100:
                        is_over_100 = True
                        break

                    if time.time() - start_time > self.timeout:
                        break

                    current_url, depth = queue.pop(0)

                    # URL正規化のクレンジング強化（末尾のスラッシュ違いやindex.htmlの重複防止）
                    normalized_url = current_url.split("#")[0].split("?")[0].rstrip("/")
                    if normalized_url.endswith("/index.html") or normalized_url.endswith("/index.php"):
                        normalized_url = re.sub(r"/index\.(html|php)$", "", normalized_url)

                    if normalized_url in visited:
                        continue

                    try:
                        response = client.get(current_url)

                        if len(visited) >= 100:
                            is_over_100 = True
                            break

                        visited.add(normalized_url)

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

                            # 構成列（グロナビ）：#topmenuや画像、フッターナビの対応強化
                            nav = (
                                soup.find(["nav", "header"])
                                or soup.find(id=re.compile(r"nav|menu|global", re.I))
                                or soup.find(class_=re.compile(r"nav|menu|global", re.I))
                                or soup.find("footer")  # ヘッダーにない場合の2段階目バックアップ
                            )

                            if nav and isinstance(nav, Tag):
                                # ロゴやサイト名が入っている「見出しタグ」や「ロゴクラス」を事前に除外
                                for skip_el in nav.find_all(["h1", "h2", "h3", "span"], class_=re.compile(r"logo|title|site-name", re.I)):
                                    skip_el.decompose()  # 要素そのものを消去して巻き込みを防ぐ

                                for item in nav.find_all(["li", "a"]):
                                    # 通常テキストの抽出
                                    menu_text = item.get_text(strip=True)

                                    # 画像ナビ（alt や data-label）の救済
                                    if not menu_text:
                                        img = item.find("img")
                                        if img and isinstance(img, Tag):
                                            menu_text = img.get("alt", "") or img.get("data-label", "")

                                    menu_text = self._clean_menu_text(str(menu_text))

                                    # テキスト内容によるフィルタリング（サイト名によく使われる文言を直接除外）
                                    if any(k in menu_text for k in ["について", "株式会社", "有限会社", "機構", "法人"]):
                                        continue

                                    if menu_text and len(menu_text) < 15 and menu_text not in global_nav_menus:
                                        global_nav_menus.append(menu_text)

                        # 問い合わせページの判定
                        is_contact_url = any(k in current_url.lower() for k in ["contact", "inquiry", "otoiawase", "entry", "support", "form", "mail"])
                        has_contact_text = False
                        contact_link_tag = soup.find("a", string=re.compile(r"問い合わせ|問合せ|相談|コンタクト|送信", re.I))
                        if contact_link_tag:
                            has_contact_text = True

                        if (is_contact_url or has_contact_text) and not contact_fields:
                            contact_fields = self._extract_form_fields(current_html)

                        # 内部リンクの探索（多言語パラメータやWordPressの個別記事URLを拾う調整）
                        for link in soup.find_all("a", href=True):
                            href = link["href"]
                            if self._is_valid_internal_link(current_url, href, base_domain_clean):
                                abs_href = urljoin(current_url, href)
                                abs_normalized = abs_href.split("#")[0].rstrip("/")
                                if abs_normalized not in visited:
                                    path_depth = len([p for p in urlparse(abs_href).path.split("/") if p])
                                    queue.append((abs_href, path_depth))

                        time.sleep(0.04)

                    except httpx.RequestError:
                        continue

        except Exception as e:
            print(f"[Warning] クローラー内で予期せぬエラーが発生しました: {e}")

        site_structure = "\n".join(global_nav_menus[:10])
        final_page_count = "100ページ以上" if is_over_100 or len(visited) >= 100 else len(visited)

        # scraper_service.py の受け取り順序と完全一致
        return (
            final_page_count,
            max_depth,
            contact_fields,
            site_structure,
            site_purpose,
            html_src,
            cms_name,
        )
