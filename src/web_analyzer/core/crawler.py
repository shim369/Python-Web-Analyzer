import logging
import posixpath
import re
import time
import warnings
from collections import deque
from typing import cast
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag, XMLParsedAsHTMLWarning

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
    _TEMPLATE_VAR_RE = re.compile(r"\{\{.*?\}\}")
    _EMPTY_PARENS_RE = re.compile(r"[\(（]\s*[\)）]")

    # お問い合わせページ判定用キーワード(日英混在)
    CONTACT_KEYWORDS = [
        "contact",
        "inquiry",
        "otoiawase",
        "toiawase",
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
        # Canva/Webflow/Framer等のノーコードサイトビルダーも、静的HTML(httpx取得分)
        # にはほぼ実質的なコンテンツ・内部リンクが含まれず、JavaScriptによる
        # クライアントサイド描画に依存している。既知のJSフレームワークと同様、
        # Playwrightでの再レンダリングが必要な対象として扱う(estercorp.co.jpで確認)。
        "canva",
        "webflow",
        "framer",
    ]

    # CMSの自動生成抜粋(excerpt)が文の途中で切れているサインとなる省略記号。
    # 例:「岡山建設は、あなたの住居に対する理想をカタチに...」のように、
    # 意味の切れたテキストがそのまま「用途」列に入ってしまうのを防ぐために使う。
    TRUNCATION_MARKERS = ("...", "…", "・・・")

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

    # WordPress等のページネーション用パスセグメント。
    # テーマ/プラグインが「次へ」リンクを "page/3/" のような絶対パスでない
    # 素の相対パスで出力していると、urljoin()による相対解決の結果、
    # "/page/2/page/3/page/4/..." のように本来同階層であるべきページネーションが
    # 無限に入れ子になっていくURLが生成されてしまう(daito-com.co.jpで確認)。
    # 数字部分が毎回変わるため _has_repeating_path_pattern の隣接同一チェックを
    # すり抜けるが、同じキーワードが2回以上パス内に出現すること自体が
    # 明確に異常な兆候であるため、専用にチェックする。
    _PAGINATION_SEGMENT_KEYWORDS = {"page", "paged"}

    # normalize_url()でクエリ文字列を丸ごと除去すると、"detail.php?id=1"や
    # "detail.php?id=2"のような、クエリパラメータで実際に異なるコンテンツを
    # 出し分ける旧来型PHPサイト(daitokasei.com等)で、全ての記事が同一URLとして
    # 重複排除されてしまい、最初の1件しか巡回されなくなる不具合があった。
    # 一方でSNS流入計測用のutm_*等のトラッキングパラメータは、同じページを
    # 指しているのに値だけ違う無数のバリエーションを生み、放置すると逆に
    # 無限に近いURLバリエーションを生成してしまう。そのため、既知の
    # トラッキング系パラメータのみを除去し、それ以外のクエリパラメータ
    # (id, p, page_id等、コンテンツ識別に使われている可能性があるもの)は
    # 保持する方針にする。
    _TRACKING_QUERY_KEYS = {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "utm_name",
        "utm_reader",
        "fbclid",
        "gclid",
        "gclsrc",
        "dclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "yclid",
        "twclid",
        "igshid",
        "_ga",
        "_gl",
        "spm",
        # 以下はEC-CUBE等のカタログ系システムで、"category_id=bag"と
        # "category_id=bag&sort=datetime_desc&word="のように、実質は同じ
        # 一覧ページの並び替え・絞り込みキーワードの初期値(空文字/デフォルト値)
        # にすぎないパラメータが付与された「見た目だけ別のURL」を生み、
        # ページ数が水増しされる原因になっていた(earth-inc.co.jpで確認)。
        # これらは値によってコンテンツが変わりうる(例: sort=price_ascで
        # 表示順が変わる、word=xxxで絞り込み結果が変わる)ため、本来は
        # 除去すべきでないケースもあるが、空値やデフォルト値での重複発生の
        # 実害の方が大きいと判断し、除去対象に含める。
        "sort",
        "order",
        "orderby",
        "word",
    }

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

    # ファイル名の拡張子としてよく使われる文字列。ドメインらしき文字列の
    # 誤検知(例: "index.php"や"page.html"をドメインと誤判定すること)を
    # 避けるため、末尾がこれらに一致する場合はドメイン判定の対象外とする。
    _COMMON_FILE_EXTENSIONS = {
        "html",
        "htm",
        "php",
        "asp",
        "aspx",
        "jsp",
        "xml",
        "json",
        "js",
        "css",
        "jpg",
        "jpeg",
        "png",
        "gif",
        "pdf",
        "txt",
        "xhtml",
        "webp",
        "svg",
        "ico",
        "zip",
        "tar",
        "gz",
        "rar",
        "7z",
        "mp3",
        "mp4",
        "avi",
        "mov",
        "wmv",
        "flv",
        "webm",
        "wav",
        "xlsx",
        "xls",
        "docx",
        "doc",
        "pptx",
        "ppt",
        "csv",
    }

    def _looks_like_bare_domain_segment(self, segment: str) -> bool:
        """パスの1セグメントが、スキーム抜けの絶対URL誤爆によるドメイン名らしいかを判定する。

        HTML側に"easnet.sakura.ne.jp/wp/hh"のように、本来別ドメインへの絶対URL
        であるべきリンクがスキーム(https://等)を欠いたまま書かれていることがある。
        この場合urljoin()は仕様通り「相対パス」として解決してしまい、
        "https://eas-c.jp/am/easnet.sakura.ne.jp/wp/hh"のような、本来存在しない
        URLがパスの途中にホスト名を含む形で生成されてしまう(eas-c.jpで確認)。
        こうしたセグメントは「ドット区切りが2つ以上あり、末尾が既知のファイル
        拡張子でも純粋な数字でもない、ラベルらしい文字列で終わる」という
        ドメイン名らしい形をしているため、これを検知して内部リンクとしての
        巡回対象から除外する。
        """
        if segment.count(".") < 2:
            return False
        last_label = segment.rsplit(".", 1)[-1].lower()
        if last_label in self._COMMON_FILE_EXTENSIONS:
            return False
        return bool(re.fullmatch(r"[a-z]{2,24}", last_label))

    # "blog"や"topics"のような、CMSで一般的に使われる正当なコンテンツ
    # ディレクトリ名。記事数が多いサイトではこれらのディレクトリだけで
    # 全体の70〜90%に達することも珍しくなく(eigyokaigi.comの"/blog/"で72%、
    # ebuno.comの"/topics/"で87%を確認)、比率のしきい値をどれだけ上げても
    # "/monster/"のような本物の別システムと安定して区別できない。
    # ディレクトリ名自体が明確に「記事・お知らせの置き場」だと分かる場合は、
    # 比率に関わらず注記の対象外とする。
    _LIKELY_CONTENT_DIR_NAMES = {
        "blog",
        "blogs",
        "topics",
        "news",
        "info",
        "information",
        "column",
        "columns",
        "article",
        "articles",
        "diary",
        "posts",
        "press",
        "release",
        "releases",
        "notice",
        "notices",
        # 個別記事・固定ページをまとめて格納する、CMSでよく使われる汎用フォルダ名。
        # "pages"(固定ページ一覧)、"archives"(年月別記事アーカイブ)、
        # "contents"(コンテンツ一覧)も、"blog"/"topics"と同様に単に記事・
        # ページ数が多いだけの正当なディレクトリであるケースが大半なので対象外とする
        # (eikoh-eng.co.jp等で確認)。
        "pages",
        "archives",
        "contents",
    }

    def _detect_subsystem_note(self, canonical_visited: set[str]) -> str:
        """サイト全体のごく一部のディレクトリ配下に、別システムらしきものが
        同居していて、そこだけでページ数の大半を占めていないかを調べる。

        "/monster/"配下のEC-CUBEショップのように、コーポレートサイト本体とは
        毛色の違う別システムが同じドメインの1ディレクトリに間借りしている
        ケースでは、そこの商品ページ・カテゴリページを律儀に数えてしまうと、
        「実際には数年前に作られたまま放置された旧システム」の分量に
        ページ数・階層数が引きずられ、本体サイトの実態を見誤らせる
        (earth-inc.co.jpで確認)。ただし「どのディレクトリが別システムか」を
        機械的に断定するのはCMSやサイト構成の多様性を考えると危険なので、
        ここでは判定・除外はせず、「先頭ディレクトリの1つに全体の大半の
        ページが集中している」という偏りだけを検知して、人間が確認する
        きっかけとなる注記文字列を返すに留める。該当しない場合は空文字。
        """
        total = len(canonical_visited)
        # サイト全体のページ数が少ない場合、たまたま1ディレクトリに複数ページ
        # あるだけでも比率が高くなりやすく、注記を出す意味が薄いため対象外とする。
        if total < 10:
            return ""

        top_dir_counts: dict[str, int] = {}
        for url in canonical_visited:
            segments = [s for s in urlparse(url).path.split("/") if s]
            if not segments:
                continue
            top_dir = segments[0]
            top_dir_counts[top_dir] = top_dir_counts.get(top_dir, 0) + 1

        if not top_dir_counts:
            return ""

        top_dir, count = max(top_dir_counts.items(), key=lambda item: item[1])
        if top_dir.lower() in self._LIKELY_CONTENT_DIR_NAMES:
            return ""
        ratio = count / total
        # 件数・比率のどちらも一定以上の場合のみ注記する。
        if count >= 10 and ratio >= 0.8:
            # "旧オンラインショップ等"のように具体例を決め打ちすると、実際には
            # 施設検索・会員機能等のCURRENTな機能であるケース(ebr-med.or.jpの
            # "/institutes/"施設検索機能等)にそぐわない。ページ数・階層数の
            # 判定に影響しうる、という事実だけを伝える中立的な表現にする。
            return (
                f"「/{top_dir}/」配下に{count}件({ratio:.0%})のページが集中しています。"
                "コーポレートサイト本体とは技術的に異なる機能やシステム"
                "(検索・会員機能、旧サイトの残存等)が同居している可能性があります。"
                "ページ数・階層数の判定に影響している場合があるため、"
                "内容を確認することをおすすめします。"
            )
        return ""

    def _is_valid_internal_link(self, current_url: str, href: str, base_domain: str) -> bool:
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            return False
        abs_url = urljoin(current_url, href)
        parsed_abs = urlparse(abs_url)
        abs_domain = parsed_abs.netloc.replace("www.", "").split(":")[0]

        # ここまでの"ドットが2つ以上"という判定は、easnet.sakura.ne.jpのような
        # 他ドメインの誤埋め込みは捕捉できるが、eas-c.jpのように調査対象サイト
        # 自身のドメイン名(ドットが1つしかない一般的な2ラベル構成)が
        # "eas-c.jp/team"のようにスキーム抜けで自己参照的に埋め込まれるケースを
        # 見逃していた(eas-c.jpで"https://eas-c.jp/eas-c.jp/team"のような
        # パターンが多数発生し、依然としてページ数100件到達の主因になっていた)。
        # パスセグメントが調査対象ドメイン自身と完全一致する場合は、ドット数に
        # 関わらず確実にスキーム抜けの自己参照リンクとみなせるため、直接比較する。
        if any(seg.lower() == base_domain.lower() for seg in parsed_abs.path.split("/") if seg):
            return False

        if any(self._looks_like_bare_domain_segment(seg) for seg in parsed_abs.path.split("/") if seg):
            return False

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
                ".xlsx",
                ".xls",
                ".docx",
                ".doc",
                ".pptx",
                ".ppt",
                ".csv",  # Office文書(添付資料等。HTMLページではなくダウンロード対象のため対象外)
            ]
        ):
            return False

        # wp-login.php(WordPressのログイン画面)は一般訪問者にも200 OKを返すため、
        # 通常のコンテンツページと見分けがつかず、そのまま巡回対象に含めると
        # サイトの実コンテンツではない管理用UIがページ数にカウントされてしまう
        # (eigyokaigi.comで確認)。xmlrpc.php・wp-admin配下も同様の理由で除外する。
        # (これはページ数カウントの話であり、ログインフォームの有無自体の検知は
        # 別途 _has_password_login_form() が担っている)
        path_lower_for_admin_check = parsed_abs.path.lower()
        if any(marker in path_lower_for_admin_check for marker in ("/wp-login.php", "/xmlrpc.php", "/wp-admin/")):
            return False

        # RSS/Atomフィード等のXMLリソースはHTMLページではなく、通常のサイト構成・調査結果としては
        # 意味を持たない(かつlxmlでHTMLとしてパースするとXMLParsedAsHTMLWarningが出る)ため対象外とする
        path_lower = parsed_abs.path.lower()
        if path_lower.endswith((".xml", ".rss", ".atom")) or path_lower.rstrip("/").endswith("/feed"):
            return False
        if re.search(r"[?&]feed=", abs_url.lower()):
            return False

        if not self._is_depth_worthy_path(parsed_abs.path):
            return False

        if self._has_nested_pagination_trap(parsed_abs.path):
            return False

        return abs_domain == base_domain

    def _has_password_login_form(self, html: str) -> bool:
        """パスワード入力欄を持つ、訪問者向けのログインフォームが存在するか判定する。

        CMSの管理者用ログイン画面(wp-login.php等)を個別に列挙して除外する方式は、
        対象CMSが増えるたびにメンテナンスが必要になり、かつ列挙し漏れたCMSでは
        効果が出ないため採用しない。代わりに、大半のCMS管理者ログイン画面が
        「サイト共通のヘッダー/グローバルナビを持たない単独ページ」として
        表示されるという構造的な特徴を利用し、CMSの種類を問わず判定する。

        訪問者向けの会員ログイン・マイページは通常のページテンプレート内に
        組み込まれているため、サイト共通のヘッダー/グローバルナビを伴って
        表示される点で区別できる。
        """
        soup = BeautifulSoup(html or "", HTML_PARSER)
        body = soup.body or soup

        has_password_field = bool(
            body.find("input", attrs={"type": re.compile(r"^password$", re.I)}) or body.find(attrs={"autocomplete": re.compile(r"current-password|new-password", re.I)})
        )
        if not has_password_field:
            return False

        has_site_chrome = bool(body.find("header") or body.find(class_=self._HEADER_LIKE_RE) or body.find(id=self._HEADER_LIKE_RE))
        return has_site_chrome

    def _detect_cms(self, html: str, url: str = "") -> str:
        html = html.lower()

        # "assets_c" はMovable Type固有の自動生成アセット格納ディレクトリ(記事内画像・
        # サムネイル等)であり、HTML本文にmt-content/mt-static等の目印が出ない
        # テンプレートでも、URLパスにこのディレクトリ名が含まれていればMovable Type製
        # サイトだと判定できる(danshinen.orgで確認)。
        if url and "/assets_c/" in url.lower():
            return "Movable Type"

        # DNN(DotNetNuke)製サイト(dohkenkyo.or.jp等)で"WP"に誤判定される事例があった。
        # DNNサイトは<base>タグでルート相対パスを解決させる作りが多く、
        # "Portals/0/images/..."のように先頭のスラッシュを省略したHTMLに
        # なっていることがある。先頭スラッシュを必須にした正規表現では
        # このパターンを拾えず判定が効かなかったため、スラッシュの有無に
        # 依存しない形に緩める。また埋め込みウィジェットや外部スクリプト
        # 経由でHTML中に偶然"wp-content"/"wp-includes"という文字列を含む
        # ことがあり、下の緩いキーワード判定(cms_patterns)がそれを拾って
        # WordPressだと誤検知してしまうため、これらのDNN特有の強いシグナル
        # は緩い判定より先に確認する。
        if re.search(r"(?:^|[\"'/])desktopmodules/", html) or re.search(r"(?:^|[\"'/])portals/\d+/", html):
            return "DNN (DotNetNuke)"

        cms_patterns = {
            # WP等の緩いキーワード判定より前に、DNN特有のシグナルが無くても
            # 拾えるようDNNを最優先でチェックする(念のための二重対策)。
            "DNN (DotNetNuke)": [
                "dotnetnuke",
                "dnn.js",
                "dnn_ctr",
            ],
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

    def _extract_wix_global_nav(self, soup: BeautifulSoup) -> list[str]:
        """Wix製サイト専用のグローバルナビ抽出。
        Wixはビルドごとにclass名がハッシュ化されるため、class名ではなく
        Wixが内部的に付与する安定した属性(data-testid, id="SITE_HEADER..." 等)
        を手がかりにする。
        """
        menus: list[str] = []
        seen: set[str] = set()

        def add(text: str) -> None:
            cleaned = self._clean_menu_text(text)
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                menus.append(cleaned)

        # --- パターンA: <wix-dropdown-menu>(ドロップダウン式) ---
        for dropdown in soup.find_all("wix-dropdown-menu"):
            items_ul = dropdown.find("ul", id=re.compile(r"itemsContainer$")) or dropdown.find("ul")
            if not isinstance(items_ul, Tag):
                continue
            for li in items_ul.find_all("li", recursive=False):
                if li.get("data-index") == "__more__":
                    continue
                a_tags = li.find_all("a", attrs={"data-testid": "linkElement"})
                if a_tags:
                    add(a_tags[0].get_text(" ", strip=True))

        if menus:
            return menus

        # --- パターンB: ul/liを使わないボタン羅列型ヘッダー(mesh-container) ---
        header_area = soup.find(id=re.compile(r"^SITE_HEADER")) or soup.find(attrs={"data-testid": "mesh-container-content"})
        if isinstance(header_area, Tag):
            for a in header_area.find_all("a", attrs={"data-testid": "linkElement"}):
                if a.find_parent(attrs={"data-testid": re.compile(r"search-box|language-selector")}):
                    continue
                if a.find_parent("h1"):  # サイトロゴ/タイトルは除外
                    continue
                add(a.get_text(" ", strip=True))

        return menus

    def _detect_js_framework(self, html: str) -> str:
        """SPA/CSRフレームワークの利用有無を検知する(フォーム取得漏れの原因切り分け用)。

        既知フレームワークの目印文字列に加えて、<body>の可視テキストがほぼ空で
        外部スクリプトの読み込みだけがある「空のシェルHTML」も検知対象にする。
        Vue CLI等が生成する最小限のindex.html(<div id="app"></div>だけ)は、
        vue.js/data-v-のような目印文字列を実際には一切含まないことがあり、
        キーワード一致だけでは検知漏れ(=Playwright再レンダリングが発動しない)
        になってしまうため。
        """
        html_lower = html.lower()
        for marker in self.JS_FRAMEWORK_MARKERS:
            if marker in html_lower:
                return marker

        if self._looks_like_empty_js_shell(html):
            return "empty-shell(推定SPA)"

        return ""

    def _looks_like_empty_js_shell(self, html: str) -> bool:
        """<body>の可視テキストがほぼ空で、外部スクリプトの読み込みだけがある
        「空のシェルHTML」かどうかを判定する。

        既知フレームワークの目印文字列を含まないSPAのindex.html(初期状態)を
        補足的に検知するためのヒューリスティック。可視テキストが極端に短く
        (目安80文字未満)、かつナビゲーションリンクもほとんど無く(3個以下)、
        外部スクリプト(<script src>)が存在する場合にTrueを返す。
        リンク数も条件に含めるのは、単に文章が短いだけの通常の静的ページ
        (ナビゲーションメニューやフッターのリンクは持っている)を誤って
        「空のシェル」と判定しないようにするため。
        """
        try:
            soup = BeautifulSoup(html, HTML_PARSER)
        except Exception:
            return False

        body = soup.body
        if body is None:
            return False

        visible_text = body.get_text(strip=True)
        has_external_script = bool(soup.select("script[src]"))
        link_count = len(body.find_all("a"))

        return len(visible_text) < 80 and link_count <= 3 and has_external_script

    def _is_truncated_text(self, text: str) -> bool:
        """CMSが自動生成した抜粋等が、省略記号で途中打ち切りになっているか判定する。

        「...」や「…」が含まれるテキストは、文の途中でぶつ切りになっている
        可能性が高く、そのまま「用途」列に出すには不適切なため、この判定に
        引っかかった候補は使わず、次の優先順位の候補(title > h1)を見に行く。
        """
        return any(marker in text for marker in self.TRUNCATION_MARKERS)

    def _extract_purpose_and_features(self, html: str) -> str:
        """HTMLから優先順位(description > title > h1)に従って文字列をそのまま抽出する。

        ただし、省略記号(...や…)を含み文の途中で切れていると判断できる候補は
        スキップし、次の優先順位の候補を見に行く。
        """
        if not html:
            return ""

        soup = BeautifulSoup(html, HTML_PARSER)

        desc_tag = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
        if desc_tag and isinstance(desc_tag, Tag):
            content_attr = desc_tag.get("content", "")
            desc_text = ("".join(content_attr) if isinstance(content_attr, list) else str(content_attr)).strip()
            if desc_text and not self._is_truncated_text(desc_text):
                return desc_text

        if soup.title and soup.title.string:
            title_text = soup.title.string.strip()
            if title_text and not self._is_truncated_text(title_text):
                return title_text

        h1_tag = soup.find("h1")
        if h1_tag and isinstance(h1_tag, Tag):
            h1_text = h1_tag.get_text(strip=True)
            if h1_text and not self._is_truncated_text(h1_text):
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

    def _clean_query(self, query: str) -> str:
        """クエリ文字列から既知のトラッキングパラメータのみを除去する。

        id/p/page_id等のコンテンツ識別に使われうるパラメータは保持し、
        残ったパラメータはキー名でソートして再構成する(同じコンテンツを
        指す"?id=1&ref=twitter"と"?ref=twitter&id=1"のような、
        並び順違いだけのURLが別ページとして重複カウントされるのを防ぐため)。
        """
        if not query:
            return ""
        pairs = parse_qsl(query, keep_blank_values=True)
        filtered = [(k, v) for k, v in pairs if k.lower() not in self._TRACKING_QUERY_KEYS]
        filtered.sort()
        return urlencode(filtered)

    def _has_nested_pagination_trap(self, path: str) -> bool:
        """ページネーション用セグメント(page/paged等)が同一パス内に2回以上出現するかを判定する。

        正常なページネーションURLは "/blog/page/3/" のように該当キーワードが
        1回しか登場しない。"/page/2/page/3/" のように2回以上登場している場合は、
        相対パス解決ミス等によって生まれた疑似的な入れ子URLである可能性が高いため、
        巡回対象・階層数カウントの対象から除外する。
        """
        segments = [p.lower() for p in path.split("/") if p]
        keyword_hits = sum(1 for seg in segments if seg in self._PAGINATION_SEGMENT_KEYWORDS)
        return keyword_hits >= 2

    def _is_depth_worthy_path(self, path: str) -> bool:
        """「階層数」のカウント・巡回対象とするに値するURLパスかどうかを判定する。

        カレンダーウィジェットの日付ドリルダウン(例: /calendar/2024/08/09/10/11/12/)
        のように、短い数値セグメントが4つ以上連続するパスは、実際のサイト構成とは
        無関係に機械的に深くなっていくURLパターン(無限に近いバリエーションを持つ
        カレンダーの日送りリンク等)である可能性が高い。これをそのまま巡回・階層数
        カウントの対象にすると、無駄にクロール予算を消費するうえ、「階層数」が
        サイトの実態とかけ離れて高く表示されてしまう。

        しきい値は意図的に「3以上」ではなく「4以上」にしている。WordPress等の
        日付ベースのパーマリンク(例: /blog/2019/07/49/ = 年/月/連番の3セグメント、
        /blog/2019/07/09/ = 年/月/日の3セグメント)はごく一般的な正規の投稿URLだが、
        数値セグメントがちょうど3つ連続するため、しきい値が3のままだと巡回対象から
        誤って除外され、ページ数・階層数が過小に判定される原因になっていた
        (daisyokousan.co.jpで確認)。実際に問題となるカレンダードリルダウン
        (年/月/日/時など)は4セグメント以上に及ぶことがほとんどのため、
        しきい値を4に引き上げても本来の目的(無限トラップの回避)は損なわれない。
        """
        segments = [p for p in path.split("/") if p]
        numeric_run = 0
        for seg in segments:
            if seg.isdigit() and len(seg) <= 4:
                numeric_run += 1
                if numeric_run >= 4:
                    return False
            else:
                numeric_run = 0
        return True

    # ------------------------------------------------------------------
    # HTML取得・デコード
    # ------------------------------------------------------------------

    def _decode_response(self, response: httpx.Response) -> str:
        """レスポンスの文字コードを判定してデコードする(Shift_JIS系はcp932に正規化し、文字化けを防ぐ)。

        <meta charset>やHTTPヘッダーに文字コードの指定が無い古いサイト
        (cgi-bin形式のフォーム処理等でよく見られる)では、httpxの推測にも頼れず
        「utf-8」に決め打ちしてしまい、実際はcp932(Shift_JIS)やeuc-jpのページが
        文字化けすることがある。これを防ぐため、明示的な文字コード指定が
        見つからない場合は、複数の候補でデコードを試し、置換文字(U+FFFD、
        デコード失敗箇所の目印)の出現率が最も低いものを採用するヒューリスティックを行う。
        """
        # 1. まずHTMLの先頭部分から meta charset を安全に探す
        # asciiの代わりに latin-1 を使うと、バイト値を壊さずに文字列化して正規表現にかけられます
        raw_content_head = response.content[:2048].decode("latin-1", errors="ignore")
        meta_charset = re.search(r'charset=["\']?([a-zA-Z0-9_-]+)', raw_content_head, re.IGNORECASE)

        def _normalize_encoding(enc: str) -> str:
            enc_lower = enc.lower()
            if enc_lower in ["shift_jis", "shift-jis", "sjis", "x-sjis", "cp932"]:
                return "cp932"
            if enc_lower in ["euc-jp", "eucjp", "x-euc-jp"]:
                return "euc-jp"
            return enc

        if meta_charset:
            # metaタグで明示されている場合は、素直にそれを信頼する
            encoding = _normalize_encoding(meta_charset.group(1))
            try:
                return response.content.decode(encoding, errors="replace")
            except Exception:
                pass  # デコード自体に失敗した場合は、下の推測ロジックにフォールスルーする

        # 2. metaタグに文字コードの指定が無い(または指定されたエンコーディングで
        # デコードできなかった)場合。httpxの推測を第一候補にしつつ、日本語サイトで
        # よくあるcp932・euc-jp・utf-8も候補に含め、最も文字化けが少ないものを採用する。
        guessed = response.encoding or response.charset_encoding
        candidates: list[str] = []
        if guessed and len(guessed) > 1:
            candidates.append(_normalize_encoding(guessed))
        for enc in ("utf-8", "cp932", "euc-jp"):
            if enc not in candidates:
                candidates.append(enc)

        best_text = ""
        best_error_ratio = 1.0
        for enc in candidates:
            try:
                decoded = response.content.decode(enc, errors="replace")
            except Exception:
                continue

            if not decoded:
                continue

            error_ratio = decoded.count("\ufffd") / len(decoded)
            if error_ratio < best_error_ratio:
                best_error_ratio = error_ratio
                best_text = decoded

            # 置換文字が全く無ければ、それ以上候補を試す必要はない
            if error_ratio == 0:
                break

        if best_text:
            return best_text

        # 3. 最終手段: httpx標準の自動デコードに頼る
        try:
            return response.text
        except Exception:
            return response.content.decode("utf-8", errors="replace")

    def _fetch_rendered_html(self, url: str) -> tuple[str, str, int | None, str]:
        """Playwrightが利用可能ならレンダリング後のHTML・最終URL・HTTPステータスコード・
        (ナビゲーション自体が失敗した場合の)理由文を取得する。

        未導入時/失敗時は ("", "", None, "") を返す。ステータスコードは、取得した
        HTMLが403 Forbidden等のエラーページ本体でないかを呼び出し側で判定する
        ために必要(httpxと違いPlaywrightのgoto()は4xx/5xxでも例外を投げず、
        エラーページのHTMLをそのまま返してしまうため)。

        また、DNS解決失敗や接続拒否などでナビゲーション自体が失敗した場合、
        Chromeは内部の「chrome-error://chromewebdata/」という特殊URLに遷移し、
        page.content()はChrome自身が生成した簡易エラーページのHTMLを返してしまう。
        これを実サイトの内容として扱うと、このURLがそのまま「移転先」として
        記録される等の誤動作につながるため、ここで検知して空文字を返し、
        代わりに原因を分類した理由文を4番目の戻り値として返す。
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.warning("Playwright未インストールのためJSレンダリングをスキップします: %s", url)
            return "", "", None, ""

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)

                # リアルなブラウザのヘッダー・画面設定を網羅してボット検知を回避
                context = browser.new_context(
                    user_agent=self.headers.get("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
                    viewport={"width": 1280, "height": 800},
                    extra_http_headers={
                        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
                    },
                    ignore_https_errors=True,
                )
                page = context.new_page()

                render_timeout_ms = max(self.page_timeout, 15.0) * 1000
                status_code: int | None = None
                goto_error: Exception | None = None

                # 改善ポイント: networkidle ではなく domcontentloaded や commit で素早く受け取りを開始する
                try:
                    nav_response = page.goto(url, timeout=render_timeout_ms, wait_until="domcontentloaded")
                    if nav_response is not None:
                        status_code = nav_response.status
                    # 接続直後に少しだけレンダリングを待つ
                    page.wait_for_timeout(1500)
                except Exception as goto_err:
                    # 完全に切断される前に取れたデータがあれば続行を試みる
                    goto_error = goto_err
                    logger.debug("goto完了前に例外が発生しましたが処理を継続します: %s", goto_err)

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

                if last_error is not None and not html:
                    raise last_error

                # ナビゲーション自体が失敗している場合(chrome-error://等のブラウザ内部URLに
                # 遷移している場合)、page.content()で取れた中身はChrome自身が生成した簡易
                # エラーページに過ぎず、実サイトの内容ではないため、取得成功として扱わない。
                if final_url.startswith(("chrome-error://", "chrome://", "about:")):
                    nav_reason = self._describe_playwright_navigation_error(goto_error, final_url)
                    return "", "", None, nav_reason

                return html, final_url, status_code, ""
        except Exception as e:
            logger.warning("Playwrightによる取得に失敗しました(%s): %s", url, e)
            return "", "", None, ""

    def _describe_playwright_navigation_error(self, error: Exception | None, final_url: str) -> str:
        """Playwrightでのページ遷移そのものが失敗した場合に、例外メッセージから
        原因を分類し、M列にそのまま出力できる理由文を生成する。

        (例: DNS_PROBE_FINISHED_NXDOMAIN, ERR_CONNECTION_CLOSED, ERR_CONNECTION_REFUSED等)
        原因を特定できない場合も、最低限「取得できなかった」ことは明示する。
        """
        message = str(error).lower() if error else ""

        if "name_not_resolved" in message or "nxdomain" in message or "dns" in message:
            return "ドメイン名を解決できませんでした（DNSエラー）。ドメインが失効している、または既に閉鎖されている可能性が高いため、手動確認をお願いします。"
        if "connection_refused" in message:
            return "接続が拒否されました。サーバーが停止している、または閉鎖されている可能性が高いため、手動確認をお願いします。"
        if "connection_closed" in message or "connection_reset" in message:
            return "接続が途中で切断されました。サーバーが停止している、または閉鎖されている可能性が高いため、手動確認をお願いします。"
        if "timed_out" in message or "timeout" in message:
            return "接続がタイムアウトしました。サーバーが応答していない可能性が高いため、手動確認をお願いします。"
        if "cert" in message or "ssl" in message:
            return "SSL証明書関連のエラーにより接続できませんでした。手動確認をお願いします。"

        if final_url.startswith("chrome-error://"):
            return "ブラウザでのページ表示に失敗したため、実際のサイト内容を取得できませんでした。手動確認をお願いします。"

        return ""

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
        text = self._TEMPLATE_VAR_RE.sub("", text)
        text = self._EMPTY_PARENS_RE.sub("", text)
        return re.sub(r"^[\s\xa0\n\r]+|[\s\xa0\n\r]+$", "", text)

    def _get_clean_element_text(self, el: Tag) -> str:
        """エラー表示領域や余計なタグを除去して純粋なラベル文言のみを取得する"""
        if not isinstance(el, Tag):
            return ""

        # 元のDOMを破壊しないよう複製
        el_copy = BeautifulSoup(str(el), HTML_PARSER)

        # class名に error / note / hint などが含まれる要素を取り除いて無効化（decompose）
        for junk in el_copy.find_all(class_=re.compile(r"error-text|error|help-block|note|hint", re.I)):
            junk.decompose()

        text = el_copy.get_text(strip=True)
        return self._remove_required_marks(text)

    _HINT_ELEMENT_CLASS_RE = re.compile(r"exam|example|hint|note|annotation|caption|desc(?:ription)?", re.I)

    def _looks_like_hint_element(self, el: Tag) -> bool:
        """「入力例」「注釈」等のヒントテキストを表す要素かどうかを判定する。

        例: <span class="exam">【全角】例：〇〇株式会社</span>のような要素。
        class名にヒントらしきキーワードを含む場合、たとえテキストが非ASCII文字を
        含んでいても、本来のラベル(dt等)より優先してラベル候補にしてはいけない。
        """
        classes = " ".join(el.get("class", [])).lower()
        return bool(self._HINT_ELEMENT_CLASS_RE.search(classes))

    def _direct_child_text(self, el: Tag) -> str:
        """要素の「直接の」子テキストノードだけを連結して返す(入れ子タグの中の文字は含めない)。

        例: <p>お問い合わせ内容 <span>(必須)<br>...<textarea>...</textarea>...</span></p>
        のように、ラベル文言が兄弟要素としてではなく、入力欄と同じブロック内に
        直接のテキストとして同居しているCF7テンプレートのパターンに対応するため。
        get_text()だと入れ子のtextarea内の値まで拾ってしまう可能性があるが、
        直接の子のNavigableStringだけを見るのでその心配がない。
        """
        parts = [str(c) for c in el.contents if isinstance(c, NavigableString)]
        return "".join(parts).strip()

    def _get_label_for_input(self, inp: Tag, form: Tag, soup: BeautifulSoup) -> str:
        """input要素に対応する厳格なラベル（W3C標準仕様）を解決する。"""
        # 1. aria-labelledby
        labelledby = inp.get("aria-labelledby")
        if labelledby:
            target = soup.find(id=str(labelledby))
            if target and isinstance(target, Tag):
                # txt = target.get_text(strip=True)
                txt = self._get_clean_element_text(target)
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
                # txt = label_tag.get_text(strip=True)
                txt = self._get_clean_element_text(label_tag)
                if txt:
                    return txt

        # 4. <label>入力欄</label> のように、labelタグ自身に内包されている場合
        parent_label = inp.find_parent("label")
        if parent_label and isinstance(parent_label, Tag):
            # txt = parent_label.get_text(strip=True)
            txt = self._get_clean_element_text(parent_label)
            if txt:
                return txt

        return ""

    def _looks_like_genuine_contact_form(
        self,
        soup: BeautifulSoup,
        fields_text: str,
    ) -> bool:
        """抽出結果をお問い合わせフォームとして確定してよいか判定する。

        URL自体がお問い合わせページの場合は比較的緩く判定する。
        一方、トップページ等の「お問い合わせリンクがあるだけ」のページでは、
        フォームコンテナ自身にお問い合わせらしい特徴がある場合のみ採用する。
        """
        if not fields_text:
            return False

        contact_words = (
            "お問い合わせ",
            "お問合せ",
            "問合せ",
            "お問い合わせ内容",
            "ご相談",
            "相談内容",
            "メールアドレス",
            "メール",
            "お名前",
            "氏名",
            "電話番号",
            "連絡先",
            "contact",
            "inquiry",
            "otoiawase",
            "toiawase",
        )

        # ページ内のフォーム候補を取得
        containers = self._find_form_containers(soup)

        for container in containers:
            container_text = container.get_text(" ", strip=True).lower()

            # フォーム内の入力項目を確認
            inputs = container.find_all(["input", "textarea", "select"])

            if not inputs:
                continue

            has_email = bool(container.find("input", {"type": "email"}))

            has_textarea = bool(container.find("textarea"))

            # name / id / placeholder も問い合わせ判定材料にする
            input_text_parts: list[str] = []

            for inp in inputs:
                for attr in ("name", "id", "placeholder", "aria-label"):
                    value = inp.get(attr)
                    if value:
                        input_text_parts.append(str(value).lower())

            input_metadata = " ".join(input_text_parts)

            contact_word_count = sum(1 for word in contact_words if word.lower() in container_text or word.lower() in input_metadata)

            # メール + textarea はかなり強い問い合わせフォームの特徴
            if has_email and has_textarea:
                return True

            # 問い合わせ関連語がフォーム自身に複数存在する場合
            if contact_word_count >= 2 and (has_email or has_textarea):
                return True

            # 問い合わせフォームではメール欄がなくても、
            # 「氏名 + 電話番号 + 内容」等の構成になっている場合がある。
            has_name = any(word in container_text or word in input_metadata for word in ("お名前", "氏名", "name"))
            has_phone = any(word in container_text or word in input_metadata for word in ("電話番号", "電話", "tel", "phone"))

            if has_textarea and has_name and has_phone:
                return True

        return False

    def _find_form_containers(self, soup: BeautifulSoup) -> list[Tag]:
        """<form>タグや疑似フォームを探すが、検索窓（Search）関連は最初から完全に除外する。"""
        raw_containers: list[Tag] = list(soup.find_all("form"))
        raw_containers.extend(soup.find_all(attrs={"role": "form"}))
        raw_containers.extend(soup.find_all(attrs={"data-form": True}))

        # <form>タグを使わず、JSでAJAX送信するカスタム実装(class名に
        # contact/form/inquiry等を含むdiv)の疑似フォームは、以前は
        # 「ページ内に<form>タグが1つも無い場合」にしか探していなかった。
        # そのため、ページ内に検索窓や無関係な小さな<form>が1つでもあると、
        # 肝心のお問い合わせ用の疑似フォームが完全に見逃されてしまっていた。
        # <form>の有無に関わらず常に探索する(ただし、既に見つかった<form>を
        # 内包するdivは、同じフォームを二重に処理しないよう除外する)。
        # また、class名に"form"を含むdivは入れ子になりやすい
        # (例: div.p-contact-form > div.l-form > div.l-form__inputs は
        # いずれも正規表現にマッチする)ため、最も外側の候補だけを採用し、
        # 内側の候補は二重処理を避けるためスキップする。
        added_divs: list[Tag] = []
        for candidate in soup.find_all("div", class_=self._FORM_LIKE_CLASS_RE):
            if not candidate.find(["input", "textarea", "select"]):
                continue
            if candidate.find("form") is not None:
                continue
            if any(candidate in already.descendants for already in added_divs):
                continue
            added_divs.append(candidate)
        raw_containers.extend(added_divs)

        # 最後の手段: <form>タグも、それらしいclass名を持つdivも一切無いページ
        # (例: 見た目上はただの<table>にinputが並んでいるだけで、実際の送信は
        # 別途JSで隠しフォームに値をコピーして行う、古い自前実装のフォーム)。
        # メールアドレス欄またはメッセージ本文欄(textarea)が実在する場合に限り、
        # <body>全体を1つのコンテナとして扱う。
        if not raw_containers and (soup.find("input", {"type": "email"}) or soup.find("textarea")):
            body = soup.body
            if isinstance(body, Tag):
                raw_containers.append(body)

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
                inp_type_val = inp.get("type", "text")
                inp_type = "".join([str(x) for x in inp_type_val]).lower().strip() if isinstance(inp_type_val, list) else str(inp_type_val).lower().strip()
                is_choice_input = inp_type in ("radio", "checkbox")

                # 1. 厳格な仕様に基づくラベル（id/for, aria）
                # ただしradio/checkboxは、個々の選択肢に<label for>が正しく付与されている
                # ことが多く、そのまま使うと「はい」「いいえ」のような選択肢の文言そのものが
                # 項目名として抽出されてしまう。知りたいのは選択肢ではなく質問文(th/legend等の
                # 見出し)なので、radio/checkboxでは厳格ラベルの解決をスキップし、
                # 下のステップ2(構造探索)でグループ全体の見出しを拾わせる。
                if not is_choice_input:
                    txt_a = self._get_label_for_input(inp, form, soup)
                    txt_a = self._remove_required_marks(txt_a)
                    if txt_a and len(txt_a) < 50 and re.search(r"[A-Za-zぁ-んァ-ヶー一-龠々]", txt_a):
                        resolved_text = txt_a

                # 2. 周辺のHTML構造から探索（dl/dt/dd, table/tr/th, 兄弟要素, fieldset/legend）
                # 優先順位を3段階に分けて祖先チェーン全体を走査する:
                #   (a) dt/dd, th/td … 1項目に対して1対1で対応することが多く、最も信頼できる
                #   (b) 直接テキスト・直前の兄弟要素 … dt/dd等が無い場合の、局所的で具体的な手がかり
                #   (c) fieldset/legend … 1つのfieldsetに複数項目(住所のfieldset等)が
                #       含まれることがあり、(a)(b)より優先すると「郵便番号」等の具体的な
                #       ラベルを「住所」という大枠の見出しで上書きしてしまうため、最後の手段とする
                # (単純な1パスにまとめると、本来のラベルより手前の階層で
                #  <span class="exam">【全角】例：〇〇株式会社</span>のような
                #  「入力例のヒント」が先にマッチしてbreakしてしまう問題があったため、
                #  この優先順位ごとに祖先チェーンを繰り返し走査する構成にしている)
                if not resolved_text:
                    parent_chain = []
                    for p in inp.parents:
                        if p is form or not isinstance(p, Tag):
                            break
                        parent_chain.append(p)

                    # (a) dt/dd, th/td
                    for parent in parent_chain:
                        if parent.name == "dd":
                            prev_dts = [t for t in parent.find_previous_siblings("dt") if isinstance(t, Tag)]
                            if prev_dts:
                                # resolved_text = self._remove_required_marks(prev_dts[0].get_text(strip=True))
                                resolved_text = self._get_clean_element_text(prev_dts[0])
                                break

                        if parent.name == "td":
                            prev_ths = [t for t in parent.find_previous_siblings("th") if isinstance(t, Tag)]
                            if prev_ths:
                                # resolved_text = self._remove_required_marks(prev_ths[0].get_text(strip=True))
                                resolved_text = self._get_clean_element_text(prev_ths[0])
                                break

                            # <th>を使わず、同じ行内の手前の<td>をラベルとして使う
                            # 古い表組みパターンに対応する
                            # (例: <tr><td class="label">氏名</td><td><input ...></td></tr>)。
                            # ラベル用の<td>は入力欄を含まないはずなので、それを条件に
                            # 別の項目の入力セルを誤って拾わないようにする。
                            prev_tds = [t for t in parent.find_previous_siblings("td") if isinstance(t, Tag)]
                            if prev_tds and not prev_tds[0].find(["input", "textarea", "select"]) and not self._looks_like_hint_element(prev_tds[0]):
                                # t = self._remove_required_marks(prev_tds[0].get_text(strip=True))
                                t = self._get_clean_element_text(prev_tds[0])
                                if t and len(t) < 50 and re.search(r"[A-Za-zぁ-んァ-ヶー一-龠々]", t):
                                    resolved_text = t
                                    break

                        # (a-2) 入力欄自身の直前の兄弟要素がラベルらしい要素の場合
                        # 例: <div class="contact__item"><p class="contact__label">お名前</p><input ...></div>
                        if not resolved_text:
                            own_siblings = inp.find_previous_siblings(["p", "label", "span", "div", "dt", "th"])
                            if own_siblings and isinstance(own_siblings[0], Tag):
                                candidate = own_siblings[0]
                                if not candidate.find(["input", "textarea", "select"]) and not self._looks_like_hint_element(candidate):
                                    t = self._get_clean_element_text(candidate)
                                    if t and len(t) < 50 and re.search(r"[A-Za-zぁ-んァ-ヶー一-龠々]", t):
                                        resolved_text = t

                    # Contact Form 7 のように、入力欄を含む <p> の
                    # 直接テキストとしてラベルが記述されている形式に対応する。
                    # 例:
                    # <p>Message <span class="required">(required)</span><br>
                    #     <span class="wpcf7-form-control-wrap ...">
                    #         <textarea ...>
                    #     </span>
                    # </p>
                    if not resolved_text:
                        for parent in parent_chain:
                            if parent.name != "p":
                                continue

                            label_text = self._direct_child_text(parent)
                            label_text = self._remove_required_marks(label_text)

                            if not label_text:
                                continue

                            # 英語・日本語どちらも許可する。
                            if len(label_text) < 50 and re.search(
                                r"[A-Za-zぁ-んァ-ヶー一-龠々]",
                                label_text,
                            ):
                                resolved_text = label_text
                                break

                    # (b) 直接テキスト・直前の兄弟要素(入力例のヒントらしき要素は除外)
                    if not resolved_text:
                        for parent in parent_chain:
                            own_text = self._remove_required_marks(self._direct_child_text(parent))
                            if own_text and len(own_text) < 50 and any(c for c in own_text if ord(c) > 0x7F):
                                resolved_text = own_text
                                break

                            siblings = parent.find_previous_siblings(["div", "span", "label", "dt", "th", "p", "table"])
                            if siblings and isinstance(siblings[0], Tag):
                                candidate = siblings[0]
                                if not candidate.find(["input", "textarea", "select"]) and not self._looks_like_hint_element(candidate):
                                    # t = self._remove_required_marks(candidate.get_text(strip=True))
                                    t = self._get_clean_element_text(candidate)
                                    if t and len(t) < 50 and re.search(r"[A-Za-zぁ-んァ-ヶー一-龠々]", t):
                                        resolved_text = t
                                        break

                    # (c) fieldset/legend(<legend>に「必須」「任意」等のマーカー要素<i>が
                    # 混ざっている実装が多いため、<i>タグは除外してテキストを取得する)
                    if not resolved_text:
                        for parent in parent_chain:
                            if parent.name == "fieldset":
                                legend = parent.find("legend", recursive=False)
                                if isinstance(legend, Tag):
                                    legend_text = "".join(
                                        str(c) if isinstance(c, NavigableString) else c.get_text() for c in legend.children if not (isinstance(c, Tag) and c.name == "i")
                                    ).strip()
                                    legend_text = self._remove_required_marks(legend_text)
                                    if (
                                        legend_text
                                        and len(legend_text) < 50
                                        and re.search(
                                            r"[A-Za-zぁ-んァ-ヶー一-龠々]",
                                            legend_text,
                                        )
                                    ):
                                        resolved_text = legend_text
                                        break

                if not resolved_text:
                    for attr in ("aria-label", "title"):
                        val = inp.get(attr)
                        if not val:
                            continue
                        t = self._remove_required_marks(str(val).strip())
                        if not t or len(t) >= 50:
                            continue
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
        (
            # office-hiro.co.jp のような「Web Page Blocked!」型のFortiGate標準ブロックページ用。
            # カテゴリ名（フィッシング等）は _extract_fortinet_category() で別途抽出し、
            # 検知できた場合はこのデフォルト文言より詳細な理由文で上書きする。
            ["web page blocked"],
            "ネットワーク側のセキュリティ機器（Fortinet等）によりアクセスがブロックされました。"
            "実際のサイト内容を取得できませんでした。別ネットワークからの再調査、または手動確認をお願いします。",
        ),
    ]

    # ネットワーク機器ではなく、アクセス先のサーバー自身がボット対策等で返してくる
    # 汎用的なエラーページ（Apache/Nginxの標準403ページ等）の検知用シグネチャ。
    # obs-pre.net の「Forbidden / You don't have permission to access this resource.」のように、
    # Fortinet等のブロックページとは原因が異なる（＝別ネットワークから再調査しても無駄な）
    # ケースを区別するために用意する。
    _GENERIC_HTTP_ERROR_SIGNATURES: list[tuple[list[str], str]] = [
        (
            ["forbidden", "you don't have permission to access this resource"],
            "アクセス先のサーバーから403 Forbidden（アクセス拒否）が返されたため、"
            "実際のサイト内容を取得できませんでした。ネットワーク機器の問題ではなく、"
            "サーバー側のボット対策等によるアクセス制限の可能性が高いため、手動確認をお願いします。",
        ),
    ]

    def _extract_delayed_redirect_target(self, html: str, current_url: str) -> str:
        """meta refreshやJavaScriptのsetTimeoutによる遷移先の絶対URLを抽出する。

        ドメインが同一か別かに関わらず、検知できた遷移先の絶対URLをそのまま返す
        (ドメインの異同判定は呼び出し側の責務とする)。検知できない場合は空文字。
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

        # ブラウザ内部の特殊スキーム(chrome-error://等)が誤って抽出された場合の保険。
        # 通常は_fetch_rendered_html側で弾かれるため、ここに来ることは無いはずだが、
        # 万一に備えて多重に防御しておく。
        if target_url.startswith(("chrome-error://", "chrome://", "about:")):
            return ""

        return urljoin(current_url, target_url)

    def _detect_delayed_cross_domain_redirect(self, html: str, current_url: str, base_domain_clean: str) -> str:
        """meta refreshやJavaScriptのsetTimeoutによる「数秒後に自動的に別ドメインへ
        リダイレクトする」形式の移転案内ページを検知する。

        静的HTML取得(httpx)だけではmeta refreshやJavaScriptによる遷移は実行されず、
        このページの内容がそのまま(トップページとして)解析されてしまう。しかし実際には
        既に別ドメインへ移転済みであることが多いため、その場合はリダイレクト先の絶対URLを
        返し、呼び出し側で「調査結果:×」「不可の理由:すでにリニューアル済のため」
        「備考:移転先URL」として扱えるようにする。

        検知できない場合や、リダイレクト先が同一ドメイン内(https化・パス変更等)の
        場合は空文字を返す。同一ドメイン内リダイレクト自体の扱いは
        _extract_delayed_redirect_target()の戻り値を呼び出し側で直接使うこと
        (edena.jpのように、トップページが同一ドメイン内の実コンテンツURLへ
        meta refreshするだけの薄いページになっており、ここで単に情報を
        捨ててしまうと巡回対象のリンクが1件も見つからず、1階層1ページの
        誤った結果になっていた)。
        """
        absolute_target = self._extract_delayed_redirect_target(html, current_url)
        if not absolute_target:
            return ""

        target_domain_clean = self._get_clean_domain(absolute_target)

        if target_domain_clean and target_domain_clean != base_domain_clean:
            return absolute_target

        return ""

    def _reason_for_status_code(self, status_code: int) -> str:
        """4xx/5xxのHTTPステータスコードから、M列にそのまま出力できる理由文を生成する。

        httpxの例外(HTTPStatusError)経由・Playwrightのレスポンス経由のどちらから
        呼ばれても同じ文言を返せるように共通化したもの。200番台等、正常系の
        ステータスコードを渡した場合は空文字を返す。
        """
        if status_code == 403:
            return (
                "アクセス先のサーバーから403 Forbidden（アクセス拒否）が返されたため、"
                "実際のサイト内容を取得できませんでした。ネットワーク機器の問題ではなく、"
                "サーバー側のボット対策等によるアクセス制限の可能性が高いため、手動確認をお願いします。"
            )
        if status_code == 429:
            return "アクセス先のサーバーから429 Too Many Requests（レート制限）が返されたため、実際のサイト内容を取得できませんでした。時間を置いての再調査、または手動確認をお願いします。"
        if status_code >= 500:
            return f"アクセス先のサーバーでエラー（HTTPステータス {status_code}）が発生しているため、実際のサイト内容を取得できませんでした。手動確認をお願いします。"
        if status_code >= 400:
            return f"アクセス先のサーバーからHTTPステータス{status_code}が返されたため、実際のサイト内容を取得できませんでした。手動確認をお願いします。"
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

        # obs-pre.net の「Forbidden / You don't have permission to access this resource.」のように、
        # ネットワーク機器ではなくアクセス先のサーバー自身が拒否しているケースを区別する。
        # raise_for_status() が投げる例外なので、response からHTTPステータスコードを直接判定できる。
        if isinstance(error, httpx.HTTPStatusError):
            reason = self._reason_for_status_code(error.response.status_code)
            if reason:
                return reason

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

    def _extract_fortinet_category(self, html: str) -> str:
        """FortiGate等のブロックページ内にある「カテゴリ: xxx」/「Category: xxx」表記を抽出する。

        フィッシング（詐欺）・ギャンブル等、ブロック理由の分類名がページ内に
        埋め込まれている場合、その値をそのままM列（不可の理由）に反映できるようにする。
        見つからない場合は空文字を返す。
        """
        # 1. 同一行（同一テキストノード）に「ラベル: 値」が収まっている単純なケース
        text_match = re.search(r"(?:カテゴリ|Category)\s*[:：]\s*([^\n<]{1,50})", html, re.I)
        if text_match:
            candidate = text_match.group(1).strip()
            if candidate:
                return candidate

        # 2. FortiGateの警告ページによくあるテーブル形式（<td>カテゴリ</td><td>値</td>等）のように、
        #    ラベルと値がタグを挟んで別要素になっているケースをBeautifulSoupで拾う
        try:
            soup = BeautifulSoup(html, HTML_PARSER)
        except Exception:
            return ""

        label_node = soup.find(string=re.compile(r"(?:カテゴリ|Category)\s*[:：]?\s*$", re.I))
        if not label_node or not isinstance(label_node.parent, Tag):
            return ""

        # ラベルを含む要素そのものの次の兄弟要素、または祖先要素の次の兄弟要素から値を探す
        current: Tag | None = label_node.parent
        for _ in range(3):
            if current is None:
                break
            sibling = current.find_next_sibling()
            if isinstance(sibling, Tag):
                candidate = sibling.get_text(strip=True)
                if candidate and len(candidate) < 50:
                    return candidate
                break
            current = current.parent if isinstance(current.parent, Tag) else None

        return ""

    def _detect_network_block_page(self, html: str) -> str:
        """取得したHTMLが、実際のサイトではなくFortinet等のネットワークセキュリティ機器が
        返す警告/ブロックページである可能性を検知する。

        該当する場合は、M列（不可の理由）にそのまま出力できる具体的な理由文字列を返す。
        該当しない場合は空文字を返す。SSL証明書切れ等が原因でこの警告ページが返された場合、
        中身を見ずに「クロールできたページ数が極端に少ない」等の別の一般的な理由で
        要確認扱いになってしまい、原因が分かりにくくなることを防ぐのが目的。

        ページ内に「カテゴリ: フィッシング（詐欺）」のような分類表記が見つかった場合は、
        シグネチャの固定文言よりも詳細な理由文（カテゴリ名入り）を優先して返す。
        """
        if not html:
            return ""

        html_lower = html.lower()
        for keywords, reason in self._NETWORK_BLOCK_SIGNATURES:
            if all(k in html_lower for k in keywords):
                category = self._extract_fortinet_category(html)
                if category:
                    return (
                        f"ネットワーク側のセキュリティ機器（Fortinet等）により「{category}」に該当するとして"
                        "アクセスがブロックされました。実際のサイト内容を取得できませんでした。"
                        "別ネットワークからの再調査、または手動確認をお願いします。"
                    )
                return reason

        return ""

    def _detect_generic_error_page(self, html: str) -> str:
        """取得したHTMLが、ネットワーク機器ではなくアクセス先のサーバー自身が返す
        汎用的なエラーページ（Apache/Nginxの標準403ページ等）である可能性を検知する。

        該当する場合は、M列（不可の理由）にそのまま出力できる理由文字列を返す。
        該当しない場合は空文字を返す。_detect_network_block_page() とは異なり、
        「別ネットワークから再調査しても無駄」なケース（サーバー側の拒否）を
        Fortinet等のネットワーク機器によるブロックと区別するのが目的。
        """
        if not html:
            return ""

        html_lower = html.lower()
        for keywords, reason in self._GENERIC_HTTP_ERROR_SIGNATURES:
            if all(k in html_lower for k in keywords):
                return reason

        return ""

    def _detect_access_blocked_page(self, html: str) -> str:
        """取得したHTMLが、実際のサイト内容ではなく何らかのアクセス拒否ページ
        （ネットワーク機器によるブロック、またはサーバー自身の汎用エラーページ）である
        可能性を検知する。M列（不可の理由）にそのまま出力できる理由文字列、または
        該当しない場合は空文字を返す。

        Fortinet等のネットワーク機器によるブロックページを優先的にチェックし、
        該当しない場合のみサーバー自身の汎用エラーページ（403 Forbidden等）を確認する。
        """
        network_block_reason = self._detect_network_block_page(html)
        if network_block_reason:
            return network_block_reason
        return self._detect_generic_error_page(html)

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
        bool,
        str,
    ]:
        """ウェブサイトを巡回し、100ページに達した時点で打ち切る。

        戻り値の最後の要素 redirect_target_url は、meta refresh/JSタイマーによる
        別ドメインへの自動リダイレクト(移転案内ページ)を検知した場合のみ非空文字列となる。

        queue_exhausted (最後から2番目、実質末尾に追加した要素) は、巡回対象の内部リンクを
        すべて見つけ切った上で自然にキューが空になったか(True)、タイムアウト等で
        まだ未訪問のリンクが残ったまま打ち切ったか(False)を示す。total_pages が
        1〜2件程度の少数だった場合に、「本当にページ数が少ないサイトなのか」
        「クロールが何らかの理由で途中で終わってしまっただけなのか」を区別するために使う。

        subsystem_note (末尾に追加した要素) は、"/monster/"配下のEC-CUBEショップの
        ように、サイト全体のごく一部のディレクトリ配下に、明らかに毛色の違う
        別システムらしきものが同居していて、そこだけでページ数の大半を占めている
        場合に、その旨を知らせる注記文字列。該当しない場合は空文字。あくまで
        人間が最終判断する際の参考情報であり、これによってページ数・階層数の
        集計や◯/×判定そのものを変えることはしない(除外ロジックを機械的に
        作ろうとすると、今度は正規のサブディレクトリを誤って除外するリスクを
        負うため、あえて注記に留める方針: earth-inc.co.jpで検討)。
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
        # 「訪問した(リダイレクト前の)URL」とは別に、「リダイレクト後の実際のコンテンツURL」を
        # 正規化した形で記録する集合。href="ur"のようなルート相対のつもりで書かれた素の
        # 相対リンクが、ページごとに異なる基準(/nt/, /am/, /se/等)で解決されて
        # "/nt/ur", "/am/ur"...という別々の(実在しない)URLを生み、WordPress側の
        # 「近いスラッグへの自動リダイレクト」機能で結局同じ実ページ(/ur/)に
        # 302/301で着地する、というケースがあった(eas-c.jpで確認)。この場合
        # visitedはリダイレクト前のURL単位でユニークになってしまい、内容が
        # 完全に同じページが何件も別ページとしてカウントされてしまう。
        # リダイレクト後の正規化URLが既にこの集合にある場合は、今回の訪問を
        # 「新しいページ」としてカウントしない。
        canonical_visited: set[str] = set()
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
            # 上のindex.html除去で、ルート直下の"/index.html"は丸ごと""(空文字)に
            # なってしまう。一方トップページ"/"はここでは既に"/"のまま変化しないため、
            # 本来同じページである"/"と"/index.html"が""と"/"という別々の文字列に
            # 正規化され、別ページとして二重カウントされてしまっていた(ddesi.co.jpで確認)。
            # 除去した結果パスが空になった場合はルートの"/"に補正する。
            if clean_path == "":
                clean_path = "/"
            if clean_path.endswith("/") and clean_path != "/":
                clean_path = clean_path.rstrip("/")
            # HTML側でURLエンコードされずに href="/お知らせ/" のように日本語等の
            # 非ASCII文字がそのまま書かれているサイトがある。ここで正規化せずに
            # queue/visited/Refererヘッダー等へ流れ込むと、httpxがヘッダーや
            # リクエストラインをASCIIとしてエンコードしようとして
            # UnicodeEncodeError('ascii' codec can't encode characters...)になることがある。
            # 既存の%XXエンコード済み部分を壊さないよう safe="/%" を指定してエンコードする。
            clean_path = quote(clean_path, safe="/%")
            # _is_valid_internal_link()のドメイン一致判定は netloc.replace("www.", "") で
            # www有無を無視して「同一サイト」として扱っているのに対し、
            # normalize_url()側ではnetlocをそのまま保持していたため、内部リンクが
            # www有りと無しの両方の絶対URLで書かれているサイト(danshinen.org等)で、
            # 実質同じページが別URL扱いされ二重に訪問・カウントされてしまっていた。
            # visited/queueの重複排除キーとしてもwww有無を同一視するよう揃える。
            clean_netloc = re.sub(r"^www\.", "", parsed.netloc, flags=re.IGNORECASE)
            clean_query = self._clean_query(parsed.query)
            # 注意: ここではスキーム(http/https)は元のまま保持する。
            # この関数の戻り値はそのままキューに積まれ、実際にclient.get()で
            # リクエストされるURLになる。以前ここでスキームを常にhttpsへ
            # 強制していたところ、内部リンクがhttps非対応のサイト
            # (elastec.co.jp等、トップページ自体がhttpでしか200を返さない)で、
            # 発見した内部リンクが軒並みhttpsに書き換えられて接続できなくなり、
            # 全てhttpx.RequestErrorで失敗して(except節でログも残らず握りつぶされる
            # ため気づきにくい)トップページ1件しか巡回できなくなる重大な回帰を
            # 起こしていた。http/https両方が有効なサイトでの二重カウント対策
            # (ebi-ken.co.jpで確認)は、実URLではなく重複判定専用のキーである
            # _dedup_key() 側でのみ行う。
            return parsed._replace(netloc=clean_netloc, path=clean_path, query=clean_query, fragment="").geturl()

        def _dedup_key(u: str) -> str:
            """visited/queued_urls/canonical_visitedの重複判定にのみ使うキーを作る。

            normalize_url()の戻り値(実際にリクエストするURL)とは別に、
            スキーム(http/https)を常にhttpsへ統一した文字列を返す。
            http://とhttps://の両方が有効なサイト(ebi-ken.co.jp等)で、
            同じページが2つの生URLとして発見されても、この関数を通した
            比較・登録では同一ページとして扱われ、二重カウントされない。
            実際のリクエストにはこの関数の戻り値を使ってはならない。
            """
            p = urlparse(u)
            scheme = "https" if p.scheme in ("http", "https") else p.scheme
            return p._replace(scheme=scheme).geturl()

        def enqueue(url: str, path_depth: int, priority: bool) -> None:
            key = _dedup_key(url)
            if key in visited or key in queued_urls or len(canonical_visited) >= 100:
                return
            queued_urls.add(key)
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
                    # raise_for_status()は4xx/5xxで例外を投げて本文を捨ててしまうため、
                    # アクセス拒否ページ（Fortinet等のネットワーク機器のブロック、または
                    # サーバー自身が返す403 Forbidden等の汎用エラーページ）の検知は
                    # 必ず例外化する前に行う。
                    # (Fortinet等のSSLインスペクションはHTTPS側だけ本文を差し替えることが多く、
                    # 403等のステータスコードそのものにも実サイトかブロックページかの情報が
                    # 本文に含まれているため、ステータスに関わらずまず中身を確認する)
                    candidate_html = self._decode_response(response)
                    detected_block_reason = self._detect_access_blocked_page(candidate_html)
                    if detected_block_reason:
                        blocked_reason = detected_block_reason
                    response.raise_for_status()
                    if not detected_block_reason:
                        first_url = str(response.url)
                        first_html = candidate_html
                        blocked_reason = ""
                except Exception as e:
                    primary_error = e
                    if fallback_url:
                        try:
                            response = client.get(fallback_url)
                            if response.status_code == 401:
                                has_basic_auth = True
                            candidate_html = self._decode_response(response)
                            detected_block_reason = self._detect_access_blocked_page(candidate_html)
                            if detected_block_reason:
                                blocked_reason = detected_block_reason
                            response.raise_for_status()
                            if not detected_block_reason:
                                first_url = str(response.url)
                                first_html = candidate_html
                                blocked_reason = ""
                        except Exception as e2:
                            fallback_error = e2

                # httpx側でブロックページらしきものを検知していても(blocked_reasonが
                # 非空でも)、ここでは即断せずPlaywright(実ブラウザ)での再確認を試みる。
                # Fortinet Webfilter等のセキュリティ機器が、ブラウザからの通常アクセス
                # ではなく本ツールのような自動化ツールのアクセスだけを狙い撃ちで
                # ブロックするケースが実際にあり(ono-and.comで確認)、httpx側の検知
                # だけで確定させると、人間なら普通に見られるサイトを誤って
                # 「アクセス不可」扱いにしてしまう。
                if not first_url and self.render_js:
                    rendered_html, rendered_url, rendered_status, nav_error_reason = self._fetch_rendered_html(primary_url)
                    if rendered_html:
                        # Playwrightのgoto()はhttpxのraise_for_status()と違い、4xx/5xxでも
                        # 例外を投げずにエラーページのHTML本体をそのまま返してしまうため、
                        # ここでも同様にブロックページ検知を行う。
                        detected_block_reason = self._detect_access_blocked_page(rendered_html)
                        if not detected_block_reason and rendered_status is not None:
                            detected_block_reason = self._reason_for_status_code(rendered_status)

                        if detected_block_reason:
                            # 実ブラウザ(Playwright)でもブロックされた場合のみ、
                            # 最終的に「アクセス不可」と確定する。
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
                                detected_block_reason,
                                "",
                                True,
                                "",
                            )

                        # 実ブラウザでは正常に取得できた(=httpx側の検知は
                        # 自動化ツール狙い撃ちの誤検知だった)ため、ブロック扱いを解除する。
                        blocked_reason = ""
                        first_url = rendered_url or primary_url
                        first_html = rendered_html
                        first_html_from_playwright = True
                    elif nav_error_reason:
                        # ナビゲーション自体が失敗した(DNS失敗・接続拒否等で
                        # chrome-error://chromewebdata/に遷移した)ケース。
                        # このURLを移転先やfirst_urlとして絶対に使わないよう、
                        # ここでは何も採用せず、理由だけをblocked_reasonに残す。
                        # (以前はこれを「移転案内ページへのリダイレクト」と誤認し、
                        #  「すでにリニューアル済のため / 移転先：chrome-error://chromewebdata/」
                        #  という誤った結果になっていた)
                        blocked_reason = nav_error_reason

                if not first_url:
                    # httpxで検知していたブロック理由があればそれを優先し、
                    # なければ接続エラー自体の分類結果を使う。
                    conn_block_reason = blocked_reason or self._classify_connection_error(primary_error) or self._classify_connection_error(fallback_error)
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
                        True,
                        "",
                    )

                # httpxのfollow_redirects=Trueにより、301/302等のHTTPレベルのリダイレクトは
                # ここまでの時点で既に自動的に追跡済みになっている(Playwright経由の場合も同様、
                # 実ブラウザがリダイレクトを終えた後のURLがfirst_urlに入る)。
                # meta refresh/JSタイマー型の「移転案内ページ」と違い、この場合は現在のHTMLには
                # 移転の痕跡が残らず(既に移転先の実際のコンテンツを取得しているため)、
                # _detect_delayed_cross_domain_redirect()では検知できない。そのため、
                # 巡回開始時に要求したドメインと、実際に最終的に取得できたURLのドメインを
                # 直接比較することで、この「即時リダイレクトによる移転」を検知する。
                final_domain_clean = self._get_clean_domain(first_url)
                if final_domain_clean and final_domain_clean != base_domain_clean:
                    logger.info(
                        "初回アクセス時点で別ドメインへリダイレクトされたことを検知しました: %s -> %s",
                        start_url,
                        first_url,
                    )
                    return (
                        0,
                        0,
                        "",
                        "",
                        "",
                        first_html,
                        "",
                        False,
                        False,
                        has_basic_auth,
                        False,
                        "",
                        first_url,
                        True,
                        "",
                    )

                queue.append((first_url, 0))
                queued_urls.add(_dedup_key(first_url))

                previous_url = ""

                while queue:
                    if len(canonical_visited) >= 100:
                        is_over_100 = True
                        break
                    if time.time() - start_time > self.timeout:
                        break

                    current_url, depth = queue.popleft()
                    queued_urls.discard(_dedup_key(current_url))
                    norm_current = normalize_url(current_url)

                    if _dedup_key(norm_current) in visited:
                        continue

                    try:
                        visited.add(_dedup_key(norm_current))
                        parsed_current = urlparse(norm_current)

                        if self._has_repeating_path_pattern(parsed_current.path):
                            continue

                        req_headers = dict(self.headers)
                        if previous_url:
                            req_headers["Referer"] = previous_url

                        if current_url == first_url and first_html:
                            current_html = first_html
                            # first_url は既にリダイレクト追従後の最終URL(str(response.url))
                            resolved_url = first_url
                        else:
                            response = client.get(current_url, headers=req_headers)
                            if response.status_code == 401:
                                has_basic_auth = True
                            if response.status_code != 200:
                                continue
                            current_html = self._decode_response(response)
                            # normalize_url()でキュー投入時に末尾スラッシュを削っているため、
                            # current_url(例: ".../sdgs")のままリンク解決の基準にすると、
                            # follow_redirects=Trueで実際に内容を取得した先(".../sdgs/")と
                            # 食い違い、ページ内の相対リンク(例: href="factory/")が
                            # urljoin()で誤って1階層上(".../factory/")に解決されてしまう。
                            # そのため、以降のリンク解決には必ずリダイレクト追従後の
                            # 実URL(response.url)を使う。
                            resolved_url = str(response.url)

                        # リダイレクト後の実URLが既に別の(リダイレクト前のURLが異なる)
                        # 訪問で処理済みだった場合、中身は同じページなので以降の
                        # リンク抽出等の処理はスキップする。ページ数の集計には
                        # canonical_visited(正規化済みの実URLの集合、スキームは
                        # _dedup_key()でhttpsに統一)だけを使い、visited(生URLの
                        # 重複リクエスト防止用)からは意図的に取り消さない。
                        # normalize_url()自体はhttp/httpsのスキームを統一しない
                        # (実際のリクエストに使うURLのスキームを勝手に書き換えると、
                        # https非対応のサイトで接続が軒並み失敗する重大な回帰に
                        # なることがあったため: elastec.co.jp等で確認)。
                        # そのため重複判定専用の_dedup_key()でのみスキームを揃える。
                        norm_resolved = normalize_url(resolved_url)
                        resolved_key = _dedup_key(norm_resolved)
                        if resolved_key in canonical_visited and resolved_key != _dedup_key(norm_current):
                            continue
                        canonical_visited.add(resolved_key)

                        # httpxで取得済みのHTMLが静的（JSフレームワーク未使用）な場合、わざわざ
                        # Playwrightで再レンダリングし直すと全体タイムアウト予算(self.timeout)の
                        # 大半を1ページ目だけで使い果たし、2ページ目以降を巡回できず総ページ数が
                        # 極端に少ない「要確認」判定に化けてしまう。JSフレームワークを検知した
                        # ページ（＝静的HTMLだけでは内容が欠落する可能性が高いページ）のみに限定する。
                        if self.render_js and len(visited) == 1 and not first_html_from_playwright and self._detect_js_framework(current_html):
                            rendered, _rendered_url, _rendered_status, _nav_error = self._fetch_rendered_html(current_url)
                            if rendered:
                                current_html = rendered

                        previous_url = resolved_url
                        soup = BeautifulSoup(current_html, HTML_PARSER)
                        combined_html_src += f"\n{current_html}"

                        if depth > max_depth:
                            max_depth = depth

                        # CMS判定(ページによって検知しやすさが違うため、複数ページに渡って試みる。
                        # 一度検知できれば以降のページでは上書きしない)
                        if not cms_name:
                            detected_cms = self._detect_cms(current_html, resolved_url)
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
                                    True,
                                    "",
                                )

                            # Fortinet等のネットワーク機器によるブロックページ、または
                            # サーバー自身が返す汎用エラーページ(403 Forbidden等)の検知
                            detected_block_reason = self._detect_access_blocked_page(current_html)
                            if detected_block_reason:
                                blocked_reason = detected_block_reason
                                logger.warning(
                                    "アクセス拒否ページ（ネットワーク機器のブロック、またはサーバー自身の拒否）を検知したため巡回を打ち切りました: %s",
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
                                    True,
                                    "",
                                )

                            site_purpose = self._extract_purpose_and_features(current_html)

                            # 多言語切り替え機能の有無を検知
                            has_multilang = self._detect_multilang_switcher(soup)

                            # --- 1. メインナビ領域の候補をスコア判定で特定 ---
                            def find_main_nav_element(soup: BeautifulSoup) -> Tag | None:
                                nav_pattern = re.compile(
                                    r"gnav|rglnav|gmenu|global|main-?menu|navbar-?nav|"
                                    r"header-?menu|header__nav|navigation|menu",
                                    re.I,
                                )

                                candidates: list[Tag] = []

                                # nav / header はクラス名に関係なく候補にする
                                candidates.extend(soup.find_all(["nav", "header"]))

                                # ul / div はナビらしい class / id を持つものだけ候補にする
                                candidates.extend(
                                    soup.find_all(
                                        ["ul", "div"],
                                        class_=nav_pattern,
                                    )
                                )
                                candidates.extend(
                                    soup.find_all(
                                        ["ul", "div"],
                                        id=re.compile(
                                            r"nav|menu|glnav|gnav|gmenu|lh|header",
                                            re.I,
                                        ),
                                    )
                                )

                                # 重複除去
                                unique_candidates = []
                                seen_ids = set()

                                for tag in candidates:
                                    tag_id = id(tag)
                                    if tag_id not in seen_ids:
                                        seen_ids.add(tag_id)
                                        unique_candidates.append(tag)

                                candidates = unique_candidates

                                best_el = None
                                max_score = -100

                                for cand in candidates:
                                    attr_str = (f"{cand.get('id', '')} {' '.join(cand.get('class', []))}").lower()

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
                                            "page",
                                            "content",
                                        ]
                                    ):
                                        continue

                                    score = 0

                                    # nav は強く優先
                                    if cand.name == "nav":
                                        score += 30
                                    elif cand.name == "header":
                                        score += 5

                                    # class / id にナビ関連語があれば加点
                                    if nav_pattern.search(attr_str):
                                        score += 50
                                    elif re.search(r"menu|nav|lh", attr_str):
                                        score += 20

                                    # リンク数
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

                                # aタグ内の子要素を含めてリンク文字列を取得
                                raw_text = a_copy.get_text(" ", strip=True)

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
                            wix_menus = self._extract_wix_global_nav(soup) if cms_name == "Wix" else []

                            if not target_area and not wix_menus:
                                header_el = soup.find("header") or soup.find("div", id=re.compile(r"header|lh", re.I))
                                if isinstance(header_el, Tag):
                                    candidate_nav = header_el.find(
                                        ["ul", "nav"],
                                        class_=re.compile(r"nav|menu|gnav", re.I),
                                    ) or header_el.find("nav")
                                    target_area = candidate_nav if isinstance(candidate_nav, Tag) else header_el

                            if wix_menus:
                                for menu_text in wix_menus:
                                    if menu_text in ["×", "閉じる", "MENU", "メニュー", "標準", "拡大", "検索", "メニューを飛ばす"]:
                                        continue
                                    if menu_text not in global_nav_menus:
                                        global_nav_menus.append(menu_text)
                            elif target_area:
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
                        is_contact_url = any(k.lower() in resolved_url.lower() for k in self.CONTACT_KEYWORDS)
                        contact_link_tag = next(
                            (
                                a
                                for a in soup.find_all("a")
                                if re.search(
                                    r"問い合わせ|問合せ|相談|コンタクト|送信",
                                    a.get_text(" ", strip=True),
                                    re.I,
                                )
                            ),
                            None,
                        )
                        has_contact_text = contact_link_tag is not None

                        if (is_contact_url or has_contact_text) and not contact_fields:
                            candidate_fields, candidate_attachment = self._extract_form_fields(
                                current_html,
                                resolved_url,
                                client,
                                depth=0,
                            )
                            # 「本当にお問い合わせフォームらしいか」を検証してから採用する。
                            # (このページ自体が持つ無関係なフォーム(サイト内検索等)を、
                            #  単に「お問い合わせへのリンクがあるページ」というだけの理由で
                            #  誤って確定させないようにするため)
                            if self._looks_like_genuine_contact_form(soup, candidate_fields):
                                contact_fields, has_attachment = candidate_fields, candidate_attachment

                        # ログイン・マイページ機能の検知
                        # (パスワード欄の有無 + サイト共通ヘッダーの有無という構造的シグナルで判定)
                        if not has_login and self._has_password_login_form(current_html):
                            has_login = True

                        # 内部リンク巡回
                        anchor_tags = soup.find_all("a", href=True)
                        candidate_hrefs = [link["href"] for link in anchor_tags]
                        candidate_hrefs.extend([f.get("src") for f in soup.find_all("frame") if f.get("src")])
                        candidate_hrefs.extend(self._extract_js_links(current_html))

                        # meta refresh/JSタイマーによる遷移先が同一ドメイン内の場合、
                        # 通常のリンクと同様に巡回対象へ加える。
                        # (edena.jpのように、トップページがPC/モバイル振り分け等の
                        # 目的で同一ドメイン内の実コンテンツURLへ即座にmeta refresh
                        # するだけの薄いページになっていることがあり、この遷移先を
                        # 無視すると<a href>が1件も無いページとして扱われ、
                        # 実際には多数のページを持つサイトが「1階層1ページ」という
                        # 誤った結果になっていた)
                        delayed_redirect_target = self._extract_delayed_redirect_target(current_html, resolved_url)
                        if delayed_redirect_target:
                            candidate_hrefs.append(delayed_redirect_target)

                        for href in candidate_hrefs:
                            if self._is_valid_internal_link(resolved_url, href, base_domain_clean):
                                abs_href = urljoin(resolved_url, href)
                                norm_abs = normalize_url(abs_href)
                                parsed_abs = urlparse(norm_abs)
                                path_depth = len([p for p in parsed_abs.path.split("/") if p])

                                # ここで発見しただけのリンク(まだ一度も訪問していない候補URL)の
                                # 深さをmax_depthに反映してしまうと、実際には二度と訪問されない
                                # URL(既訪問との重複排除で捨てられるもの、JS内のlocation.href
                                # 代入から拾ったダミー文字列等のノイズ)によって「階層数」が
                                # 実態より過大に表示される不具合があった(dohkenkyo.or.jpで確認:
                                # 実際に訪問した最深ページは5セグメントしかないのに、階層数が
                                # 10と表示されていた)。max_depthは、実際にキューから取り出して
                                # 処理した(=本当に訪問した)ページの深さのみを根拠にする
                                # (visited時点の更新は本ループの先頭付近、
                                # `if depth > max_depth: max_depth = depth` を参照)。
                                # ここではenqueue()の優先度判定に使うpath_depthの算出のみ行う。

                                is_priority = any(
                                    k.lower() in norm_abs.lower()
                                    for k in (
                                        "contact",
                                        "inquiry",
                                        "otoiawase",
                                        "form",
                                        "login",
                                        "mypage",
                                        "member",
                                        "account",
                                    )
                                )
                                enqueue(norm_abs, path_depth, is_priority)

                        time.sleep(0.04)

                    except httpx.RequestError:
                        continue

        except Exception as e:
            logger.warning("クローラー内で予期せぬエラーが発生しました: %s", e, exc_info=True)

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
                True,
                "",
            )

        site_structure = "\n".join(global_nav_menus[:10])
        # 総ページ数は、リダイレクト前の生URL(visited)の件数ではなく、実際に
        # 表示された実体ページ(canonical_visited)の件数で数える。生URLは
        # http/httpsの混在や壊れた相対リンクによって同じページに対して
        # 複数存在しうるため、visitedの件数をそのまま使うと過大カウントに
        # なる(eas-c.jp等で確認)。
        final_page_count = "100ページ以上" if is_over_100 or len(canonical_visited) >= 100 else len(canonical_visited)

        # 内部的にはトップページ=0階層目として深さを数えているが、これをそのまま
        # 「階層数」として返すと、1ページだけの正常なサイトでも"0"と表示されてしまい、
        # クロールが失敗したように見えて紛らわしい。「◯階層」という言い回しに合わせ、
        # トップページ=1階層目として+1した値を、evaluator側の判定にも使う統一の値として返す。
        # (evaluator.py側の「3階層以上でNG」というしきい値も、この1始まりの値を
        # 前提に max_depth > 2 として判定するようになっている)
        display_depth: int | str = max_depth + 1
        if max_depth > 10:
            display_depth = "要確認"

        # キューが空 = 発見できた内部リンクはすべて訪問し終えて自然に終了したことを意味する。
        # 逆にキューに未訪問のURLが残っている場合、タイムアウトや最大ページ数(100件)到達等の
        # 理由で途中で打ち切っただけであり、「本当にページ数の少ないサイト」とは区別する必要がある。
        queue_exhausted = not queue

        # 「別システム同居」の注記機能は無効化した。blog/topics/news/pages/archives/
        # contentsと除外リストを積み増しても、今度は"products"(商品一覧)のような
        # 正当なディレクトリでも誤って注記が出るケースが見つかり、ディレクトリ名の
        # ブロックリスト方式ではこの種の誤検知にきりがないと判断したため。
        # _detect_subsystem_note()自体は将来別のアプローチ(例: セッションCookieの
        # スコープで技術的に異なるシステムかどうかを判定する等)で作り直す可能性を
        # 考慮してメソッドとしては残すが、呼び出しはしない。
        subsystem_note = ""

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
            queue_exhausted,
            subsystem_note,
        )
