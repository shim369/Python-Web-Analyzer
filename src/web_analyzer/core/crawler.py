import logging
import posixpath
import re
import time
import warnings
from collections import deque
from typing import cast
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag, XMLParsedAsHTMLWarning

from web_analyzer.models import LOGIN_KEYWORDS
from web_analyzer.utils.decorators import measure_time

logger = logging.getLogger(__name__)

# フィード(RSS/Atom)やsitemap.xml等、拡張子フィルタをすり抜けてしまったXMLリソースを
# 誤ってHTMLとしてパースしてしまうケースに備えた保険。実害はないため警告自体は抑制する。
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

# HTML解析に使うパーサー。
# 標準の "html.parser" は寛容すぎて、<li>タグが閉じられていない(</li>が無い)ような
# 実在の(特に古い/レガシーな)サイトでよくある崩れたHTMLに対し、後続の要素を
# 誤って「入れ子の子要素」として解釈してしまうことがある。その結果、
# グローバルナビ内の複数のメニュー項目のテキストが1つの巨大な文字列に
# 連結されてしまい、「構成」列が読めない内容になる原因となる。
# "lxml" はHTML5相当のタグ自動補完(暗黙のクローズ処理)を行うため、この種の
# 崩れたHTMLでも実ブラウザに近い、正しい兄弟要素構造として解釈できる。
# 万が一 lxml がインストールされていない環境でも動作は継続できるよう、
# その場合は従来の "html.parser" にフォールバックする。
try:
    import lxml  # noqa: F401

    HTML_PARSER = "lxml"
except ImportError:
    logger.warning("lxmlが見つからないため、HTML解析精度の低いhtml.parserにフォールバックします。")
    HTML_PARSER = "html.parser"


class WebCrawler:
    """ウェブサイトを巡回し、構成、CMS、問い合わせ項目、階層、用途などを解析するクローラー。

    v2: iframe内フォーム解析、JS遷移検知、label紐付け強化、div/dlフォーム対応、
    優先度キューのdeque化、logging化などを反映。
    v3: crawl_and_analyzeをtuple(9要素)返却に変更、繰り返しパストラップ検知を強化。
    """

    LOGIN_KEYWORDS = LOGIN_KEYWORDS

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

    # 多言語切り替えウィジェットらしきコンテナのclass/id判定用
    # (グローバルナビの中ではなく、ヘッダー上部などに単独で配置されるケースを拾うための正規表現)
    _MULTILANG_CONTAINER_RE = re.compile(
        r"^lang(?:uage)?$"  # class="lang" / class="language" のような単独指定にも対応
        r"|lang(?:uage)?[-_]?(?:switch|select|selector|list|menu|nav|area|box|bar|toggle|change|btn)"
        r"|i18n|locale[-_]?switch|multilingual|globalnav.*lang|lang.*nav|header.*lang|gnav.*lang",
        re.I,
    )

    # 「header」というタグ名/class/idを持たない(セマンティックなheader要素を使っていない)
    # レガシーな作りのサイトでも、ヘッダー相当の領域を拾えるようにするための正規表現
    _HEADER_LIKE_RE = re.compile(r"header|gnav|globalnav|utility|top-?bar|topnav|l-header", re.I)

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

        # RSS/Atomフィード等のXMLリソースはHTMLページではなく、通常のサイト構成・調査結果としては
        # 意味を持たない(かつlxmlでHTMLとしてパースするとXMLParsedAsHTMLWarningが出る)ため対象外とする
        path_lower = parsed_abs.path.lower()
        if path_lower.endswith((".xml", ".rss", ".atom")) or path_lower.rstrip("/").endswith("/feed"):
            return False
        if re.search(r"[?&]feed=", abs_url.lower()):
            return False

        return abs_domain == base_domain

    def _detect_cms(self, html: str) -> str:
        html = html.lower()

        cms_patterns = {
            "WP": [
                "wp-content",
                "wp-includes",
            ],
            "baserCMS": ["basercms"],
            "EC-CUBE": [
                "eccube",
            ],
            "Movable Type": [
                "mt-content",
                "mt-static",
            ],
            "MODX": [
                "modx",
            ],
            "Drupal": [
                "drupal",
                "drupal-settings-json",
            ],
            "Joomla!": [
                "joomla!",
            ],
            "TYPO3": [
                "typo3",
            ],
            "concrete5": [
                "concretecms",
                "concrete5",
            ],
            "XOOPS": [
                "xoops",
            ],
            "NetCommons": [
                "netcommons",
            ],
            "PowerCMS": [
                "powercms",
            ],
            "Craft CMS": [
                "craftcms",
            ],
            "Sitecore": [
                "sitecore",
            ],
            "Kentico": [
                "kentico",
            ],
            "SilverStripe": [
                "silverstripe",
            ],
            "Jimdo": [
                "jimdo",
            ],
            "Wix": [
                "wix.com",
                "_wixcss",
            ],
            "Shopify": [
                "shopify",
                "cdn.shopify.com",
            ],
            "ColorMe Shop": [
                "colorme",
            ],
            "MakeShop": [
                "makeshop",
            ],
            "futureshop": [
                "futureshop",
            ],
            "a-blog cms": [
                "a-blog",
            ],
            "RCMS": [
                "rcms",
            ],
            "HeartCore": [
                "heartcore",
            ],
            "BlueMonkey": [
                "bluemonkey",
            ],
            "BiNDup": [
                "bindup",
            ],
        }

        for cms, patterns in cms_patterns.items():
            if any(pattern in html for pattern in patterns):
                return cms

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

        soup = BeautifulSoup(html, HTML_PARSER)

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

        `/a/a/a` のような単純な繰り返しに加え、URLエンコードされたスラッグ等を
        含む任意長のセグメント塊(1〜6セグメント)が**連続して2回**現れるケースも
        検知する。JS遷移リンクの相対パス誤解決などで
        `/blog/2023/01/11/blog/2023/01/11/...` のように塊がどんどん
        追加されていくトラップは、3回繰り返しを待たずにここで早期に弾く。
        """
        # 単一セグメントの3連続繰り返し(/a/a/a など)は従来通り即弾く
        if re.search(r"([^/]+)/\1/\1", path):
            return True

        segments = [p for p in path.split("/") if p]
        if len(segments) < 2:
            return False

        # 1〜6セグメント単位の塊が、隣接して2回連続で出現していないかをチェック
        max_chunk_size = min(6, len(segments) // 2)
        for chunk_size in range(1, max_chunk_size + 1):
            for i in range(len(segments) - chunk_size * 2 + 1):
                a = segments[i : i + chunk_size]
                b = segments[i + chunk_size : i + chunk_size * 2]
                if a == b:
                    return True

        return False

    # ------------------------------------------------------------------
    # HTML取得・デコード
    # ------------------------------------------------------------------

    def _decode_response(self, response: httpx.Response) -> str:
        """レスポンスの文字コードを判定してデコードする(Shift_JIS系はcp932に正規化し、文字化けを防ぐ)"""
        # 1. まずHTMLの先頭部分から meta charset を安全に探す
        # asciiの代わりに latin-1 を使うと、バイト値を壊さずに文字列化して正規表現にかけられます
        raw_content_head = response.content[:2048].decode("latin-1", errors="ignore")
        meta_charset = re.search(r'charset=["\']?([a-zA-Z0-9_-]+)', raw_content_head, re.IGNORECASE)

        if meta_charset:
            encoding = meta_charset.group(1)
        else:
            # 2. metaタグにない場合は、httpxがヘッダー等から推測したエンコーディングを使用
            # (※ None や 'X-USER-DEFINED' などの無効な値への対策)
            guessed = response.encoding or response.charset_encoding
            encoding = guessed if (guessed and len(guessed) > 1) else "utf-8"

        # 3. Shift_JIS系のエンコーディングをWindows拡張の cp932 に統一
        # 「〜」や「①」、特殊な漢字（藏 など）の化け・欠損を防ぎます
        enc_lower = encoding.lower()
        if enc_lower in ["shift_jis", "shift-jis", "sjis", "x-sjis", "cp932"]:
            encoding = "cp932"
        elif enc_lower in ["euc-jp", "eucjp", "x-euc-jp"]:
            encoding = "euc-jp"
        else:
            # 念のため utf-8 と明示されていても、実際は別コードのケースへのフォールバック用
            pass

        # 4. 決定したエンコーディングでデコードを試みる
        try:
            return response.content.decode(encoding, errors="replace")
        except Exception:
            # 失敗した場合は、httpx標準の自動デコードに頼る
            try:
                return response.text
            except Exception:
                return response.content.decode("utf-8", errors="replace")

    def _fetch_rendered_html(self, url: str) -> tuple[str, str]:
        """Playwrightが利用可能ならレンダリング後のHTMLと最終URLを取得する。

        未導入時/失敗時は ("", "") を返す。
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.warning("Playwright未インストールのためJSレンダリングをスキップします: %s", url)
            return "", ""

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(user_agent=self.headers["User-Agent"])
                # httpx用のpage_timeoutをそのまま流用すると短すぎる/ネットワーク未落ち着き
                # のままcontent()を呼んでレース状態になりやすいため、レンダリング用には
                # 長めのタイムアウトを別途確保し、networkidleまで待つ。
                render_timeout_ms = max(self.page_timeout, 15.0) * 1000
                page.goto(url, timeout=render_timeout_ms, wait_until="networkidle")

                # goto完了直後でもクライアント側リダイレクト等でページが再ナビゲーション中の
                # ことがあり、その瞬間にcontent()を呼ぶと
                # "Unable to retrieve content because the page is navigating" で失敗するため、
                # 少し待って数回リトライする。
                html = ""
                last_error: Exception | None = None
                for _attempt in range(3):
                    try:
                        html = page.content()
                        last_error = None
                        break
                    except Exception as retry_error:
                        last_error = retry_error
                        page.wait_for_timeout(500)

                final_url = page.url
                browser.close()

                if last_error is not None:
                    raise last_error

                return html, final_url
        except Exception as e:
            logger.warning("Playwrightによる取得に失敗しました(%s): %s", url, e)
            return "", ""

    # ------------------------------------------------------------------
    # JS遷移リンクの検知
    # ------------------------------------------------------------------

    def _extract_js_links(self, html: str) -> list[str]:
        """onclick="location.href='...'" やインラインscript内のwindow.location代入からリンクを拾う。"""
        return self._JS_LOCATION_RE.findall(html)

    # ------------------------------------------------------------------
    # 多言語ページの検知
    # ------------------------------------------------------------------

    def _is_multilang_element(self, item: Tag, menu_text: str) -> bool:
        """要素が多言語切り替え用メニューであるかを強固かつ幅広く判定する"""

        # 1. 判定用キーワード（主要言語の英語表記・日本語表記・現地語表記を網羅）
        lang_keywords = {
            # 共通・概念
            "language",
            "lang",
            "select language",
            "global",
            "multilingual",
            "言語",
            "多言語",
            "Japanese",
            "japanese",
            "japan",
            "jp",
            "ja",
            "English",
            "english",
            "eng",
            "en",
            # 中国語（繁体・簡体・各種表記）
            "繁体",
            "簡体",
            "chinese",
            "中文",
            "中国語",
            "簡体字",
            "繁体字",
            "zh",
            # 韓国語
            "한국어",
            "korean",
            "ko",
            "韓国語",
            # 東南アジア諸国
            "tiếng việt",
            "vietnamese",
            "vi",
            "thai",
            "th",
            "bahasa",
            "indonesian",
            "id",
            "myanmar",
            "my",
            # ヨーロッパ・その他主要言語
            "español",
            "spanish",
            "es",
            "français",
            "french",
            "fr",
            "deutsch",
            "german",
            "de",
            "italiano",
            "italian",
            "it",
            "português",
            "portuguese",
            "pt",
            "русский",
            "russian",
            "ru",
        }

        # 2. URLパス判定用の「2文字/3文字の言語コード」判定用正規表現
        # 例: /en/, /ko/, /zh-cn/, /de/ などを安全にキャッチする（他の単語の巻き込みを防ぐ）
        # ※ 2026年現在の主要なWeb標準（ISO 639-1）に基づく2文字コードおよび拡張表記に対応
        LANG_CODE_PATTERN = re.compile(r"^(en|ja|ko|zh|es|fr|de|it|pt|ru|vi|th|id|ms|my|tl|hi|ar)(-\w+)?$")

        # --- A. テキスト（メニュー名）による判定 ---
        # 2文字前後の短いラテン文字コード(en, jp, id, my 等)は英単語の一部と偶然一致しやすい
        # (例: "Guide"に"id"が含まれる等)ため、完全一致のみを許可する。
        # それ以外の十分に長いキーワード(language, 日本語 等)は部分一致でも誤検知しにくいので
        # 従来通り部分一致を許可する。
        short_latin_codes = {kw for kw in lang_keywords if len(kw) <= 3 and kw.isascii()}
        long_keywords = lang_keywords - short_latin_codes

        menu_text_lower = menu_text.lower().strip()
        if menu_text_lower in short_latin_codes:
            return True
        if any(lk in menu_text_lower for lk in long_keywords):
            return True

        # --- B. 画像（imgタグ）のalt属性・src属性による判定 ---
        img_tags = item.find_all("img")
        if item.name == "img":
            img_tags.append(item)

        for img in img_tags:
            alt_text = img.get("alt", "").strip().lower()
            if alt_text in short_latin_codes or any(lk in alt_text for lk in long_keywords):
                return True

            # src全体(ドメイン名込みのURL)に対する部分一致だと、日本の.co.jpドメインのように
            # 短いコード("jp"等)がドメイン名に必ず含まれてしまうサイトで、ロゴ画像等の
            # 無関係な画像まで軒並み「多言語」と誤判定してしまう。
            # (例: https://example.co.jp/img/logo.svg は "jp" を含むだけで誤検知していた)
            # そのため、判定対象はファイル名部分のみに限定する。
            src = str(img.get("src", "")).lower()
            src_filename = src.rsplit("/", 1)[-1].split("?")[0]
            src_stem = re.sub(r"\.[a-z0-9]+$", "", src_filename)
            if src_stem in short_latin_codes or any(lk in src_stem for lk in long_keywords):
                return True

        # --- C. リンクのURL（href属性）による判定 【超強化】 ---
        # 1. item が Tag かどうか、および item 自身の href を安全に取得
        href: str | list[str] | None = None
        if isinstance(item, Tag):
            href = item.get("href")

            # 2. item 自身に href がない場合、子要素の <a> から取得
            if not href:
                a_tag = item.find("a")
                if isinstance(a_tag, Tag):
                    href = a_tag.get("href")

        # 3. href が存在し、かつ通常の文字列（str）である場合のみ処理を進める
        if href and isinstance(href, str):
            href_lower = href.lower()
            path = urlparse(href_lower).path
            path_segments = [seg for seg in path.split("/") if seg]

            for segment in path_segments:
                # ① キーワードリスト（english, korean など）と完全一致するか
                if segment in lang_keywords:
                    return True
                # ② URLが「/en/」「/ko/」「/zh-tw/」のような言語コード形式になっているか
                if LANG_CODE_PATTERN.match(segment):
                    return True

            # パラメータ形式のURL対策（例: ?lang=en, ?language=korean）
            if any(p in href_lower for p in ["lang=", "language=", "locale="]):
                return True

        return False

    def _detect_multilang_switcher(self, soup: BeautifulSoup) -> bool:
        """多言語切り替え機能の有無を、ページ全体から検知する。"""
        # 1. hreflang属性は最も確実なシグナル
        if soup.find(attrs={"hreflang": True}):
            return True

        # 2. Google翻訳ウィジェットの検知
        if soup.find(id="google_translate_element") or soup.find(class_=re.compile(r"goog-te", re.I)):
            return True

        # 3. class/id が言語切り替えらしいコンテナをページ全体から探索
        candidate_containers: list[Tag] = []
        candidate_containers.extend(soup.find_all(["div", "ul", "nav", "li", "span"], class_=self._MULTILANG_CONTAINER_RE))
        candidate_containers.extend(soup.find_all(["div", "ul", "nav", "li", "span"], id=self._MULTILANG_CONTAINER_RE))

        for container in candidate_containers:
            links: list[Tag] = list(container.find_all("a"))
            if container.name == "a":
                links = [container] + links

            for link in links:
                if not hasattr(link, "get_text"):
                    continue
                text = self._clean_menu_text(link.get_text(strip=True))
                if self._is_multilang_element(link, text):
                    return True

        # 4. ヘッダー領域の探索（フォールバック）
        header_candidates: list[Tag] = [h for h in soup.find_all("header") if isinstance(h, Tag)]

        # 【重要】 id="header" や class="header" を持つ div も確実に対象に含める
        header_candidates.extend(soup.find_all("div", id=re.compile(r"^header$", re.I)))
        header_candidates.extend(soup.find_all("div", class_=re.compile(r"^header$", re.I)))

        # 既存の正規表現による候補も追加
        header_candidates.extend(soup.find_all(["div", "section"], id=self._HEADER_LIKE_RE))
        header_candidates.extend(soup.find_all(["div", "section"], class_=self._HEADER_LIKE_RE))

        seen_header_ids: set[int] = set()
        for header in header_candidates:
            if id(header) in seen_header_ids:
                continue
            seen_header_ids.add(id(header))

            lang_link_count = 0
            for link in header.find_all("a", href=True):
                if not hasattr(link, "get_text"):
                    continue
                text = self._clean_menu_text(link.get_text(strip=True))

                if self._is_multilang_element(link, text):
                    lang_link_count += 1

                    # 決定的なキーワードがあれば1つでも即座にTrue
                    text_lower = text.lower()
                    strong_keywords = {"language", "lang", "select language", "global", "multilingual", "言語", "多言語"}
                    if any(sk in text_lower for sk in strong_keywords):
                        return True

            # このHTMLのように「English」「Japanese」などの言語リンクがヘッダー内に2つ以上あれば検知
            if lang_link_count >= 2:
                return True

        # 5. <select>による言語切り替えドロップダウン
        for select in soup.find_all("select"):
            if not isinstance(select, Tag):
                continue
            lang_option_count = 0
            for option in select.find_all("option"):
                if not hasattr(option, "get_text"):
                    continue
                text = self._clean_menu_text(option.get_text(strip=True))
                if self._is_multilang_element(option, text):
                    lang_option_count += 1
            if lang_option_count >= 2:
                return True

        return False

    # ------------------------------------------------------------------
    # フォーム項目抽出
    # ------------------------------------------------------------------

    def _remove_required_marks(self, text: str) -> str:
        text = self._REQUIRED_MARK_RE.sub("", text)
        text = self._EMPTY_PARENS_RE.sub("", text)
        return re.sub(r"^[\s\xa0\n\r]+|[\s\xa0\n\r]+$", "", text)

    def _get_label_for_input(self, inp: Tag, form: Tag, soup: BeautifulSoup) -> str:
        """input要素に対応する厳格なラベル（W3C標準仕様）を解決する。"""
        # 1. aria-labelledby
        labelledby = inp.get("aria-labelledby")
        if labelledby:
            target = soup.find(id=str(labelledby))
            if target and isinstance(target, Tag):
                txt = target.get_text(strip=True)
                if txt:
                    return txt

        # 2. aria-label
        aria_label = inp.get("aria-label")
        if aria_label:
            txt = str(aria_label).strip()
            if txt:
                return txt

        # 3. <label for="id">
        input_id = inp.get("id")
        if input_id:
            label_tag = form.find("label", attrs={"for": str(input_id)})
            if label_tag and isinstance(label_tag, Tag):
                txt = label_tag.get_text(strip=True)
                if txt:
                    return txt

        # 4. <label>入力欄</label> のように、labelタグ自身に内包されている場合
        parent_label = inp.find_parent("label")
        if parent_label and isinstance(parent_label, Tag):
            txt = parent_label.get_text(strip=True)
            if txt:
                return txt

        return ""

    def _find_form_containers(self, soup: BeautifulSoup) -> list[Tag]:
        """<form>タグや疑似フォームを探すが、検索窓（Search）関連は最初から完全に除外する。"""
        raw_containers: list[Tag] = list(soup.find_all("form"))
        raw_containers.extend(soup.find_all(attrs={"role": "form"}))
        raw_containers.extend(soup.find_all(attrs={"data-form": True}))

        if not raw_containers:
            for candidate in soup.find_all("div", class_=self._FORM_LIKE_CLASS_RE):
                if candidate.find(["input", "textarea", "select"]):
                    raw_containers.append(candidate)

        seen_ids = set()
        unique_containers = []

        # 検索窓キーワード定義（ID、クラス名、アクション、テキストにこれらがあれば除外）
        SEARCH_KEYWORDS = ["search", "keyword", "検索", "kensaku"]

        for c in raw_containers:
            if id(c) in seen_ids:
                continue

            # 1. 各属性のチェック
            c_id = str(c.get("id", "")).lower()
            class_attr = c.get("class")
            c_class = ("".join([str(x) for x in class_attr]) if isinstance(class_attr, list) else str(class_attr or "")).lower()
            c_action = str(c.get("action", "")).lower()
            c_name = str(c.get("name", "")).lower()

            if any(k in c_id or k in c_class or k in c_action or k in c_name for k in SEARCH_KEYWORDS):
                continue

            # 2. フォーム内の入力欄自体が検索用１個だけかどうかのチェック
            inputs = c.find_all(["input", "textarea", "select"])
            valid_inputs = []
            for inp in inputs:
                t_val = inp.get("type", "text")
                itype = "".join([str(x) for x in t_val]).lower().strip() if isinstance(t_val, list) else str(t_val).lower().strip()
                if itype not in ["hidden", "submit", "button", "image", "reset"]:
                    valid_inputs.append(inp)

            # 入力欄が1つしかなく、その名前やプレースホルダーが検索用の場合は除外
            if len(valid_inputs) == 1:
                inp = valid_inputs[0]
                inp_name = str(inp.get("name", "")).lower()
                inp_id = str(inp.get("id", "")).lower()
                inp_placeholder = str(inp.get("placeholder", "")).lower()
                if any(k in inp_name or k in inp_id or k in inp_placeholder for k in SEARCH_KEYWORDS):
                    continue

            # 全てのチェックをクリアした本命フォームのみ残す
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
        """フォーム内の入力項目ラベルを抽出する。"""
        # URL自体がメルマガ用のページなら、フォーム抽出処理そのものをさせない
        if "magazine" in base_url.lower():
            logger.info("URLに 'magazine' が含まれるためフォーム抽出をスキップします: %s", base_url)
            return "", False

        fields: list[str] = []
        has_attachment = False

        soup = BeautifulSoup(html, HTML_PARSER)
        containers = self._find_form_containers(soup)

        # logger.debug を使う
        logger.debug("==================================================")
        logger.debug("[解析対象URL]: %s", base_url)
        logger.debug("==================================================")
        logger.debug("=== [DEBUG] 検索用を除外後、%d 個のフォームコンテナを検出 ===", len(containers))

        for i, form in enumerate(containers, 1):
            form_id = form.get("id", "No ID")
            form_class = form.get("class", "No Class")
            # print から logger.debug（または info）に変更
            logger.debug("\n--- [検証中の本命フォーム #%d] ID: %s | Class: %s ---", i, form_id, form_class)

            inputs = form.find_all(["input", "textarea", "select"])
            valid_inputs = []
            for inp in inputs:
                if not isinstance(inp, Tag):
                    continue
                t_val = inp.get("type", "text")
                itype = "".join([str(x) for x in t_val]).lower().strip() if isinstance(t_val, list) else str(t_val).lower().strip()
                if itype in ["hidden", "submit", "button", "image", "reset"]:
                    continue
                valid_inputs.append(inp)

            form_fields: list[str] = []

            # ★ 入力欄ごとにループを回し、テキストを抽出した「後」でログを吐く
            for inp in valid_inputs:
                if inp.name == "input" and str(inp.get("type", "")).lower() == "file":
                    has_attachment = True

                resolved_text = ""

                # 1. 厳格な仕様に基づくラベル（id/for, aria）
                txt_a = self._get_label_for_input(inp, form, soup)
                txt_a = self._remove_required_marks(txt_a)
                if txt_a and len(txt_a) < 50 and any(c for c in txt_a if ord(c) > 0x7F):
                    resolved_text = txt_a

                # 2. 周辺のHTML構造から探索（dl/dt/dd, table/tr/th, 兄弟要素）
                if not resolved_text:
                    for parent in inp.parents:
                        if parent is form or not isinstance(parent, Tag):
                            break

                        # dl/dt/dd 構造
                        if parent.name == "dd":
                            prev_dts = parent.find_previous_siblings("dt")
                            if prev_dts:
                                resolved_text = self._remove_required_marks(prev_dts[0].get_text(strip=True))
                                break

                        # table/tr/th 構造
                        if parent.name == "td":
                            prev_ths = parent.find_previous_siblings("th")
                            if prev_ths:
                                resolved_text = self._remove_required_marks(prev_ths[0].get_text(strip=True))
                                break

                        # 直前の兄弟要素
                        siblings = parent.find_previous_siblings(["div", "span", "label", "dt", "th"])
                        # 1. siblings[0] が存在し、かつ Tag インスタンスであることを確認
                        if siblings and isinstance(siblings[0], Tag):
                            # 2. Tag 型であることが保証されたため、安全に .find() が呼べる
                            if not siblings[0].find(["input", "textarea", "select"]):
                                t = self._remove_required_marks(siblings[0].get_text(strip=True))
                                if t and len(t) < 50 and any(c for c in t if ord(c) > 0x7F):
                                    resolved_text = t
                                    break

                # 3. 最終フォールバック（属性値）
                if not resolved_text:
                    for attr in ("placeholder", "aria-label", "title", "name"):
                        val = inp.get(attr)
                        if val:
                            t = self._remove_required_marks(str(val).strip())
                            if t and len(t) < 50:
                                resolved_text = t
                                break

                # ここなら resolved_text の抽出が終わっているので安全に出力できます
                logger.debug("    入力欄 [name=%s] -> 抽出結果: '%s'", inp.get("name"), resolved_text)

                if resolved_text and not any(k in resolved_text for k in ["送信", "リセット", "確認"]):
                    if resolved_text not in form_fields:
                        form_fields.append(resolved_text)

            logger.debug("  => フォーム #%d から抽出された項目: %s", i, form_fields)

            for f in form_fields:
                if f not in fields:
                    fields.append(f)

        return "\n".join(fields), has_attachment

    # ネットワーク機器(社内プロキシ・ファイアウォール等のSSLインスペクション機能)が
    # 実際のサイトの代わりに返してくる警告/ブロックページの検知用シグネチャ。
    # (キーワードリスト, 該当時にM列へ出力する理由文) のタプルで管理する。
    _NETWORK_BLOCK_SIGNATURES: list[tuple[list[str], str]] = [
        (
            ["fortinet webfilter"],
            "Fortinet Webfilter（ネットワーク側のセキュリティ機器）によりアクセスがブロックされたため、"
            "実際のサイト内容を取得できませんでした。別ネットワークからの再調査、または手動確認をお願いします。",
        ),
        (
            ["this connection is invalid", "ssl certificate"],
            "SSL証明書エラー（期限切れ等）によりセキュリティ機器がアクセスをブロックしたため、"
            "実際のサイト内容を取得できませんでした。別ネットワークからの再調査、または手動確認をお願いします。",
        ),
    ]

    def _detect_delayed_cross_domain_redirect(self, html: str, current_url: str, base_domain_clean: str) -> str:
        """meta refreshやJavaScriptのsetTimeoutによる「数秒後に自動的に別ドメインへ
        リダイレクトする」形式の移転案内ページを検知する。

        静的HTML取得(httpx)だけではmeta refreshやJavaScriptによる遷移は実行されず、
        このページの内容がそのまま(トップページとして)解析されてしまう。しかし実際には
        既に別ドメインへ移転済みであることが多いため、その場合はリダイレクト先の絶対URLを
        返し、呼び出し側で「調査結果:×」「不可の理由:すでにリニューアル済のため」
        「備考:移転先URL」として扱えるようにする。

        検知できない場合や、リダイレクト先が同一ドメイン内(https化・パス変更等)の
        場合は空文字を返す。
        """
        if not html:
            return ""

        target_url = ""

        # 1. <meta http-equiv="refresh" content="5;url=https://...">形式
        meta_match = re.search(
            r'<meta[^>]+http-equiv=["\']?refresh["\']?[^>]*content=["\']?[^"\'>]*url=([^"\'>\s]+)',
            html,
            re.I,
        )
        if meta_match:
            target_url = meta_match.group(1).strip().rstrip("'\"")

        # 2. JavaScriptのsetTimeoutによるlocation遷移
        #    例: setTimeout(function(){ location.href = "https://..."; }, 5000);
        if not target_url:
            js_match = re.search(
                r"setTimeout\s*\([^;]*?(?:location(?:\.href)?|window\.location(?:\.href)?)\s*"
                r"(?:=|\.replace\(|\.assign\()\s*[\"']([^\"']+)[\"']",
                html,
                re.I | re.S,
            )
            if js_match:
                target_url = js_match.group(1).strip()

        if not target_url:
            return ""

        # 相対URLの場合は絶対URLに変換したうえで、ドメインが実際に異なる場合のみ「移転」とみなす
        # (同一ドメイン内でのhttps化・パス変更等は対象外)
        absolute_target = urljoin(current_url, target_url)
        target_domain_clean = self._get_clean_domain(absolute_target)

        if target_domain_clean and target_domain_clean != base_domain_clean:
            return absolute_target

        return ""

    def _classify_connection_error(self, error: Exception | None) -> str:
        """初回接続そのものが例外で失敗した場合に、その原因がSSL証明書関連かどうかを分類する。

        Fortinet等のネットワーク機器がSSLインスペクションでコネクションを遮断する場合、
        ブラウザでは「この接続ではプライバシーが保護されません」という警告画面が先に出て、
        ユーザーが「詳細を表示」→「サイトに移動」と手動操作して初めてFortinetの警告ページの
        HTMLが見られる、という2段階になっていることがある。この場合、httpx(verify=False)側は
        ブラウザのような「詳細を表示」操作を模倣できないわけではないが、証明書の形式自体が
        壊れている等の理由でTLSハンドシェイクの時点で例外が飛び、HTML本文を一切受け取れない
        ことがある。その場合 _detect_network_block_page() ではHTML本文を見られないため検知
        できないので、代わりに例外メッセージそのものから証明書関連のエラーらしさを判定する。
        該当すればM列にそのまま出力できる理由文を返し、無関係な接続エラー
        (タイムアウト・DNS失敗等)の場合は空文字を返して従来の汎用メッセージに委ねる。
        """
        if error is None:
            return ""

        err_text = str(error).lower()
        ssl_error_keywords = [
            "certificate",
            "ssl",
            "self signed certificate",
            "self-signed certificate",
            "certificate_verify_failed",
            "certificate has expired",
            "certificate verify failed",
            "unable to get local issuer certificate",
            "handshake failure",
            "tlsv1_alert",
        ]
        if any(k in err_text for k in ssl_error_keywords):
            return (
                "SSL証明書関連のエラー（期限切れ・不正な証明書等）により接続できませんでした。"
                "Fortinet等のネットワーク機器によるSSLインスペクション/ブロックの可能性もあるため、"
                "別ネットワークからの再調査、または手動確認をお願いします。"
            )
        return ""

    def _detect_network_block_page(self, html: str) -> str:
        """取得したHTMLが、実際のサイトではなくFortinet等のネットワークセキュリティ機器が
        返す警告/ブロックページである可能性を検知する。

        該当する場合は、M列（不可の理由）にそのまま出力できる具体的な理由文字列を返す。
        該当しない場合は空文字を返す。SSL証明書切れ等が原因でこの警告ページが返された場合、
        中身を見ずに「クロールできたページ数が極端に少ない」等の別の一般的な理由で
        要確認扱いになってしまい、原因が分かりにくくなることを防ぐのが目的。
        """
        if not html:
            return ""

        html_lower = html.lower()
        for keywords, reason in self._NETWORK_BLOCK_SIGNATURES:
            if all(k in html_lower for k in keywords):
                return reason

        return ""

    # ------------------------------------------------------------------
    # メインクロール処理
    # ------------------------------------------------------------------

    @measure_time
    def crawl_and_analyze(
        self, start_url: str
    ) -> tuple[
        int | str,
        int | str,
        str,
        str,
        str,
        str,
        str,
        bool,
        bool,
        bool,
        bool,
        str,
        str,
    ]:
        """ウェブサイトを巡回し、100ページに達した時点で打ち切る。

        戻り値の最後の要素 redirect_target_url は、meta refresh/JSタイマーによる
        別ドメインへの自動リダイレクト(移転案内ページ)を検知した場合のみ非空文字列となる。
        """
        if not start_url.startswith(("http://", "https://")):
            primary_url = f"https://{start_url}"
            fallback_url = f"http://{start_url}"
        else:
            primary_url = start_url
            fallback_url = start_url.replace("https://", "http://") if start_url.startswith("https://") else ""

        base_domain_clean = self._get_clean_domain(primary_url)
        start_time = time.time()

        visited: set[str] = set()
        queued_urls: set[str] = set()
        queue: deque[tuple[str, int]] = deque()
        is_over_100 = False

        max_depth = 0
        contact_fields = ""
        has_attachment = False
        has_login = False
        has_basic_auth = False
        has_multilang = False
        blocked_reason = ""
        redirect_target_url = ""  # 移転案内ページ(別ドメインへの自動リダイレクト)を検知した場合のリダイレクト先
        global_nav_menus: list[str] = []
        site_purpose = ""
        cms_name = ""
        combined_html_src = ""

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
                first_url = ""
                first_html = ""
                first_html_from_playwright = False
                primary_error: Exception | None = None
                fallback_error: Exception | None = None

                try:
                    response = client.get(primary_url)
                    if response.status_code == 401:
                        has_basic_auth = True
                    response.raise_for_status()
                    first_url = str(response.url)
                    first_html = self._decode_response(response)
                except Exception as e:
                    primary_error = e
                    if fallback_url:
                        try:
                            response = client.get(fallback_url)
                            if response.status_code == 401:
                                has_basic_auth = True
                            response.raise_for_status()
                            first_url = str(response.url)
                            first_html = self._decode_response(response)
                        except Exception as e2:
                            fallback_error = e2

                if not first_url and self.render_js:
                    # httpxでの初回取得が両方(https/http)とも失敗した場合、WAF/ボット対策等で
                    # httpxクライアントのみブロックされている可能性があるため、
                    # 最後の手段として実ブラウザ(Playwright)での取得を試みる。
                    rendered_html, rendered_url = self._fetch_rendered_html(primary_url)
                    if rendered_html:
                        first_url = rendered_url or primary_url
                        first_html = rendered_html
                        first_html_from_playwright = True

                if not first_url:
                    conn_block_reason = self._classify_connection_error(primary_error) or self._classify_connection_error(fallback_error)
                    return (
                        0,
                        0,
                        "",
                        "",
                        "",
                        "",
                        "",
                        False,
                        False,
                        has_basic_auth,
                        False,
                        conn_block_reason,
                        "",
                    )

                queue.append((first_url, 0))
                queued_urls.add(first_url)

                previous_url = ""

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
                            if response.status_code == 401:
                                has_basic_auth = True
                            if response.status_code != 200:
                                continue
                            current_html = self._decode_response(response)

                        if self.render_js and len(visited) == 1 and not first_html_from_playwright:
                            rendered, _rendered_url = self._fetch_rendered_html(current_url)
                            if rendered:
                                current_html = rendered

                        previous_url = current_url
                        soup = BeautifulSoup(current_html, HTML_PARSER)
                        combined_html_src += f"\n{current_html}"

                        if depth > max_depth:
                            max_depth = depth

                        # CMS判定(ページによって検知しやすさが違うため、複数ページに渡って試みる。
                        # 一度検知できれば以降のページでは上書きしない)
                        if not cms_name:
                            detected_cms = self._detect_cms(current_html)
                            if detected_cms:
                                cms_name = detected_cms

                        # 初回（トップ）ページのみナビゲーションと目的を取得
                        if len(visited) == 1:
                            # 移転案内ページ(meta refresh / JSタイマーによる別ドメインへの
                            # 自動リダイレクト)の検知。静的HTML取得ではリダイレクト自体は
                            # 実行されないため、ページ内容から検知して即座に打ち切る。
                            redirect_target_url = self._detect_delayed_cross_domain_redirect(current_html, current_url, base_domain_clean)
                            if redirect_target_url:
                                logger.info(
                                    "移転案内ページを検知したため巡回を打ち切ります: %s -> %s",
                                    start_url,
                                    redirect_target_url,
                                )
                                return (
                                    0,
                                    0,
                                    "",
                                    "",
                                    "",
                                    combined_html_src,
                                    cms_name,
                                    False,
                                    False,
                                    has_basic_auth,
                                    False,
                                    "",
                                    redirect_target_url,
                                )

                            # Fortinet等のネットワーク機器によるSSL証明書エラー/ブロックページの検知
                            detected_block_reason = self._detect_network_block_page(current_html)
                            if detected_block_reason:
                                blocked_reason = detected_block_reason
                                logger.warning(
                                    "ネットワーク機器によるブロックページを検知したため巡回を打ち切りました: %s",
                                    start_url,
                                )
                                return (
                                    0,
                                    0,
                                    "",
                                    "",
                                    "",
                                    combined_html_src,
                                    "",
                                    False,
                                    False,
                                    has_basic_auth,
                                    False,
                                    blocked_reason,
                                    "",
                                )

                            site_purpose = self._extract_purpose_and_features(current_html)

                            # 多言語切り替え機能の有無を検知
                            has_multilang = self._detect_multilang_switcher(soup)

                            # --- 1. メインナビ領域の候補をスコア判定で特定 ---
                            def find_main_nav_element(soup: BeautifulSoup) -> Tag | None:
                                nav_pattern = re.compile(
                                    r"gnav|rglnav|gmenu|global|main-?menu|navbar-?nav|header-?menu|header__nav|navigation",
                                    re.I,
                                )

                                candidates = soup.find_all(
                                    ["nav", "header", "ul", "div"],
                                    class_=nav_pattern,
                                )
                                candidates.extend(
                                    soup.find_all(
                                        ["ul", "div", "nav"],
                                        id=re.compile(r"nav|menu|glnav|gnav|gmenu|lh|header", re.I),
                                    )
                                )

                                if not candidates:
                                    candidates = soup.find_all("nav")

                                best_el = None
                                max_score = -100

                                for cand in candidates:
                                    attr_str = f"{cand.get('id', '')} {' '.join(cand.get('class', []))}".lower()

                                    if any(
                                        k in attr_str
                                        for k in [
                                            "footer",
                                            "side",
                                            "widget",
                                            "search",
                                            "drawer",
                                            "modal",
                                            "news",
                                            "main",
                                            "page",
                                            "content",
                                        ]
                                    ):
                                        continue

                                    score = 0
                                    if cand.name == "nav":
                                        score += 30
                                    elif cand.name == "header":
                                        score += 5

                                    if nav_pattern.search(attr_str):
                                        score += 50
                                    elif re.search(r"menu|nav|lh", attr_str):
                                        score += 20

                                    a_count = len(cand.find_all("a"))
                                    if 3 <= a_count <= 25:
                                        score += 30
                                    elif a_count > 30:
                                        score -= 40

                                    if score > max_score:
                                        max_score = score
                                        best_el = cand

                                return best_el if max_score >= 15 else None

                            # --- 2. リンクからテキストを抽出 ---
                            def extract_text_from_link(a_tag: Tag) -> str:
                                a_copy: BeautifulSoup = BeautifulSoup(str(a_tag), HTML_PARSER)

                                for sub in a_copy.find_all(
                                    ["span", "small", "p", "em", "i"],
                                    class_=re.compile(r"sub|subtitle|en|english|ruby", re.I),
                                ):
                                    sub.decompose()

                                raw_text = a_copy.get_text("\n", strip=True)
                                if "\n" in raw_text:
                                    raw_text = raw_text.split("\n")[0]

                                if not raw_text:
                                    img = a_tag.find("img")
                                    if img and isinstance(img, Tag):
                                        alt = img.get("alt")
                                        title = img.get("title")

                                        if isinstance(alt, list):
                                            alt = " ".join(alt)
                                        if isinstance(title, list):
                                            title = " ".join(title)

                                        raw_text = alt or title or ""

                                return self._clean_menu_text(str(raw_text))

                            # --- 3. メインナビ領域の確定 ---
                            target_area = find_main_nav_element(soup)

                            if not target_area:
                                header_el = soup.find("header") or soup.find("div", id=re.compile(r"header|lh", re.I))
                                if isinstance(header_el, Tag):
                                    candidate_nav = header_el.find(
                                        ["ul", "nav"],
                                        class_=re.compile(r"nav|menu|gnav", re.I),
                                    ) or header_el.find("nav")
                                    target_area = candidate_nav if isinstance(candidate_nav, Tag) else header_el

                            if target_area:
                                target_links: list[Tag] = []

                                if target_area.name == "ul":
                                    main_ul: Tag | None = target_area
                                else:
                                    main_ul = cast(
                                        Tag | None,
                                        (
                                            target_area.find("ul", class_=re.compile(r"nav|menu|gnav|gmenu", re.I))
                                            or target_area.find("ul", id=re.compile(r"nav|menu|gnav|gmenu", re.I))
                                            or target_area.find("ul")
                                        ),
                                    )

                                if isinstance(main_ul, Tag):
                                    lis = main_ul.find_all("li", recursive=False)

                                    if not lis:
                                        first_li_result = main_ul.find("li")

                                        if isinstance(first_li_result, Tag) and isinstance(first_li_result.parent, Tag):
                                            lis = first_li_result.parent.find_all("li", recursive=False)

                                    for li in lis:
                                        all_a = li.find_all("a")
                                        if all_a and isinstance(all_a[0], Tag):
                                            target_links.append(all_a[0])

                                else:
                                    target_links = [link for link in target_area.find_all("a") if isinstance(link, Tag)]

                                for a_tag in target_links:
                                    href = a_tag.get("href", "")

                                    if isinstance(href, list):
                                        href = href[0] if href else ""

                                    href = str(href).strip().lower()

                                    menu_text = extract_text_from_link(a_tag)

                                    if not menu_text:
                                        continue
                                    if menu_text in [
                                        "×",
                                        "閉じる",
                                        "MENU",
                                        "メニュー",
                                        "標準",
                                        "拡大",
                                        "検索",
                                        "メニューを飛ばす",
                                    ]:
                                        continue
                                    if self._is_multilang_element(a_tag, menu_text):
                                        continue

                                    if menu_text not in global_nav_menus:
                                        global_nav_menus.append(menu_text)

                        # お問い合わせ判定
                        is_contact_url = any(k.lower() in current_url.lower() for k in self.CONTACT_KEYWORDS)
                        contact_link_tag = soup.find(
                            "a",
                            string=re.compile(
                                r"問い合わせ|問合せ|相談|コンタクト|送信",
                                re.I,
                            ),
                        )
                        has_contact_text = contact_link_tag is not None

                        if (is_contact_url or has_contact_text) and not contact_fields:
                            contact_fields, has_attachment = self._extract_form_fields(
                                current_html,
                                current_url,
                                client,
                                depth=0,
                            )

                        # 内部リンク巡回
                        candidate_hrefs = [link["href"] for link in soup.find_all("a", href=True)]
                        # JS遷移(onclick="location.href='...'"等)によるリンクも対象に含める
                        candidate_hrefs.extend(self._extract_js_links(current_html))

                        for href in candidate_hrefs:
                            if self._is_valid_internal_link(current_url, href, base_domain_clean):
                                abs_href = urljoin(current_url, href)
                                norm_abs = normalize_url(abs_href)
                                parsed_abs = urlparse(norm_abs)
                                path_depth = len([p for p in parsed_abs.path.split("/") if p])
                                is_priority = any(
                                    k.lower() in norm_abs.lower()
                                    for k in (
                                        "contact",
                                        "inquiry",
                                        "otoiawase",
                                        "form",
                                    )
                                )
                                enqueue(norm_abs, path_depth, is_priority)

                        time.sleep(0.04)

                    except httpx.RequestError:
                        continue

        except Exception as e:
            logger.warning("クローラー内で予期せぬエラーが発生しました: %s", e)

        if blocked_reason:
            return (
                0,
                0,
                "",
                "",
                "",
                combined_html_src,
                "",
                False,
                False,
                has_basic_auth,
                False,
                blocked_reason,
                "",
            )

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
            combined_html_src,
            cms_name,
            has_attachment,
            has_login,
            has_basic_auth,
            has_multilang,
            blocked_reason,
            redirect_target_url,
        )
