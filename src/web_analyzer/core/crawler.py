import re
import time
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag


class WebCrawler:
    """同一ドメイン内の巡回、ページ数カウント、国内主要CMSの特定、および問い合わせページの解析を行うクローラー。"""

    def __init__(self, timeout: float = 12.0) -> None:
        self.timeout = timeout
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
            "Cache-Control": "max-age=0",
        }

    def _get_clean_domain(self, url: str) -> str:
        """URLからプロトコルや 'www.' を除去した、純粋なドメイン部分のみを抽出する。"""
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        return domain

    def _is_valid_internal_link(self, current_url: str, link_url: str, base_domain_clean: str) -> bool:
        """リンク先が、プロトコル（http/https）を問わず、同一の親ドメイン内にある有効なページか検証する。"""
        absolute_url = urljoin(current_url, link_url)
        parsed = urlparse(absolute_url)

        if parsed.scheme not in ("http", "https"):
            return False

        link_domain_clean = parsed.netloc.lower()
        if link_domain_clean.startswith("www."):
            link_domain_clean = link_domain_clean[4:]

        if link_domain_clean != base_domain_clean and not link_domain_clean.endswith("." + base_domain_clean):
            return False

        path = parsed.path.lower()
        invalid_extensions = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".zip", ".tar", ".gz", ".mp3", ".mp4", ".css", ".js", ".xml", ".txt")
        if any(path.endswith(ext) for ext in invalid_extensions):
            return False

        return True

    def _detect_cms(self, html_src: str) -> str:
        """HTMLソース内から、国内で利用される主要なCMSのシグネチャを検知し、CMS名を返す。"""
        html_lower = html_src.lower()

        if "wp-content" in html_lower or "wp-includes" in html_lower or 'content="wordpress' in html_lower:
            return "WordPress"

        if "/.shared/mt-static/" in html_lower:
            return "MovableType.net"
        if "mt-static" in html_lower or 'content="movable type' in html_lower:
            return "Movable Type"

        if "eccube" in html_lower or "user_data/packages/default" in html_lower:
            return "EC-CUBE"

        if "concrete5" in html_lower or "/ccm/" in html_lower or "/updates/concrete" in html_lower:
            return "Concrete CMS"

        if "ablogcms" in html_lower or "/themes/system/images/" in html_lower:
            return "a-blog cms"

        if "basercms" in html_lower or "baser_helper" in html_lower:
            return "baserCMS"

        if "shopify" in html_lower or "cdn.shopify.com" in html_lower:
            return "Shopify"

        if "microcms" in html_lower or "images.microcms-assets.io" in html_lower:
            return "microCMS"

        if "wixstatic.com" in html_lower or 'content="wix.com' in html_lower:
            return "Wix"

        if "makeshop.jp" in html_lower or "/shopimages/" in html_lower:
            return "MakeShop"

        if "dotnetnuke" in html_lower or "__dnnvariable" in html_lower or 'content="dotnetnuke' in html_lower:
            return "DNN"

        return ""

    def _extract_purpose_and_features(self, html: str) -> str:
        """トップページのHTMLから用途を判定する。"""
        soup = BeautifulSoup(html, "html.parser")

        purpose = ""
        desc_tag = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
        if desc_tag and isinstance(desc_tag, Tag):
            val = desc_tag.get("content")
            if isinstance(val, str) and val.strip():
                purpose = val.strip()

        if not purpose and soup.title:
            purpose = soup.title.text.strip()

        if not purpose:
            h1_tag = soup.find("h1")
            if h1_tag:
                purpose = h1_tag.text.strip()

        if len(purpose) > 100:
            purpose = purpose[:100] + "..."

        return purpose

    def _extract_form_fields(self, html: str) -> str:
        """入力フォームから入力項目名を取得、または外部の代表的埋め込みフォームを検出する。"""
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
        for label in form.find_all("label"):
            text = label.get_text(strip=True)
            if text and text not in fields:
                fields.append(text)

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

        if len(fields) < 2:
            form_text = form.get_text().lower()
            field_patterns = {
                "お名前": r"name|氏名|名前|お名前|担当者",
                "メールアドレス": r"mail|email|アドレス|連絡先|メール",
                "電話番号": r"tel|phone|電話|番号",
                "会社名": r"company|corp|会社|組織|法人",
                "お問い合わせ内容": r"content|message|body|内容|問合せ|質問|自由記述",
            }
            for field_name, pattern in field_patterns.items():
                if re.search(pattern, form_text) and field_name not in fields:
                    fields.append(field_name)

        return "\n".join(fields[:15])

    def crawl_and_analyze(self, start_url: str) -> tuple[int | str, int, str, str, str, str, str]:
        """巡回を行い、用途を優先判定しつつ解析を行う。100ページに達した時点で打ち切る。"""
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
        is_over_100 = False  # 100ページ以上フラグ

        max_depth = 0
        contact_fields = ""
        global_nav_menus: list[str] = []
        site_purpose = ""
        cms_name = ""

        html_src = ""
        page_timeout = 2.5

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
                    # 100ページ以上の場合は打ち切り
                    if len(visited) >= 100:
                        is_over_100 = True
                        break

                    if time.time() - start_time > self.timeout:
                        break

                    current_url, depth = queue.pop(0)
                    normalized_url = current_url.split("#")[0].rstrip("/")
                    if normalized_url in visited:
                        continue

                    try:
                        response = client.get(current_url)

                        if len(visited) >= 100:
                            is_over_100 = True
                            break

                        visited.add(normalized_url)
                        max_depth = max(max_depth, depth)

                        html_src = response.text

                        detected = self._detect_cms(html_src)
                        if detected != "" and cms_name == "":
                            cms_name = detected

                        soup = BeautifulSoup(html_src, "html.parser")

                        if len(visited) == 1:
                            site_purpose = self._extract_purpose_and_features(html_src)

                            nav = soup.find(["nav", "header"]) or soup.find(class_=lambda x: x and ("menu" in x or "nav" in x))
                            if nav and isinstance(nav, Tag):
                                for item in nav.find_all(["li", "a"]):
                                    menu_text = item.get_text(strip=True)
                                    if menu_text and len(menu_text) < 15 and menu_text not in global_nav_menus:
                                        global_nav_menus.append(menu_text)

                        is_contact_url = any(k in current_url.lower() for k in ["contact", "inquiry", "otoiawase", "entry", "support", "form", "mail"])
                        has_contact_text = False
                        contact_link_tag = soup.find("a", string=re.compile(r"問い合わせ|問合せ|相談|コンタクト|送信", re.I))
                        if contact_link_tag:
                            has_contact_text = True

                        if (is_contact_url or has_contact_text) and not contact_fields:
                            contact_fields = self._extract_form_fields(html_src)

                        for link in soup.find_all("a", href=True):
                            href = link["href"]
                            if self._is_valid_internal_link(current_url, href, base_domain_clean):
                                abs_href = urljoin(current_url, href)
                                abs_normalized = abs_href.split("#")[0].rstrip("/")
                                if abs_normalized not in visited:
                                    path_depth = len([p for p in urlparse(abs_href).path.split("/") if p])
                                    queue.append((abs_href, path_depth))

                        time.sleep(0.05)

                    except httpx.RequestError:
                        continue

        except Exception as e:
            print(f"[Warning] クローラー内で予期せぬエラーが発生しました: {e}")

        site_structure = "\n".join(global_nav_menus[:10])

        # 100ページ以上の判定結果を適用
        final_page_count = "100ページ以上" if is_over_100 or len(visited) >= 100 else len(visited)

        return (
            final_page_count,
            max_depth,
            contact_fields,
            site_structure,
            site_purpose,
            "",
            cms_name,
        )
