import time
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag


class WebCrawler:
    """同一ドメイン内の巡回、ページ数カウント、および問い合わせページの解析を行うクローラー。"""

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) python-web-analyzer"
        }

    def _get_domain(self, url: str) -> str:
        """URLからドメイン（netloc）を抽出する。"""
        parsed = urlparse(url)
        return parsed.netloc

    def _is_valid_internal_link(self, current_url: str, link_url: str, base_domain: str) -> bool:
        """リンク先が同一ドメイン内の有効なWebページであるか検証する。"""
        absolute_url = urljoin(current_url, link_url)
        parsed = urlparse(absolute_url)

        # ドメインが一致し、かつHTTP(S)プロトコルであること
        if parsed.netloc != base_domain or parsed.scheme not in ("http", "https"):
            return False

        # 静的ファイルや非Webページ（画像、PDF、zip、tel、mailtoなど）を除外
        path = parsed.path.lower()
        invalid_extensions = (
            ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".zip", ".tar",
            ".gz", ".mp3", ".mp4", ".css", ".js", ".xml"
        )
        if any(path.endswith(ext) for ext in invalid_extensions):
            return False

        return True

    def _extract_form_fields(self, html: str) -> str:
        """HTML内のフォーム要素から、お問合せ項目名（labelやplaceholderなど）を改行区切りで抽出する。"""
        soup = BeautifulSoup(html, "html.parser")
        # 主要なフォームコンテナまたは直接input要素を探索
        form = soup.find("form")
        if not form or not isinstance(form, Tag):
            # フォームタグがない場合はページ全体のインプット要素から推測
            form = soup

        fields: list[str] = []

        # label要素からテキストを抽出
        for label in form.find_all("label"):
            text = label.get_text(strip=True)
            if text and text not in fields:
                fields.append(text)

        # labelが見つからない場合、input/textarea/select の placeholder や name から補完
        for elem in form.find_all(["input", "textarea", "select"]):
            # 送信ボタンや非表示フィールド、チェックボックス等はスキップ
            elem_type = elem.get("type", "")
            if elem_type in ("submit", "hidden", "button", "image", "radio"):
                continue

            placeholder = elem.get("placeholder", "")
            if placeholder and placeholder not in fields:
                fields.append(placeholder)
                continue

            name = elem.get("name", "")
            # よくある項目名（かつlabel等にまだ追加されていないもの）をマッピング
            name_mapping = {
                "name": "お名前",
                "email": "メールアドレス",
                "tel": "電話番号",
                "subject": "件名",
                "message": "お問合せ内容",
            }
            if name in name_mapping and name_mapping[name] not in fields:
                fields.append(name_mapping[name])

        # 改行区切りの縦並びで表示
        return "\n".join(fields[:15])  # 項目が多すぎる場合は上限15個に制限

    def crawl_and_analyze(self, start_url: str) -> tuple[int, int, str, str]:
        """10秒のタイムアウト制約のなかで、同一ドメイン内を巡回し各種解析を行う。

        Returns:
            tuple[int, int, str, str]: (総ページ数, 最大階層数, 問い合わせ項目, サイト構成)
        """
        # プロトコルの自動補完
        if not start_url.startswith(("http://", "https://")):
            start_url = f"https://{start_url}"

        base_domain = self._get_domain(start_url)
        start_time = time.time()

        # クロール管理用セット
        visited: set[str] = set()
        queue: list[tuple[str, int]] = [(start_url, 0)]  # (URL, 現在の階層深さ)

        max_depth = 0
        contact_fields = ""
        global_nav_menus: list[str] = []

        try:
            with httpx.Client(headers=self.headers, timeout=3.0, follow_redirects=True) as client:
                while queue:
                    # 全体の処理時間が制限時間（10秒）を超えた場合は即座に打ち切り
                    if time.time() - start_time > self.timeout:
                        break

                    current_url, depth = queue.pop(0)

                    # 正規化して重複判定
                    normalized_url = current_url.split("#")[0].rstrip("/")
                    if normalized_url in visited:
                        continue

                    try:
                        response = client.get(current_url)
                        visited.add(normalized_url)
                        max_depth = max(max_depth, depth)

                        # HTML解析
                        soup = BeautifulSoup(response.text, "html.parser")

                        # 1. トップページ解析（初回巡回時）からグローバルナビを抽出
                        if len(visited) == 1:
                            # 一般的なナビゲーションタグやクラスからメニューを抽出
                            nav = soup.find(["nav", "header"]) or soup.find(class_=lambda x: x and "menu" in x or "nav" in x)
                            if nav and isinstance(nav, Tag):
                                for item in nav.find_all(["li", "a"]):
                                    menu_text = item.get_text(strip=True)
                                    # 冗長な空白文字や、空のメニュー、長すぎる文字列を除外
                                    if menu_text and len(menu_text) < 15 and menu_text not in global_nav_menus:
                                        global_nav_menus.append(menu_text)

                        # 2. 問い合わせページの探索
                        # URLに "contact"、"inquiry"、"otoiawase"、または日本語の「問い合わせ」等を含む場合
                        is_contact_url = any(
                            k in current_url.lower() for k in ["contact", "inquiry", "otoiawase"]
                        )
                        if is_contact_url and not contact_fields:
                            contact_fields = self._extract_form_fields(response.text)

                        # 3. 同一ドメイン内リンクの探索
                        for link in soup.find_all("a", href=True):
                            href = link["href"]
                            if self._is_valid_internal_link(current_url, href, base_domain):
                                abs_href = urljoin(current_url, href)
                                abs_normalized = abs_href.split("#")[0].rstrip("/")
                                if abs_normalized not in visited:
                                    # 階層深さのカウント: パスのスラッシュの数で簡易計算
                                    path_depth = len([p for p in urlparse(abs_href).path.split("/") if p])
                                    queue.append((abs_href, path_depth))

                        # サーバー不可を考慮し、微小なディレイを挟む
                        time.sleep(0.1)

                    except httpx.RequestError:
                        # 個別ページの取得エラーはログを残して次のページへ
                        continue

        except Exception as e:
            # 予期せぬ重大なエラーは安全にいなして終了
            print(f"[Warning] クローラー内で予期せぬエラーが発生しました: {e}")

        # 構成（改行区切り）の作成
        site_structure = "\n".join(global_nav_menus[:10])

        return len(visited), max_depth, contact_fields, site_structure
