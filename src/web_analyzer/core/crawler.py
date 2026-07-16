import re
import time
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag


class WebCrawler:
    """同一ドメイン内の巡回、ページ数カウント、および問い合わせページの解析を行うクローラー。"""

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout
        self.headers = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) python-web-analyzer")}

    def _get_domain(self, url: str) -> str:
        """URLからドメイン（netloc）を抽出する。"""
        parsed = urlparse(url)
        return parsed.netloc

    def _is_valid_internal_link(self, current_url: str, link_url: str, base_domain: str) -> bool:
        """リンク先が同一ドメイン内の有効なWebページであるか検証する。"""
        absolute_url = urljoin(current_url, link_url)
        parsed = urlparse(absolute_url)

        if parsed.netloc != base_domain or parsed.scheme not in ("http", "https"):
            return False

        path = parsed.path.lower()
        invalid_extensions = (
            ".pdf",
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".zip",
            ".tar",
            ".gz",
            ".mp3",
            ".mp4",
            ".css",
            ".js",
            ".xml",
        )
        if any(path.endswith(ext) for ext in invalid_extensions):
            return False

        return True

    def _extract_purpose_and_features(self, html: str, url: str) -> tuple[str, str]:
        """トップページのHTMLから用途をフォールバック抽出（Description -> Title -> H1）し、

        特徴（備考）を自動合成する。
        """
        soup = BeautifulSoup(html, "html.parser")

        # ---------------------------------------------------------------------
        # 用途（Description -> Title -> H1）
        # ---------------------------------------------------------------------
        purpose = ""
        # 優先①: meta description (OGP含む)
        desc_tag = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
        if desc_tag and isinstance(desc_tag, Tag):
            val = desc_tag.get("content")
            if isinstance(val, str) and val.strip():
                purpose = val.strip()

        # 優先②: Title
        if not purpose and soup.title:
            purpose = soup.title.text.strip()

        # 優先③: H1
        if not purpose:
            h1_tag = soup.find("h1")
            if h1_tag:
                purpose = h1_tag.text.strip()

        if not purpose:
            purpose = "コーポレートサイト（推測）"

        if len(purpose) > 100:
            purpose = purpose[:100] + "..."

        # ---------------------------------------------------------------------
        # 備考・特徴（キーワード判定）
        # ---------------------------------------------------------------------
        features: list[str] = []
        text_content = soup.get_text().lower()

        if "採用" in text_content or "recruit" in url.lower():
            features.append("採用活動に注力している")
        if any(
            k in text_content
            for k in [
                "カート",
                "買い物かご",
                "商品一覧",
                "特定商取引",
                "cart",
                "shop",
            ]
        ):
            features.append("EC/オンラインショップ機能を有している")
        if "wp-content" in html or "wp-includes" in html:
            features.append("WordPressによるWeb運用を行っている")
        if "instagram.com" in html:
            features.append("InstagramなどのSNSを活用したWebマーケティングを実施している")

        features_summary = (
            "、".join(features) + "特徴が見受けられます。" if features else "シンプルな構成のコーポレート・Web紹介サイトの特徴を持っています。"
        )

        return purpose, features_summary

    def _extract_form_fields(self, html: str) -> str:
        """HTML内のフォーム要素や埋め込み外部サービスからお問合せ項目名を抽出する。"""
        # 外部フォーム埋め込みサービスの検出
        if "formrun.jp" in html:
            return "お名前\nメールアドレス\nお問い合わせ内容 (Formrun埋め込み)"
        if "tayori.com" in html:
            return "お名前\nメールアドレス\nお問い合わせ内容 (Tayori埋め込み)"
        if "forms.gle" in html:
            return "お名前\nメールアドレス\nお問い合わせ内容 (Googleフォーム埋め込み)"

        soup = BeautifulSoup(html, "html.parser")
        form = soup.find("form")
        if not form or not isinstance(form, Tag):
            form = soup

        fields: list[str] = []

        # label要素からテキストを抽出
        for label in form.find_all("label"):
            text = label.get_text(strip=True)
            if text and text not in fields:
                fields.append(text)

        # placeholder や name から補完
        for elem in form.find_all(["input", "textarea", "select"]):
            elem_type = elem.get("type", "")
            if elem_type in ("submit", "hidden", "button", "image", "radio"):
                continue

            placeholder = elem.get("placeholder", "")
            if placeholder and placeholder not in fields:
                fields.append(placeholder)
                continue

            name = elem.get("name", "")
            name_mapping = {
                "name": "お名前",
                "email": "メールアドレス",
                "tel": "電話番号",
                "subject": "件名",
                "message": "お問合せ内容",
            }
            if name in name_mapping and name_mapping[name] not in fields:
                fields.append(name_mapping[name])

        # 抽出項目が極めて少ない場合、問い合わせ項目を確実にするための正規表現マッチング
        if len(fields) < 2:
            form_text = form.get_text().lower()
            field_patterns = {
                "お名前": r"name|氏名|名前|お名前|担当者",
                "メールアドレス": r"mail|email|アドレス|連絡先",
                "電話番号": r"tel|phone|電話|番号",
                "会社名": r"company|corp|会社|組織|法人",
                "お問い合わせ内容": r"content|message|body|内容|問合せ|質問|自由記述",
            }
            for field_name, pattern in field_patterns.items():
                if re.search(pattern, form_text) and field_name not in fields:
                    fields.append(field_name)

        return "\n".join(fields[:15])

    def crawl_and_analyze(self, start_url: str) -> tuple[int, int, str, str, str, str]:
        """10秒のタイムアウト制約の中で巡回し、用途・特徴（備考）を優先判定しつつ解析を行う。

        Returns:
            tuple[int, int, str, str, str, str]:
            (総ページ数, 最大階層数, 問い合わせ項目, サイト構成, 用途, 備考)
        """
        if not start_url.startswith(("http://", "https://")):
            start_url = f"https://{start_url}"

        base_domain = self._get_domain(start_url)
        start_time = time.time()

        visited: set[str] = set()
        queue: list[tuple[str, int]] = [(start_url, 0)]

        max_depth = 0
        contact_fields = ""
        global_nav_menus: list[str] = []
        site_purpose = ""
        site_remarks = ""

        try:
            with httpx.Client(headers=self.headers, timeout=3.0, follow_redirects=True) as client:
                while queue:
                    if time.time() - start_time > self.timeout:
                        break

                    current_url, depth = queue.pop(0)
                    normalized_url = current_url.split("#")[0].rstrip("/")
                    if normalized_url in visited:
                        continue

                    try:
                        response = client.get(current_url)
                        visited.add(normalized_url)
                        max_depth = max(max_depth, depth)

                        soup = BeautifulSoup(response.text, "html.parser")

                        # 初回巡回（トップページ）のタイミングで用途、特徴、ナビを抽出
                        if len(visited) == 1:
                            site_purpose, site_remarks = self._extract_purpose_and_features(response.text, start_url)

                            nav = soup.find(["nav", "header"]) or soup.find(class_=lambda x: x and "menu" in x or "nav" in x)
                            if nav and isinstance(nav, Tag):
                                for item in nav.find_all(["li", "a"]):
                                    menu_text = item.get_text(strip=True)
                                    if menu_text and len(menu_text) < 15 and menu_text not in global_nav_menus:
                                        global_nav_menus.append(menu_text)

                        # 問い合わせページの探索
                        is_contact_url = any(k in current_url.lower() for k in ["contact", "inquiry", "otoiawase"])
                        if is_contact_url and not contact_fields:
                            contact_fields = self._extract_form_fields(response.text)

                        # 同一ドメイン内リンクの探索
                        for link in soup.find_all("a", href=True):
                            href = link["href"]
                            if self._is_valid_internal_link(current_url, href, base_domain):
                                abs_href = urljoin(current_url, href)
                                abs_normalized = abs_href.split("#")[0].rstrip("/")
                                if abs_normalized not in visited:
                                    path_depth = len([p for p in urlparse(abs_href).path.split("/") if p])
                                    queue.append((abs_href, path_depth))

                        time.sleep(0.1)

                    except httpx.RequestError:
                        continue

        except Exception as e:
            print(f"[Warning] クローラー内で予期せぬエラーが発生しました: {e}")

        site_structure = "\n".join(global_nav_menus[:10])

        return (
            len(visited),
            max_depth,
            contact_fields,
            site_structure,
            site_purpose,
            site_remarks,
        )
