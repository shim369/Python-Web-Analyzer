import html as html_lib
import logging
import posixpath
import re
import time
import warnings
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag, XMLParsedAsHTMLWarning

from web_analyzer.core.evaluator import RenewalEvaluator
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
    v3: crawl_and_analyzeの戻り値をtupleへ変更し、繰り返しパストラップ検知を強化。
    その後も戻り値要素の追加が続いており、最新の構成は crawl_and_analyze() の
    型ヒント・docstringおよび docs/model_design.md を参照。
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
    # 「必須」「任意」のマークを囲む括弧の種類
    _MARK_OPEN = r"[\(（\[［【〔〈《「『｛{<＜]"
    _MARK_CLOSE = r"[\)）\]］】〕〉》」』｝}>＞]"
    _MARK_WORDS = r"(?:必須項目|任意項目|必須|任意|required|optional)"
    # 括弧で囲まれた「必須」「任意」等のマーク(「会社名【任意】」「お名前<必須>」「Company (optional)」等)。
    # 「任意」は語句の一部(「任意団体名」等)としても使われるため、単独では消さず、括弧で囲まれた
    # もの・記号付きのもの・項目名の端にあるものに限って消す
    _BRACKETED_MARK_RE = re.compile(rf"{_MARK_OPEN}\s*{_MARK_WORDS}\s*{_MARK_CLOSE}", re.I)
    _PREFIXED_OPTIONAL_RE = re.compile(r"[※＊*]\s*(?:任意|optional)", re.I)
    _TRAILING_OPTIONAL_RE = re.compile(r"[\s :：|｜/／・-]*(?:任意|optional)$", re.I)
    _LEADING_OPTIONAL_RE = re.compile(r"^(?:任意|optional)(?:[\s ]+|[\s ]*[:：|｜/／][\s ]*)", re.I)
    # 「必須」等を除去した後に残る空の括弧(例: "お名前〔必須〕" → "お名前〔〕")
    _EMPTY_PARENS_RE = re.compile(rf"{_MARK_OPEN}\s*{_MARK_CLOSE}")

    # リンクを辿り尽くして見つかったページ数がこれ以下の場合のみ、sitemap.xmlで補完する
    SITEMAP_FALLBACK_MAX_PAGES = 2
    # サイトマップインデックスから読む子サイトマップの最大数
    MAX_CHILD_SITEMAPS = 5

    # 本巡回が時間切れ(self.timeout)で打ち切られ、100ページ未満のまま未訪問の
    # リンクが残った場合に限り、ページ数を確定させるためだけの追加巡回を行う。
    # 追加巡回ではCMS判定・フォーム解析等を一切行わずリンク抽出だけに絞り、
    # 複数ページを並列取得する。本巡回は1ページずつ解析しながら進むため、
    # 応答の遅いサイトでは150秒で数十ページしか進まず「36以上」のような
    # 下限値しか出せず、他ツールでページ数を調べ直す手間が発生していた。
    # 時間予算を本巡回のtimeoutごと延ばすと、並列ワーカー枠を重いサイトが
    # 長時間占有して全体のスループットが落ちる(scraper_service.py参照)ため、
    # 時間切れになったサイトにだけ追加予算を与える。
    # 実測では、ページ数が確定しなかった16サイトの追加巡回は最大75秒で
    # 終わった(末尾スラッシュ補完・誤った相対リンクの学習を入れた後の値)。
    # 余裕を見て120秒とする。
    COUNT_ONLY_EXTRA_TIMEOUT = 120.0
    # 応答の遅いサーバーにはリクエストを1件ずつしか処理しないものがあり
    # (実際に確認済み: 単発なら1件約2秒だが、4並列では各リクエストが約7秒、
    # 8並列では約14秒待たされ、合計の処理時間は並列数によらず変わらなかった)、
    # 並列数を上げても速くならないうえ、待ち時間が本巡回のpage_timeout(8秒)を
    # 超えてページを取りこぼし、実行のたびにページ数がぶれる原因になっていた。
    # 並列数は控えめにし、追加巡回のリクエストに限りタイムアウトを長めに取る。
    COUNT_ONLY_WORKERS = 3
    COUNT_ONLY_PAGE_TIMEOUT = 20.0

    # お問い合わせフォームもmailtoリンクも見つからなかった場合にL列(問合せ項目)に出す文言。
    # フォームを取り損ねた疑いがある場合は、巡回後にcontact_form_uncertainをTrueにする
    # (Excelでは背景色を付け、AI補完の対象にする)
    NO_CONTACT_FORM_TEXT = "問い合わせフォームなし"

    # お問い合わせページ・メールフォームらしいURLのパス(巡回が届いたかの判定用)
    CONTACT_PAGE_PATH_RE = re.compile(r"contact|inquiry|toiawase|otoiawase|mail-?form", re.I)

    # 外部のフォーム作成サービスのURL(リンク先・埋め込み)。サイト内でフォームが取れず、
    # これへのリンクがある場合は、フォームを取り損ねた疑いがあるとみなす
    FORM_SERVICE_URL_RE = re.compile(
        r"docs\.google\.com/forms|forms\.gle|forms\.office\.com|form\.run|hsforms|tayori\.com/form|"
        r"form-mailer\.jp|formzu|typeform\.com|jotform|questant",
        re.I,
    )

    # メールアドレスが設定されたmailtoリンク(<a href="mailto:info@example.com">)。
    # 宛先の無い"mailto:"だけのリンクは対象外。
    MAILTO_HREF_PATTERN = re.compile(r"^\s*mailto:\s*[^\s@?]+@", re.IGNORECASE)

    # 別ドメインにあるお問い合わせページへのリンク判定用。CONTACT_KEYWORDSは
    # "support"/"help"/"mail"等を含み、別ドメインに対して使うと
    # support.google.com等の無関係な外部リンクまで拾ってしまうため、より狭く絞る。
    EXTERNAL_CONTACT_URL_PATTERN = re.compile(r"contact|inquiry|toiawase|otoiawase", re.IGNORECASE)
    EXTERNAL_CONTACT_TEXT_PATTERN = re.compile(r"問い合わせ|問合せ|問合わせ|コンタクト|contact", re.IGNORECASE)
    # 巡回終了後に取得を試みる、別ドメインのお問い合わせページの最大件数
    MAX_EXTERNAL_CONTACT_PAGES = 3

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
        "__remixcontext",
        "astro-island",
        # Canva/Webflow/Framer等のノーコードサイトビルダーも、静的HTML(httpx取得分)
        # にはほぼ実質的なコンテンツ・内部リンクが含まれず、JavaScriptによる
        # クライアントサイド描画に依存している。既知のJSフレームワークと同様、
        # Playwrightでの再レンダリングが必要な対象として扱う(実際に確認済み)。
        # (Canva/Framerは単語として現れる場合だけをJS_FRAMEWORK_WORD_MARKER_REで判定する)
        "webflow",
        "framerusercontent",
    ]
    # 他の語の一部としても現れる目印は、単語として現れる場合に限る。部分一致では、WordPressの
    # CSS(--wp-editor-canvas-background)やJavaScript(e.canvas.width)の"canvas"、"premix"等に
    # 反応し、調査したサイトの半数以上で、トップページを不要にブラウザで描画し直していた(実際に確認済み)
    JS_FRAMEWORK_WORD_MARKER_RE = re.compile(r"(?<![a-z])(canva|remix|framer)(?![a-z])")

    # CMSの自動生成抜粋(excerpt)が文の途中で切れているサインとなる省略記号。
    # 例:「岡山建設は、あなたの住居に対する理想をカタチに...」のように、
    # 意味の切れたテキストがそのまま「用途」列に入ってしまうのを防ぐために使う。
    TRUNCATION_MARKERS = ("...", "…", "・・・")

    # フォームらしきコンテナ(form要素が無い場合の代替検出用)
    _FORM_LIKE_CLASS_RE = re.compile(r"form|contact|inquiry|entry", re.I)
    _LABEL_LIKE_CLASS_RE = re.compile(r"label|title|item-?label|form-?label|field-?name", re.I)

    # URLにキーワード"form"を部分一致で探すと、"/information/"等の無関係な単語にも
    # マッチしてしまう(実際に確認済み: お知らせ一覧"/information/"配下の記事が
    # すべて優先巡回扱いになって100ページ上限を使い切り、トップページから
    # リンクされている"/inquiry/"に一度も到達しないまま「問い合わせフォームなし」と
    # なっていた)。キーワード判定の前にこれらの単語を取り除いてから照合する。
    _FORM_FALSE_POSITIVE_RE = re.compile(r"information|platform|uniform|performance|transform|format", re.I)

    # お問い合わせページへのリンクらしいリンクテキスト。URLにキーワードを含まない
    # お問い合わせページ(例: "/materials/"の「資料請求」)を優先巡回するために使う。
    CONTACT_LINK_TEXT_RE = re.compile(r"問い合わせ|問合せ|問合わせ|資料請求|ご相談|コンタクト|contact|inquiry", re.I)

    # 静的HTMLに入力欄が無いお問い合わせページを、JSレンダリングし直して
    # フォームを探す最大回数(1ページあたり数秒かかるため、時間予算を食い潰さないよう制限する)
    MAX_CONTACT_PAGE_RENDERS = 2

    # <form>要素も疑似フォームのdivも無い場合に、本文欄(textarea)を起点として
    # フォーム相当のまとまりとみなす祖先要素を探す際の最大遡り階層数
    MAX_BARE_FORM_ANCESTOR_LEVELS = 6

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
    # 無限に入れ子になっていくURLが生成されてしまう(実際に確認済み)。
    # 数字部分が毎回変わるため _has_repeating_path_pattern の隣接同一チェックを
    # すり抜けるが、同じキーワードが2回以上パス内に出現すること自体が
    # 明確に異常な兆候であるため、専用にチェックする。
    _PAGINATION_SEGMENT_KEYWORDS = {"page", "paged"}

    # normalize_url()でクエリ文字列を丸ごと除去すると、"detail.php?id=1"や
    # "detail.php?id=2"のような、クエリパラメータで実際に異なるコンテンツを
    # 出し分ける旧来型PHPサイトで、全ての記事が同一URLとして
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
        # ページ数が水増しされる原因になっていた(実際に確認済み)。
        # これらは値によってコンテンツが変わりうる(例: sort=price_ascで
        # 表示順が変わる、word=xxxで絞り込み結果が変わる)ため、本来は
        # 除去すべきでないケースもあるが、空値やデフォルト値での重複発生の
        # 実害の方が大きいと判断し、除去対象に含める。
        "sort",
        "order",
        "orderby",
        "word",
        # CakePHP 2系のサイト(mod_rewrite未使用時のURL構造をリンク生成時にも
        # 引きずっているもの)では、"/news/index/page:2?url=news"と
        # "/news/index/page:2?url=news%2Findex"のように、パス自体を
        # ミラーしただけの自己参照的な"url"パラメータがリンクごとに値違いで
        # 付与され、実質同一ページが別ページとして重複カウントされていた
        # 実際に確認済み。コンテンツ識別に使われるパラメータではなく
        # ルーティングの残骸にすぎないため除去対象に含める。
        "url",
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
        # 直前のcrawl_and_analyze()で「問い合わせフォームなし」とした結果に、取り損ねの疑いがあるか
        # (お問い合わせページに入力欄があるのに項目を取れなかった・お問い合わせページまで
        # 巡回が届かなかった・外部のフォームサービスへのリンクがある)
        self.contact_form_uncertain = False

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

        HTML側に"assets.example.net/js/app.js"のように、本来別ドメインへの絶対URL
        であるべきリンクがスキーム(https://等)を欠いたまま書かれていることがある。
        この場合urljoin()は仕様通り「相対パス」として解決してしまい、
        "https://example.jp/assets.example.net/js/app.js"のような、本来存在しない
        URLがパスの途中にホスト名を含む形で生成されてしまう(実際に確認済み)。
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
    # 全体の70〜90%に達することも珍しくなく(実際に"/blog/"で72%、"/topics/"で
    # 87%に達した例を確認)、比率のしきい値をどれだけ上げても
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
        # (実際に確認済み)。
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
        (実際に確認済み)。ただし「どのディレクトリが別システムか」を
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
            # 施設検索・会員機能等のCURRENTな機能であるケースにそぐわない。ページ数・階層数の
            # 判定に影響しうる、という事実だけを伝える中立的な表現にする。
            return (
                f"「/{top_dir}/」配下に{count}件({ratio:.0%})のページが集中しています。"
                "コーポレートサイト本体とは技術的に異なる機能やシステム"
                "(検索・会員機能、旧サイトの残存等)が同居している可能性があります。"
                "ページ数・階層数の判定に影響している場合があるため、"
                "内容を確認することをおすすめします。"
            )
        return ""

    def _url_has_keyword(self, url: str, keywords: tuple[str, ...] | list[str]) -> bool:
        """URLにキーワードのいずれかが含まれるか判定する("/information/"等による"form"の誤検知を除く)。"""
        url_lower = self._FORM_FALSE_POSITIVE_RE.sub("", url.lower())
        return any(k.lower() in url_lower for k in keywords)

    def _is_valid_internal_link(self, current_url: str, href: str, base_domain: str) -> bool:
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            return False
        abs_url = urljoin(current_url, href)
        parsed_abs = urlparse(abs_url)
        abs_domain = parsed_abs.netloc.replace("www.", "").split(":")[0]

        # ここまでの"ドットが2つ以上"という判定は、サブドメインを持つ
        # 他ドメインの誤埋め込みは捕捉できるが、調査対象サイト自身の
        # ドメイン名(ドットが1つしかない一般的な2ラベル構成)が
        # "example.jp/team"のようにスキーム抜けで自己参照的に埋め込まれるケースを
        # 見逃していた(実際に"https://example.jp/example.jp/team"のような
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
        # (実際に確認済み)。xmlrpc.php・wp-admin配下も同様の理由で除外する。
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

        if self._has_calendar_query_trap(parsed_abs.query):
            return False

        # "pageindices"は、PDFカタログ等をめくれるデジタルブック化する
        # ビューアウィジェット(flipper.js等)が、検索エンジン向けに
        # ページ単位で自動生成するSEO用の代替HTML群に使われるディレクトリ名
        # (実際に確認済み: /catalog/202604/pageindices/index1.html
        # 〜index112.htmlのように、カタログ1冊(112ページ)につき100件超の
        # URLが生成され、ページ数100件上限到達の主因になっていた)。
        # 実際の閲覧者は"../index.html"のビューア1ページを操作して
        # めくるだけで、1ページずつ個別URLとして遷移することはなく、
        # サイトの実コンテンツページとしては数えるべきではない。
        if "/pageindices/" in parsed_abs.path.lower():
            return False

        return abs_domain == base_domain or self._is_shared_ssl_alias(abs_domain, base_domain)

    @staticmethod
    def _is_shared_ssl_alias(host: str, base_domain: str) -> bool:
        """レンタルサーバーの共用SSL用ホスト(調査対象サイトの別名)か判定する。

        実際に確認済み: etp-jp.comのメニューのリンク先がすべて
        "https://etp-jp-com.prm-ssl.jp/contact.html"のような共用SSLホストに
        なっており、外部ドメイン扱いで巡回されずにページ数・問合せ項目が
        取得できていなかった。共用SSLホストは先頭ラベルが「ドメイン名の
        ドットをハイフンに置き換えたもの」になる命名が一般的なため、
        それに完全一致する場合のみ同じサイトとみなす。
        """
        host = host.split(":")[0].lower()
        base = re.sub(r"^www\.", "", base_domain.split(":")[0].lower())
        first_label, _, rest = host.partition(".")
        return bool(rest) and host != base and first_label == base.replace(".", "-")

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

        has_password_field = bool(body.find("input", attrs={"type": re.compile(r"^password$", re.I)}) or body.find(attrs={"autocomplete": re.compile(r"current-password|new-password", re.I)}))
        if not has_password_field:
            return False

        has_site_chrome = bool(body.find("header") or body.find(class_=self._HEADER_LIKE_RE) or body.find(id=self._HEADER_LIKE_RE))
        return has_site_chrome

    def _detect_cms(self, html: str, url: str = "") -> str:
        html = html.lower()

        # "assets_c" はMovable Type固有の自動生成アセット格納ディレクトリ(記事内画像・
        # サムネイル等)であり、HTML本文にmt-content/mt-static等の目印が出ない
        # テンプレートでも、URLパスにこのディレクトリ名が含まれていればMovable Type製
        # サイトだと判定できる(実際に確認済み)。
        if url and "/assets_c/" in url.lower():
            return "Movable Type"

        # DNN(DotNetNuke)製サイトで"WP"に誤判定される事例があった。
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
            # v9(2021年)で"concrete5"から"Concrete CMS"へ改称された。旧称の
            # サイトも新称のサイトも判定できるよう検出キーワードは両方
            # 残しつつ、表示名はevaluator.py側のコメント(CAPTCHA_KEYWORDS)
            # とも表記を揃え、現行の正式名称にする。
            "Concrete CMS": [
                "concretecms",
                "concrete5",
                # コア機能のJS/CSSが"/concrete/js/...", "/concrete/css/..."のように
                # "concrete"という名前のディレクトリから配信される。metaタグの
                # generator表記が"Concrete CMS 9.x"のようにスペース入りで
                # "concretecms"にマッチしないケースの補完にもなる。末尾スラッシュ
                # 込みにすることで、建設業者サイト等での"concrete"(コンクリート)
                # という一般的な単語との誤検知を避ける。
                "concrete/",
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

    def _extract_global_nav_menus(self, soup: BeautifulSoup, cms_name: str) -> list[str]:
        """トップページのグローバルナビゲーションのメニュー項目名を、表示順に返す(「構成」列用)。"""
        menus: list[str] = []

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
                # WordPressはメニュー名からidを作るため、日本語のメニュー名が
                # "menu-%e3%83%95%e3%83%83..."(=フッターメニュー)のようにURLエンコード
                # されたidになる。デコードしてから判定しないと"フッター"を見落とす。
                attr_str = unquote(f"{cand.get('id', '')} {' '.join(cand.get('class', []))}").lower()

                # メニュー項目(li)の中にある要素は、ドロップダウンの子メニューであって
                # グローバルナビ本体ではない(class名に"menu"を含むため高得点になりやすい)。
                # フッター内のメニューも同様に対象外
                if cand.find_parent("li") is not None or self._is_in_page_footer(cand) or "フッター" in attr_str:
                    continue
                # 本文(<main>/<article>)の中にあるメニューは、そのページのローカルナビ等で
                # グローバルナビではない。ヘッダーをJavaScriptで書き出すサイトでは
                # 本文中のローカルナビを拾っていた(実際に確認済み)。ただし<main>が
                # <header>ごとページ全体を包んでいる作りもあるため、その場合は除外しない
                if self._is_in_page_body(cand):
                    continue

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

                # スマホ用のナビ(class="sp_navi"、"siteHeader__nav-sp"等)は、PC用の
                # グローバルナビとは別に置かれ、子メニューまで展開した項目を持つことが多い。
                # navタグの加点でPC用のul等に勝ってしまっていた(実際に確認済み)
                if self.SP_NAV_ATTR_RE.search(attr_str):
                    score -= 60
                elif self.PC_NAV_ATTR_RE.search(attr_str):
                    score += 10

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

                # リンク数。メニューのリストがある場合はトップレベルの項目数で数える
                # (ドロップダウン/メガメニューの子リンクまで数えると、本物の
                # グローバルナビほど30件を超えて減点され、カルーセルの送りボタン等の
                # 別の領域に負けていた。実際に確認済み)
                menu_items = self._menu_list_items(cand)
                a_count = len(menu_items) if len(menu_items) >= 3 else len(cand.find_all("a"))

                if 3 <= a_count <= 25:
                    score += 30
                elif a_count > 30:
                    score -= 40

                # 同点の場合は、メニュー項目(トップレベルのli)の多い方を優先する。
                # ヘッダー上部の「その他/お問い合わせ」のような小さなボタン群のnavと、
                # 本来のグローバルナビのnavが同じ点数になり、文書順で先に出てくる
                # 前者が選ばれていた(実際に確認済み)。
                score += min(len(menu_items), 12)

                if score > max_score:
                    max_score = score
                    best_el = cand

            return best_el if max_score >= 15 else None

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
                if menu_text not in menus:
                    menus.append(menu_text)
        elif target_area:
            menus = self._menu_texts_from_area(target_area)

        # ヘッダーにメニューが無く、サイト全体共通のサイドバー(aside.sidebar等)や
        # ハンバーガーメニューで開くドロワーにだけグローバルナビを置いているサイトが
        # ある(実際に確認済み)。ブログのカテゴリ一覧等のサイドバーを誤って拾わない
        # よう、上記で3項目未満しか取れなかった場合に限り、本文領域の外にある
        # サイドバー・ドロワーのメニューを探す。
        if len(menus) < 3 and not wix_menus:
            sidebar_nav = self._find_sidebar_nav(soup)
            if sidebar_nav is not None:
                sidebar_menus = self._menu_texts_from_area(sidebar_nav)
                if len(sidebar_menus) >= 3:
                    menus = sidebar_menus

        return menus

    # メニュー名に添えられた英語表記・副題(<span class="en">About</span>等)のclass名。
    # 以前は部分一致で判定していたため、"menu"や"center"のように"en"を含むだけの
    # class名でもメニュー名そのものを消してしまい、構成が空欄になっていた
    # (実際に確認済み: <span class="l-header__menu-bottom-box">製品のご案内</span>)。
    # class名の区切り("-"/"_")単位か、キャメルケースの語として現れる場合に限る。
    SUB_LABEL_CLASS_RE = re.compile(r"(?:^|[-_])(?:sub|subtitle|en|eng|english|ruby)(?:$|[-_\d])", re.I)
    SUB_LABEL_CAMEL_CLASS_RE = re.compile(r"[a-z](?:Sub|En|Eng|English|Ruby)(?:$|[-_A-Z\d])")

    # classの無い英語表記の要素とみなす文字列(英字・空白・記号のみ。数字だけの「01」等は含めない)
    EN_SUB_LABEL_TEXT_RE = re.compile(r"^[A-Za-z][A-Za-z &'.\-/]*$")
    JAPANESE_TEXT_RE = re.compile(r"[ぁ-んァ-ヶ一-龠々]")

    @classmethod
    def _is_sub_label_class(cls, class_name: str | None) -> bool:
        if not class_name:
            return False
        return bool(cls.SUB_LABEL_CLASS_RE.search(class_name) or cls.SUB_LABEL_CAMEL_CLASS_RE.search(class_name))

    # メニュー項目として扱わない文言(ハンバーガーメニューの開閉ボタン、文字サイズ切替等)
    NON_MENU_TEXTS = frozenset(["×", "閉じる", "MENU", "メニュー", "標準", "拡大", "検索", "メニューを飛ばす"])

    # スマートフォン表示でだけ出すメニュー項目に付けられる典型的なclass名。
    # PC表示のグローバルナビに無い項目が「構成」に混ざるため除外する。
    SP_ONLY_CLASS_RE = re.compile(
        r"^(sp|is-sp|u-sp|sp[-_]?only|only[-_]?sp|show[-_]?sp|sp[-_]?show|visible[-_]?(sp|xs)|"
        r"(hidden|hide|none)[-_]?pc|pc[-_]?(hidden|hide|none)|d-(md|lg)-none)$",
        re.I,
    )

    # ナビ領域のclass/id(小文字化・空白区切り)が、スマホ用/PC用のナビであることを示す語。
    # "sp-none"/"pc-hide"等は逆の意味(その端末では非表示)になる
    SP_NAV_ATTR_RE = re.compile(r"(?:^|[\s_-])(?:sp(?:nav|menu|[\s_-]|$)(?!(?:hide|hidden|none|off)(?:[\s_-]|$))|pc[\s_-](?:hide|hidden|none)(?:[\s_-]|$))")
    PC_NAV_ATTR_RE = re.compile(r"(?:^|[\s_-])(?:pc(?:[\s_-]|$)(?!(?:hide|hidden|none|off)(?:[\s_-]|$))|sp[\s_-](?:hide|hidden|none)(?:[\s_-]|$))")

    # グローバルナビとみなす最大項目数。これを超えたら切り詰めずに「構成」列を空欄にする
    # (途中で切れた内容は使えないため。Excelでは背景色が付き、AI補完の対象になる)
    MAX_STRUCTURE_ITEMS = 10

    # SNSへのリンクだけの項目(ナビ内に並んでいても構成の項目にはしない)
    SNS_ITEM_RE = re.compile(r"^(twitter|x|facebook|instagram|youtube|line|tiktok|note|linkedin|pinterest)(page|公式)?$", re.I)

    # <li>項目名<a class="link"></a></li>のような、中身の無いリンクを重ねた項目で、
    # liの文字列を項目名とみなす最大文字数
    OVERLAY_LINK_LABEL_MAX_LEN = 20

    # リンクの無い短い文字列だけの項目を見出しとみなす際に除く文言(コピーライト・電話番号・
    # ドロワーの閉じるボタン・検索欄等)
    NON_LABEL_TEXT_RE = re.compile(r"©|copyright|\d{2,4}-\d{2,4}-\d{3,4}|^(close|search|menu|open|閉じる|検索)$", re.I)

    # グローバルナビ本体のリストらしいclass/id(同じ項目数のリストが複数ある場合の優先に使う)
    NAV_LIST_ATTR_RE = re.compile(r"gnav|global|main-?menu|main-?nav|header-?(menu|nav)|nav-?list", re.I)

    # ul/liを使わないナビで、ドロップダウンの子メニューを包む要素のclass
    DROPDOWN_PANEL_CLASS_RE = re.compile(r"dropdown-menu|sub-?menu|mega-?menu|child", re.I)

    def _menu_list_items(self, area: Tag) -> list[Tag]:
        """ナビ領域内で、グローバルナビ本体とみなすリストのトップレベル項目(li)を返す。

        ナビ領域内に複数のリスト(本体のメニューと「お問い合わせ/WEB見積もり」のような
        小さなボタン群)がある場合、以前はclass/idに"nav"/"menu"を含むものを
        優先していたため、id="global"の本体ではなくid="nav-sub"のボタン群を
        拾っていた(実際に確認済み)。名前ではなく、トップレベルの項目数が
        最も多いリストを本体とみなす。ドロップダウンの中のリスト(li配下のリスト)は
        項目数が多くても本体ではないため対象外にする。
        """
        lists = [area] if area.name in ("ul", "ol") else []
        lists += [ul for ul in area.find_all(["ul", "ol"]) if not any(p is not area and p.name == "li" for p in self._ancestors_within(ul, area))]

        best: list[Tag] = []
        best_list: Tag | None = None
        best_has_nav_class = False
        for lst in lists:
            items = self._list_items(lst)
            # 項目数が同じなら、class/idがナビらしいリストを優先する(PC用ナビの中で、
            # 先に出てくるメガメニューの製品一覧が本体と同数になり選ばれていた。実際に確認済み)
            has_nav_class = bool(self.NAV_LIST_ATTR_RE.search(f"{lst.get('id', '')} {' '.join(lst.get('class') or [])}"))
            if len(items) > len(best) or (items and len(items) == len(best) and has_nav_class and not best_has_nav_class):
                best, best_list, best_has_nav_class = items, lst, has_nav_class

        # <nav><ul class="l-gnav__list">…</ul><ul class="l-gnav__list">…</ul></nav>のように、
        # 1つのグローバルナビを同じclassのリストに分けて並べている作り(実際に確認済み)は
        # まとめて1つのナビとみなす。classの違うリスト(お問い合わせ等のボタン群)はまとめない
        if best_list is not None and best_list.get("class"):
            siblings = [lst for lst in lists if lst is best_list or (lst.parent is best_list.parent and lst.get("class") == best_list.get("class"))]
            if len(siblings) > 1:
                best = [li for lst in siblings for li in self._list_items(lst)]
        return best

    def _list_items(self, lst: Tag) -> list[Tag]:
        """リストlstのトップレベルの項目(li)を返す。"""
        # <ul><a href="/x/"><li>項目</li></a>…のように、リンクの中にliを置いた
        # 崩れたマークアップ(実際に確認済み)のliも、リスト直下の項目として数える
        items = [li for li in lst.find_all("li") if isinstance(li, Tag) and self._is_top_level_item(li, lst)]
        if not items:
            # <ul><div><li>...のような崩れたマークアップ
            first_li = lst.find("li")
            if isinstance(first_li, Tag) and isinstance(first_li.parent, Tag):
                items = [li for li in first_li.parent.find_all("li", recursive=False) if isinstance(li, Tag)]
        return items

    @staticmethod
    def _is_top_level_item(li: Tag, lst: Tag) -> bool:
        """liがリストlstの直下の項目か(リスト直下のリンクに包まれたliを含む)。"""
        parent = li.parent
        if parent is lst:
            return True
        return isinstance(parent, Tag) and parent.name == "a" and parent.parent is lst

    @staticmethod
    def _is_in_page_footer(el: Tag) -> bool:
        """ページ全体のフッター(<footer>)の中にあるか。

        <section>等の中の<footer>は「そのセクションのフッター」で、ページ上部の
        ヘッダー領域のセクション内にグローバルナビを置くサイトもあるため対象外。
        """
        for footer in el.find_parents("footer"):
            if footer.find_parent(["section", "article", "aside", "header", "main", "nav"]) is None:
                return True
        return False

    @staticmethod
    def _is_in_page_body(el: Tag) -> bool:
        """本文領域(<main>/<article>)の中にあるか。<header>を含む<main>はページ全体の外枠とみなす。"""
        return any(body.find("header") is None for body in el.find_parents(["main", "article"]))

    @staticmethod
    def _ancestors_within(el: Tag, area: Tag) -> list[Tag]:
        """elの祖先のうち、areaより内側(area自身を含む)にあるものを返す。"""
        result: list[Tag] = []
        for parent in el.parents:
            result.append(parent)
            if parent is area:
                break
        return result

    def _is_sp_only_item(self, li: Tag) -> bool:
        # <a class="sp-only"><li>…</li></a>のように、liを包むリンク側に付いている場合も見る
        elements = [li, li.parent] if isinstance(li.parent, Tag) and li.parent.name == "a" else [li]
        for el in elements:
            classes = el.get("class") or []
            if any(self.SP_ONLY_CLASS_RE.match(c) for c in classes):
                return True
            style = str(el.get("style", "")).replace(" ", "").lower()
            if "display:none" in style:
                return True
        return False

    def _menu_item_label_link(self, li: Tag) -> Tag | None:
        """メニュー項目(li)の見出しとなる要素を返す。

        ドロップダウンを持つ親項目は、見出し自体がリンクでなく<span>等の場合があり
        (実際に確認済み: <li><span>各注意事項</span><div class="drop-down">…子リンク…</div></li>)、
        liの中の最初の<a>を拾うと子メニューのリンク名になってしまう。li直下の
        子メニュー(リスト・複数リンクを含むパネル)を取り除いた残りから見出しを探し、
        <a>があればそれを、無ければ残りの要素(見出しテキスト)を返す。
        """
        if li.find("a") is None and isinstance(li.parent, Tag) and li.parent.name == "a" and li.parent.get("href"):
            # <a href="/x/"><li>項目</li></a>のような、リンクの中にliを置いた崩れたマークアップ
            return li.parent
        li_copy = BeautifulSoup(str(li), HTML_PARSER).find("li")
        if not isinstance(li_copy, Tag):
            return None
        has_submenu = False
        # パネルの判定(リンク数)にリスト内のリンクも数えるため、リストより先に除去する。
        # 内側のパネルから除去する(外側から除去すると、<div class="h_option"><label>見出し</label>
        # <div class="content">子リンク…</div></div>のように見出しと子メニューを包む外枠ごと
        # 消え、見出しの代わりに子メニューのリンク名を拾っていた。実際に確認済み)
        for panel in reversed(li_copy.find_all(["div", "section"])):
            if panel.decomposed:
                continue
            if len(panel.find_all("a")) >= 2 or panel.find(["ul", "ol"]) is not None:
                panel.decompose()
                has_submenu = True
        for submenu in li_copy.find_all(["ul", "ol"]):
            submenu.decompose()
            has_submenu = True

        label_link = next((a for a in li_copy.find_all("a") if a.get_text(strip=True) or a.find("img")), None)
        if isinstance(label_link, Tag):
            return label_link
        # リンクでない見出しを採るのは、子メニューを持つ親項目に限る
        # (コピーライト表記等、ナビのリスト内にある単なる文字列を拾わないため)
        if has_submenu and li_copy.get_text(strip=True):
            return li_copy
        # <li>特装車<a href="speq.html" class="link"></a></li>のように、項目名はliに書き、
        # 中身の無いリンクを項目全体に重ねてクリック範囲にしている作り(実際に確認済み)。
        # 説明文付きのカード(本文の一覧)を拾わないよう、項目名らしい短い文字列に限る
        if len(li_copy.find_all("a")) == 1 and 0 < len(li_copy.get_text(strip=True)) <= self.OVERLAY_LINK_LABEL_MAX_LEN:
            return li_copy
        # <li><span>業務内容</span></li>のように、子メニューを別の場所(メガメニュー)に置いた
        # 親項目は、liの中にリンクも子メニューも無い(実際に確認済み)。同じリストの他の項目の
        # 半数以上がリンクで、項目名らしい短い文字列の場合に限り見出しとみなす
        if li_copy.find("a") is None and self._is_text_label_among_links(li, li_copy.get_text(strip=True)):
            # 文字列を包む要素のclassが"sub"等だと英語表記として消されるため、文字列だけを渡す
            # (実際に確認済み: <span class="cont sub hasc3"><span>業務内容</span></span>)
            label = BeautifulSoup("", HTML_PARSER).new_tag("span")
            label.string = li_copy.get_text(" ", strip=True)
            return label
        # 見出しが取れない場合は従来どおりli内の最初のリンク
        first_a = li.find("a")
        return first_a if isinstance(first_a, Tag) else None

    def _is_text_label_among_links(self, li: Tag, text: str) -> bool:
        """リンクの無い項目liの文字列textを、メニューの見出しとみなせるか。"""
        if not 0 < len(text) <= self.OVERLAY_LINK_LABEL_MAX_LEN or self.NON_LABEL_TEXT_RE.search(text):
            return False
        if li.find(["form", "input", "button", "select"]) is not None:
            return False
        # 区切り記号("|"等)だけの項目は除く
        if not re.search(r"[A-Za-z0-9ぁ-んァ-ヶ一-龠]", text):
            return False
        siblings = [s for s in li.find_next_siblings("li") + li.find_previous_siblings("li") if isinstance(s, Tag)]
        linked = sum(s.find("a", href=True) is not None for s in siblings)
        return linked >= 2 and linked * 2 >= len(siblings)

    def _menu_link_text(self, a_tag: Tag) -> str:
        """メニューのリンク(または見出し要素)から、英語表記・副題を除いた項目名を取り出す。"""
        a_copy: BeautifulSoup = BeautifulSoup(str(a_tag), HTML_PARSER)

        for sub in a_copy.find_all(["span", "small", "p", "em", "i"], class_=self._is_sub_label_class):
            sub.decompose()

        # <a>SDGs宣言<span>SDGs</span></a>のように、classの無い要素に英語表記を添えた作り
        # (実際に確認済み)。日本語の項目名があり、子要素が英字だけの場合はその子要素を除く。
        # 文字列の末尾の英字を消す_clean_menu_text()では、項目名が英字で始まる・「・」を含む
        # 場合に消えず、同じナビ内で英語表記の残る項目と消える項目が混在していた
        full_text = a_copy.get_text("", strip=True)
        for sub in a_copy.find_all(["span", "small", "p", "em", "i", "b", "strong"]):
            sub_text = sub.get_text("", strip=True)
            if sub.find(True) is None and self.EN_SUB_LABEL_TEXT_RE.match(sub_text) and self.JAPANESE_TEXT_RE.search(full_text.replace(sub_text, "", 1)):
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

    # ナビを別ファイルに分けてiframeで読み込む古い作り(<iframe src="parts/menu.htm">)のiframeのsrc
    # ファイル名(拡張子を除く)がナビらしい語で始まるか、語を区切り文字で挟んで含むもの
    # ("topics"・"headline"等のお知らせのiframeは対象外)
    NAV_IFRAME_SRC_RE = re.compile(r"(?:^|[_\-.])(?:g?menu|g?navi?|header)(?:$|[_\-.\d])", re.I)
    MAX_NAV_IFRAMES = 3

    def _has_nav_iframe(self, soup: BeautifulSoup) -> bool:
        """srcのファイル名がナビらしいiframeがあるか(ページを取得し直す前の絞り込み用。ホストは見ない)。"""
        return any(self.NAV_IFRAME_SRC_RE.search(urlparse(str(frame.get("src", ""))).path.rsplit("/", 1)[-1].rsplit(".", 1)[0]) for frame in soup.find_all(["iframe", "frame"], src=True))

    def _extract_iframe_nav_menus(self, soup: BeautifulSoup, page_url: str, client: httpx.Client, cms_name: str) -> list[str]:
        """トップページのナビがiframeの中にある場合に、iframeのページからナビの項目を取り出す。

        ホームページ・ビルダー等で作られた古いサイトには、ヘッダー・メニューを別ファイルにして
        iframeで読み込むもの(<iframe src="parts/menu.htm">)がある(実際に確認済み)。トップページ
        自体にはナビが無いため、srcのファイル名がナビらしい同一サイト内のiframeを文書順に取得し、
        3項目以上取れたものを使う。iframeのページ自体がナビなので、ul/liを使わない
        表組みのリンクの並び(collect_nav_candidates()の候補)も採る。
        ナビらしいiframeが無い・取れなかった場合は空リスト(取得のリクエストも行わない)。
        """
        base_host = urlparse(page_url).netloc.lower().removeprefix("www.")
        fetched = 0
        for frame in soup.find_all(["iframe", "frame"], src=True):
            if fetched >= self.MAX_NAV_IFRAMES:
                break
            src = urljoin(page_url, str(frame.get("src", "")))
            parsed = urlparse(src)
            if parsed.scheme not in ("http", "https") or parsed.netloc.lower().removeprefix("www.") != base_host:
                continue
            if not self.NAV_IFRAME_SRC_RE.search(parsed.path.rsplit("/", 1)[-1].rsplit(".", 1)[0]):
                continue
            fetched += 1
            try:
                response = client.get(src)
            except httpx.HTTPError:
                continue
            if response.status_code >= 400:
                continue
            frame_soup = BeautifulSoup(self._decode_response(response), HTML_PARSER)
            menus = self._extract_global_nav_menus(frame_soup, cms_name)
            if len(menus) < 3:
                candidates = [c for c in self.collect_nav_candidates(frame_soup) if 3 <= len(c) <= self.MAX_STRUCTURE_ITEMS]
                menus = candidates[0] if candidates else []
            if len(menus) >= 3:
                return menus
        return []

    def collect_nav_candidates(self, soup: BeautifulSoup, max_candidates: int = 12) -> list[list[str]]:
        """ページ内の「グローバルナビかもしれないメニューのリスト」の項目名を、文書順に返す。

        構成列をAIで補完する際に、AIにはこの候補の中から番号で選ばせる(AIが項目名を
        言い換えたり、存在しない項目を作ったりしないようにするため)。ドロップダウンの
        子メニュー(li配下のリスト)とページ全体のフッターは除き、項目が3〜15件の
        リストに限る。項目名がまったく同じリスト(PC用とスマホ用の重複等)は1つにまとめる。
        """
        candidates: list[list[str]] = []
        for lst in soup.find_all(["ul", "ol"]):
            if not isinstance(lst, Tag) or lst.find_parent("li") is not None or self._is_in_page_footer(lst):
                continue
            if lst.find_parent(["main", "article"]) is not None:
                continue
            items = self._menu_list_items(lst)
            if not 3 <= len(items) <= 15:
                continue
            menus = self._menu_texts_from_area(lst)
            if len(menus) >= 3 and menus not in candidates:
                candidates.append(menus)
            if len(candidates) >= max_candidates:
                return candidates

        # ul/liを使わず、テーブルやdivの中にリンク(画像リンクを含む)を並べただけの
        # 古い作りのナビもある(実際に確認済み)。リストの外にあるリンクを、最も近い
        # ブロック要素ごとにまとめて候補にする。
        groups: dict[int, list[Tag]] = {}
        for a in soup.find_all("a", href=True):
            if not isinstance(a, Tag) or a.find_parent(["ul", "ol", "main", "article"]) is not None or self._is_in_page_footer(a):
                continue
            container = a.find_parent(["div", "table", "nav", "header", "section", "aside", "p", "center", "body"])
            if isinstance(container, Tag):
                groups.setdefault(id(container), []).append(a)
        for links in groups.values():
            if not 3 <= len(links) <= 15:
                continue
            menus = []
            for a in links:
                text = self._menu_link_text(a)
                if text and text not in self.NON_MENU_TEXTS and text not in menus and not self._is_multilang_element(a, text):
                    menus.append(text)
            if len(menus) >= 3 and menus not in candidates:
                candidates.append(menus)
            if len(candidates) >= max_candidates:
                break
        return candidates

    def _menu_texts_from_area(self, target_area: Tag) -> list[str]:
        """ナビ領域からメニュー項目名を表示順・重複なしで取り出す。"""
        target_links: list[Tag] = []
        items = self._menu_list_items(target_area)
        if items:
            for li in items:
                if self._is_sp_only_item(li):
                    continue
                label = self._menu_item_label_link(li)
                if label is not None:
                    target_links.append(label)
        else:
            # ul/liを使わないナビでは、ドロップダウンの子メニュー(class="dropdown-menu"等)の
            # リンクまで項目に並んでいた(実際に確認済み)
            target_links = [
                link
                for link in target_area.find_all("a")
                if isinstance(link, Tag) and not any(self.DROPDOWN_PANEL_CLASS_RE.search(" ".join(p.get("class") or [])) for p in self._ancestors_within(link, target_area) if p is not target_area)
            ]

        menus: list[str] = []
        for a_tag in target_links:
            menu_text = self._menu_link_text(a_tag)
            if not menu_text or menu_text in self.NON_MENU_TEXTS or self.SNS_ITEM_RE.match(menu_text):
                continue
            if self._is_multilang_element(a_tag, menu_text):
                continue
            if menu_text not in menus:
                menus.append(menu_text)
        return menus

    def _find_sidebar_nav(self, soup: BeautifulSoup) -> Tag | None:
        """本文領域の外にある、サイト共通のサイドバー・ドロワーのナビ(3〜12項目のリスト)を探す。"""
        excluded_attr_re = re.compile(r"footer|widget|content|main|article|post|entry|news|blog|archive|categor", re.I)
        side_attr_re = re.compile(r"side|drawer|hamburger|offcanvas|off-canvas|mobile-?nav|sp-?nav|slide-?menu", re.I)
        sides = soup.find_all(["aside", "div", "nav"], class_=side_attr_re) + soup.find_all(["div", "nav"], id=side_attr_re) + soup.find_all("aside")
        for cand in sides:
            if not isinstance(cand, Tag):
                continue
            # <body>のclassにはWordPressが"home blog"等のページ種別を付けるため、body手前までを見る
            chain = [cand, *(p for p in cand.parents if p.name not in ("body", "html", "[document]"))]
            if any(isinstance(el, Tag) and (el.name in ("main", "article", "footer") or excluded_attr_re.search(f"{el.get('id', '')} {' '.join(el.get('class') or [])}")) for el in chain):
                continue
            # 本文(<main>/<article>)を含む要素は、サイドバーではなくページ全体の外枠
            # (<div class="side"><header>…</header><main>…</main></div>のような作りで、
            # 本文中のお知らせ一覧を拾っていた。実際に確認済み)
            if cand.find(["main", "article"]) is not None:
                continue
            items = self._menu_list_items(cand)
            if 3 <= len(items) <= 12:
                return cand
        return None

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
        word_match = self.JS_FRAMEWORK_WORD_MARKER_RE.search(html_lower)
        if word_match:
            return word_match.group(1)

        if self._looks_like_empty_js_shell(html):
            return "empty-shell(推定SPA)"

        return ""

    @staticmethod
    def _has_empty_header(html: str) -> bool:
        """ページのヘッダー(<header>・id="header")が空か。

        ヘッダーを fetch("/common/parts/header.html") や jQueryの.load() 等で後から
        差し込むサイトは、静的HTMLの<header>が空になる(実際に確認済み)。
        """
        soup = BeautifulSoup(html, HTML_PARSER)
        headers = [h for h in soup.find_all("header") + soup.find_all(id=re.compile(r"^header$", re.I)) if isinstance(h, Tag)]
        page_headers = [h for h in headers if h.find_parent(["main", "article", "section"]) is None]
        return bool(page_headers) and all(h.find(["a", "img"]) is None and not h.get_text(strip=True) for h in page_headers)

    @staticmethod
    def _has_form_like_controls(soup: BeautifulSoup) -> bool:
        """フォームらしい入力欄があるか(サイト内検索欄だけのページは無いとみなす)。"""
        if soup.find("textarea"):
            return True
        controls = [
            tag for tag in soup.find_all(["input", "select"]) if isinstance(tag, Tag) and str(tag.get("type", "text")).lower() not in ("hidden", "submit", "button", "image", "reset", "search")
        ]
        return len(controls) >= 3

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
        # 行頭の装飾記号(<span>●</span>HOME 等)
        text = re.sub(r"^[●○■□◆◇▶▷►▸・>＞»›]+", "", text)
        # 行末の矢印記号(「会社概要→」等。外部リンクの「↗」は構成補完側で判定に使うため残す)
        text = re.sub(r"[→➔➜]+$", "", text)
        # Material Iconsのアイコン名(合字で矢印として表示される文字列。例: 企業情報keyboard_arrow_down)
        text = re.sub(r"(keyboard_arrow_(down|up|left|right)|expand_(more|less)|chevron_(right|left)|arrow_(drop_down|forward|right_alt))$", "", text)
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
        # keep_blank_values=Falseにより、"?general="/"?junior="のような値を
        # 持たないパラメータはここで自動的に除去される。値が空ということは
        # コンテンツ識別に使われていない(=タブ切り替え等、JS側でしか使われない
        # フラグ)可能性が高く、実際にあるページの"?general="/"?junior="は、
        # サーバー側のレンダリングは3パターンとも
        # 完全に同一で、別ページとして重複カウントされていた。id=1のような
        # 値を持つパラメータはこれまで通り保持される。
        pairs = parse_qsl(query, keep_blank_values=False)
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
        (実際に確認済み)。実際に問題となるカレンダードリルダウン
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

    def _has_calendar_query_trap(self, query: str) -> bool:
        """クエリ文字列に、カレンダー/スケジュール表の月送りウィジェットが
        生成する年月値(例: "2027-10", "2023-1")が含まれているかを判定する。

        _is_depth_worthy_path()はパスセグメント側の数値ドリルダウン
        (例: /calendar/2024/08/09/)を検知するが、"schedule.php?month=2027-10"
        のように年月がクエリ文字列側に入っている場合はこれでは検知できない。
        この形のカレンダーウィジェットは「前月/翌月」リンクを延々と辿れて
        しまい(実際に確認済み: schedule.php?month=2022-12から2030-6
        まで、実質無限に近い件数のURLバリエーションを生んでいた)、他は
        7ページ程度の小規模サイトなのにページ数100件上限に達してしまう
        主因になっていた。年月らしき値(YYYY-M、YYYY-M-D、区切りは-/どちらも)は
        通常の商品ID・カテゴリスラッグ等とは形が明確に異なるため、この形に
        一致するクエリ値を持つリンクは巡回対象から除外する。
        """
        for _key, value in parse_qsl(query):
            if re.fullmatch(r"\d{4}[-/]\d{1,2}([-/]\d{1,2})?", value):
                return True
        return False

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

                # リアルなブラウザのヘッダー・画面設定を網羅してボット検知を回避…
                # という意図だったが、User-Agentだけは固定文字列で上書きしない
                # 方がよいことが判明した(実際に確認済み)。実際の
                # Chromiumは、User-Agentヘッダーを上書きしてもnavigator.userAgent
                # やSec-CH-UA系のClient Hintsヘッダーには「本当のバージョン」
                # (例: HeadlessChrome/149)がそのまま出続けるため、User-Agent文字列
                # だけ「Chrome/122」のように古いバージョンへ書き換えると、むしろ
                # UAとClient Hintsの不整合という分かりやすいボット判定シグナルを
                # 自ら作り出してしまい、固定UA文字列を指定しない(Chromium自身の
                # 素のUAをそのまま使う)場合よりWAFに弾かれやすくなっていた。
                context = browser.new_context(
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

    def _collect_link_candidates(self, soup: BeautifulSoup, html: str, resolved_url: str) -> list[str]:
        """ページ内から巡回候補となるhref(未検証・未解決のまま)を集める。

        本巡回とページ数カウント専用の追加巡回とで、拾うリンクの範囲を揃えるために共通化している。
        """
        candidate_hrefs = [link["href"] for link in soup.find_all("a", href=True)]
        candidate_hrefs.extend([f.get("src") for f in soup.find_all("frame") if f.get("src")])
        candidate_hrefs.extend(self._extract_js_links(html))

        # meta refresh/JSタイマーによる遷移先が同一ドメイン内の場合、
        # 通常のリンクと同様に巡回対象へ加える。
        # (実際に確認済み: トップページがPC/モバイル振り分け等の
        # 目的で同一ドメイン内の実コンテンツURLへ即座にmeta refresh
        # するだけの薄いページになっていることがあり、この遷移先を
        # 無視すると<a href>が1件も無いページとして扱われ、
        # 実際には多数のページを持つサイトが「1階層1ページ」という
        # 誤った結果になっていた)
        delayed_redirect_target = self._extract_delayed_redirect_target(html, resolved_url)
        if delayed_redirect_target:
            candidate_hrefs.append(delayed_redirect_target)
        return candidate_hrefs

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
        text = self._BRACKETED_MARK_RE.sub("", text)
        text = self._PREFIXED_OPTIONAL_RE.sub("", text)
        text = self._REQUIRED_MARK_RE.sub("", text)
        text = re.sub(r"^[\s ]+|[\s ]+$", "", text)
        text = self._TRAILING_OPTIONAL_RE.sub("", text)
        text = self._LEADING_OPTIONAL_RE.sub("", text)
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

        # 「必須」「任意」だけのバッジ要素(<p class="bg-blue">任意</p>等)。「任意」は語句としては
        # 消せない(「任意団体名」等)ため、要素の文字列がマークだけの場合に限って除く
        for badge in el_copy.find_all(True):
            if not badge.decomposed and badge.find(True) is None and self._MARK_ONLY_TEXT_RE.fullmatch(badge.get_text(strip=True)):
                badge.decompose()

        # <br>で改行して並べた複数の項目名(「都道府県<br>市区郡・町村」)は、項目名どうしの場合に限り
        # 改行区切りで返し、呼び出し側で別々の項目にする。補足(「お名前<br>（全角）」等)は従来どおり連結する
        for br in el_copy.find_all("br"):
            br.replace_with(NavigableString(self._LABEL_BREAK))
        parts = [self._remove_required_marks(p).strip("・･ ") for p in el_copy.get_text(strip=True).split(self._LABEL_BREAK)]
        parts = [p for p in parts if p]
        if len(parts) >= 2 and all(self._LABEL_PART_RE.fullmatch(p) for p in parts):
            return "\n".join(parts)
        return self._remove_required_marks("".join(parts))

    # 確認用の再入力欄の項目名(「E-mail(確認用)」「メールアドレス（確認）」等)。
    # 「確認」を含む文字列はボタンの文言(「確認画面へ」等)として除くが、これらは入力項目
    _CONFIRM_FIELD_TEXT_RE = re.compile(r"(?:メール|mail|アドレス|パスワード|password).*確認|確認.*(?:メール|mail|アドレス|パスワード|password|再入力)", re.I)

    def _looks_like_button_text(self, text: str) -> bool:
        """抽出したラベルが、入力項目ではなくボタンの文言等か(送信・リセット・確認画面へ等)。"""
        if any(k in text for k in ("送信", "リセット", "Δ")):
            return True
        return "確認" in text and not self._CONFIRM_FIELD_TEXT_RE.search(text)

    # 「必須」「任意」のマークだけの文字列(バッジ要素の判定用)
    _MARK_ONLY_TEXT_RE = re.compile(rf"[※＊*]?\s*{_MARK_OPEN}?\s*{_MARK_WORDS}\s*{_MARK_CLOSE}?\s*[※＊*]?", re.I)

    def _is_mark_only_element(self, el: Tag) -> bool:
        """「必須」「任意」のバッジだけの要素か(<td><p class="must">必須</p></td>、<div class="required-badge">必須</div>等)。

        項目名と入力欄の間にこのような要素を挟むフォームでは、入力欄の直前の要素(セル)を
        項目名とみなすと空になり、探索が外側へ進んでフォーム冒頭の注意書きを全項目の
        項目名として拾ったり、項目名が取れなかったりしていた(実際に確認済み)。読み飛ばして
        その手前の要素を項目名の候補にする。
        """
        return not self._contains_fillable_field(el) and bool(self._MARK_ONLY_TEXT_RE.fullmatch(el.get_text(strip=True)))

    # ラベル内の<br>の位置を示す目印(テキストに現れない文字)
    _LABEL_BREAK = "\x00"
    # <br>で区切られた部分を、それぞれ独立した項目名とみなせる文字列。次のものは除く
    # (実際に確認済み: 「個人情報の<br>取り扱いについて」「メールアドレス<br>半角英数」「お問い合わせ<br>事項」)
    # - 括弧・注記で始まる補足、日本語を含まない部分、2文字以下の部分
    # - 助詞・読点で終わる(文の途中で改行している)部分
    # - 入力方法の案内(「半角」「〜文字以内」「選択してください」等)
    _LABEL_PART_RE = re.compile(r"(?![（(\[［【〔※＊*例])(?=.*[ぁ-んァ-ヶ一-龠])(?!.*(?:文字|半角|全角|選択|入力|まで|以内|ください|可))[^\n]{3,15}(?<![のをにはがでと、。,])")

    # 選択内容に応じて表示を切り替える、初期状態で非表示のブロックのclass
    _HIDDEN_BLOCK_CLASS_RE = re.compile(r"^(?:is-)?(?:hidden|hide)$|^d-none$|^display-none$", re.I)

    _HINT_ELEMENT_CLASS_RE = re.compile(r"exam|example|hint|note|annotation|caption|desc(?:ription)?", re.I)

    def _looks_like_hint_element(self, el: Tag) -> bool:
        """「入力例」「注釈」等のヒントテキストを表す要素かどうかを判定する。

        例: <span class="exam">【全角】例：〇〇株式会社</span>のような要素。
        class名にヒントらしきキーワードを含む場合、たとえテキストが非ASCII文字を
        含んでいても、本来のラベル(dt等)より優先してラベル候補にしてはいけない。
        """
        classes = " ".join(el.get("class", [])).lower()
        return bool(self._HINT_ELEMENT_CLASS_RE.search(classes))

    def _looks_like_navigation(self, el: Tag) -> bool:
        """ナビメニュー等(「構成」列の内容)を表す要素かどうかを判定する。

        フォームのコンテナがヘッダーやグローバルナビまで包んでいる場合、
        「直前の兄弟要素」をラベルとみなす探索がナビに届き、
        「ホーム会社概要事業内容」のようなメニュー文字列を問合せ項目として
        拾ってしまうため、ラベル候補から除外する。
        フォーム項目のラベルがリンクを複数含むことはまず無いため、
        リンク2つ以上もナビとみなす。
        """
        if el.name in ("nav", "header", "footer") or el.find(["nav", "header", "footer"]):
            return True
        return len(el.find_all("a")) >= 2

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

    def _contains_fillable_field(self, el: Tag) -> bool:
        """利用者が入力する項目(hidden等を除く)を内包するか判定する。

        <div class="head_box"><p>お名前</p><input type="hidden" name="info[your_name][head]"></div>
        のように、ラベル用の要素に項目名を送信するためのhidden inputが同居している
        フォームがある。hiddenまで「入力欄を含む」と数えるとラベル候補から外れてしまい、
        探索が上位へ進んで無関係な文字列(下記の非表示要素等)を拾ってしまう。
        """
        return any(self._is_fillable_field(inp) for inp in el.find_all(["input", "textarea", "select"]))

    @staticmethod
    def _is_hidden_element(el: Tag) -> bool:
        """style="display:none"やhidden属性で画面に表示されない要素か判定する。

        例: yubinbango.js用の<span class="p-country-name" style="display:none;">Japan</span>
        がフォーム先頭にあり、これを問合せ項目として拾ってしまっていた。
        """
        if el.has_attr("hidden") or str(el.get("aria-hidden", "")).lower() == "true":
            return True
        style = re.sub(r"\s+", "", str(el.get("style", ""))).lower()
        return "display:none" in style or "visibility:hidden" in style

    def _is_in_hidden_block(self, inp: Tag, form: Tag) -> bool:
        """入力欄が、フォーム内の初期状態で非表示のブロック(style/hidden属性/class="is-hidden"等)の中にあるか。"""
        for parent in inp.parents:
            if parent is form or not isinstance(parent, Tag):
                return False
            if self._is_hidden_element(parent) or any(self._HIDDEN_BLOCK_CLASS_RE.match(c) for c in parent.get("class") or []):
                return True
        return False

    def _choice_option_label_in_shared_cell(self, inp: Tag, cell: Tag, soup: BeautifulSoup) -> str:
        """セル(td)の見出しを手前のテキスト欄が使っていて、選択肢(radio/checkbox)が見出し無しで
        同居している場合に、その選択肢自身の名前(<label>の文字列)を返す。該当しなければ空文字。"""
        # セル内の最初の入力欄がテキスト欄(=見出しはそちらの項目名)の場合に限る
        fields = [el for el in cell.find_all(["input", "textarea", "select"]) if self._is_fillable_field(el)]
        if not fields or fields[0] is inp or str(fields[0].get("type", "text")).lower() in ("radio", "checkbox"):
            return ""
        label = inp.find_parent("label")
        if label is None and inp.get("id"):
            found = soup.find("label", attrs={"for": inp.get("id")})
            label = found if isinstance(found, Tag) else None
        if label is None:
            return ""
        text = self._remove_required_marks(label.get_text(strip=True))
        return text if 0 < len(text) < 50 else ""

    def _choice_group_container(self, inp: Tag, form: Tag) -> Tag | None:
        """radio/checkboxについて、同じnameの選択肢をすべて含む最も内側の祖先要素を返す。

        選択肢がさらに小見出し(<h3>特別養護老人ホーム</h3>等)でグループ分けされている
        場合、個々の選択肢から探索すると小見出しの方を項目名として拾ってしまう。
        グループ全体の容器から探索を始めることで、本来の項目名(「事業選択」等)に届く。
        """
        name = inp.get("name")
        if not name:
            return None
        siblings = [el for el in form.find_all("input", attrs={"name": name}) if el is not inp]
        if not siblings:
            return None
        for ancestor in inp.parents:
            if ancestor is form or not isinstance(ancestor, Tag):
                return None
            if all(ancestor in sib.parents for sib in siblings):
                return ancestor
        return None

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

    def _retry_if_path_dropped(
        self,
        client: httpx.Client,
        requested_url: str,
        response: httpx.Response,
        headers: dict[str, str],
    ) -> httpx.Response:
        """www有無違いのホストへのリダイレクトでパスが捨てられた場合、元のパスで取り直す。

        実際に確認済み: "https://example.co.jp/contact/"だけが
        "http://www.example.co.jp/"(トップページ)へ301リダイレクトされ、パスが
        失われるサイトがある(他のページはwww無しでもそのまま表示される)。
        リダイレクト先のトップページは巡回済みとして読み飛ばされるため、
        お問い合わせページに一度も到達できずL列が空欄になっていた。
        リダイレクト先のホストで元のパスを取得し直し、トップページ以外の
        ページが返ってきた場合のみそちらを採用する。
        """
        if not response.history:
            return response

        requested = urlparse(requested_url)
        final = urlparse(str(response.url))
        if requested.path in ("", "/") or final.path not in ("", "/") or final.query:
            return response

        def _host(netloc: str) -> str:
            return re.sub(r"^www\.", "", netloc.split(":")[0].lower())

        # 別ドメインへの転送(移転等)は対象外。www有無の違いだけの場合に限る
        if final.netloc.lower() == requested.netloc.lower() or _host(final.netloc) != _host(requested.netloc):
            return response

        retry_url = final._replace(path=requested.path, query=requested.query).geturl()
        try:
            retry_response = client.get(retry_url, headers=headers)
        except httpx.RequestError:
            return response
        if retry_response.status_code != 200 or urlparse(str(retry_response.url)).path in ("", "/"):
            return response
        return retry_response

    def _fetch_sitemap_page_urls(self, client: httpx.Client, site_url: str) -> list[str]:
        """サイトのsitemap.xmlに載っているページURLの一覧を返す。

        robots.txtのSitemap:行と、一般的な配置場所(/sitemap.xml等)を順に試し、
        最初に読めたサイトマップを使う。サイトマップインデックスの場合は
        子サイトマップを最大MAX_CHILD_SITEMAPS件まで読む(それより深くは辿らない)。
        """
        parsed = urlparse(site_url)
        root = f"{parsed.scheme}://{parsed.netloc}"

        candidates: list[str] = []
        try:
            robots = client.get(f"{root}/robots.txt")
            if robots.status_code == 200:
                candidates.extend(re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots.text))
        except httpx.RequestError:
            pass
        candidates.extend(f"{root}{path}" for path in ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml"))

        for sitemap_url in dict.fromkeys(candidates):
            kind, locs = self._read_sitemap(client, sitemap_url)
            if kind == "urlset":
                return locs
            if kind == "sitemapindex":
                page_urls: list[str] = []
                for child_url in locs[: self.MAX_CHILD_SITEMAPS]:
                    child_kind, child_locs = self._read_sitemap(client, child_url)
                    if child_kind == "urlset":
                        page_urls.extend(child_locs)
                return page_urls
        return []

    def _read_sitemap(self, client: httpx.Client, url: str) -> tuple[str, list[str]]:
        """サイトマップを取得し、(種類("urlset"/"sitemapindex"/""), <loc>の一覧)を返す。

        SPA等では存在しないパスにもHTMLを200で返すことがあるため、
        ステータスコードではなくXMLのルート要素の有無で判定する。
        """
        try:
            response = client.get(url)
        except httpx.RequestError:
            return "", []
        if response.status_code != 200:
            return "", []
        text = response.text
        locs = [html_lib.unescape(loc.strip()) for loc in re.findall(r"<loc>\s*(.*?)\s*</loc>", text, re.IGNORECASE | re.DOTALL)]
        if re.search(r"<sitemapindex[\s>]", text, re.IGNORECASE):
            return "sitemapindex", locs
        if re.search(r"<urlset[\s>]", text, re.IGNORECASE):
            return "urlset", locs
        return "", []

    def _find_external_contact_links(self, soup: BeautifulSoup, page_url: str, base_domain: str) -> list[str]:
        """ページ内の、別ドメインにあるお問い合わせページへのリンクURLを返す。

        URLに"contact"等を含むか、リンクテキストが「お問い合わせ」等のものを対象にする。
        調査対象サイト自身(www有無・共用SSLホストを含む)へのリンクは通常の巡回で
        処理されるため除外する。
        """
        base = re.sub(r"^www\.", "", base_domain.split(":")[0].lower())
        urls: list[str] = []
        for a in soup.find_all("a", href=True):
            href = str(a["href"]).strip()
            if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
                continue
            parsed = urlparse(urljoin(page_url, href))
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                continue
            host = re.sub(r"^www\.", "", parsed.netloc.split(":")[0].lower())
            if host == base or self._is_shared_ssl_alias(host, base):
                continue
            if self.EXTERNAL_CONTACT_URL_PATTERN.search(parsed.netloc + parsed.path) or self.EXTERNAL_CONTACT_TEXT_PATTERN.search(a.get_text(" ", strip=True)):
                urls.append(parsed._replace(fragment="").geturl())
        return urls

    def _extract_external_contact_form(self, client: httpx.Client, urls: list[str]) -> tuple[str, bool]:
        """別ドメインにあるお問い合わせページを取得し、フォームの項目と添付機能の有無を返す。

        リンク元(URL・リンクテキスト)でお問い合わせページと分かっているため、
        本文欄があればお問い合わせフォームとして採用する(is_contact_page=True)。
        """
        for url in urls:
            try:
                response = client.get(url)
            except httpx.RequestError:
                continue
            if response.status_code != 200:
                continue
            html = self._decode_response(response)
            fields, has_attachment = self._extract_form_fields(html, str(response.url), client, depth=0)
            if self._looks_like_genuine_contact_form(BeautifulSoup(html, HTML_PARSER), fields, is_contact_page=True):
                logger.info("別ドメインのお問い合わせページからフォームを取得しました: %s", response.url)
                return fields, has_attachment
        return "", False

    def _looks_like_genuine_contact_form(
        self,
        soup: BeautifulSoup,
        fields_text: str,
        is_contact_page: bool = False,
    ) -> bool:
        """抽出結果をお問い合わせフォームとして確定してよいか判定する。

        本文欄(textarea)があることを必須条件にする。メール欄はログインフォーム
        や会員登録フォームにもあるため決め手にしない。一方、本文欄はブログの
        コメント欄にもあるため、コメントフォームは除外する。

        URL自体がお問い合わせページ(is_contact_page)の場合は、本文欄があれば
        採用する。トップページ等の「お問い合わせリンクがあるだけ」のページでは、
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

            # 本文欄(textarea)の無いフォームは、ログイン・検索・会員登録等とみなす
            if not container.find("textarea"):
                continue

            # パスワード欄を持つフォームはログイン・会員登録用
            if container.find("input", {"type": "password"}):
                continue

            # ブログのコメント欄(WordPressの#commentform等)は名前・メール・本文を
            # 持つため、お問い合わせフォームと区別して除外する
            if self._is_comment_form(container):
                continue

            if is_contact_page:
                return True

            # name / id / placeholder も問い合わせ判定材料にする
            input_text_parts: list[str] = []

            for inp in inputs:
                for attr in ("name", "id", "placeholder", "aria-label"):
                    value = inp.get(attr)
                    if value:
                        input_text_parts.append(str(value).lower())

            input_metadata = " ".join(input_text_parts)

            contact_word_count = sum(1 for word in contact_words if word.lower() in container_text or word.lower() in input_metadata)

            # 問い合わせ関連語がフォーム自身に複数存在する場合
            if contact_word_count >= 2:
                return True

            # 問い合わせフォームではメール欄がなくても、
            # 「氏名 + 電話番号 + 内容」等の構成になっている場合がある。
            has_name = any(word in container_text or word in input_metadata for word in ("お名前", "氏名", "name"))
            has_phone = any(word in container_text or word in input_metadata for word in ("電話番号", "電話", "tel", "phone"))

            if has_name and has_phone:
                return True

        return False

    def _is_comment_form(self, container: Tag) -> bool:
        """ブログ等のコメント投稿フォームか判定する。"""
        if "wp-comments-post" in str(container.get("action") or "").lower():
            return True
        for el in [container, *container.find_parents(limit=3)]:
            names = " ".join([*(el.get("class") or []), str(el.get("id") or "")]).lower()
            if "commentform" in names or "comment-form" in names or "comment-respond" in names:
                return True
        return False

    @staticmethod
    def _is_fillable_field(inp: Tag) -> bool:
        """hidden・送信ボタン等ではない、利用者が入力する項目か判定する。"""
        t_val = inp.get("type", "text")
        itype = "".join([str(x) for x in t_val]).lower().strip() if isinstance(t_val, list) else str(t_val).lower().strip()
        return itype not in ("hidden", "submit", "button", "image", "reset")

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
            # 送信ボタンだけを包むdiv(例: <div class="btnForm"><input type="image"></div>)は
            # 入力項目を持たないので疑似フォームとはみなさない
            if not any(self._is_fillable_field(inp) for inp in candidate.find_all(["input", "textarea", "select"])):
                continue
            if candidate.find("form") is not None:
                continue
            if any(candidate in already.descendants for already in added_divs):
                continue
            added_divs.append(candidate)
        raw_containers.extend(added_divs)

        # <form>要素も疑似フォームのdivも無く、JSで送信する裸の入力欄だけが
        # 並んでいるページ(実際に確認済み: <div id="section01"><dl><dt>お名前</dt>
        # <dd><input></dd></dl>...)に対応する。<body>全体をコンテナにすると
        # ナビメニューまで拾ってしまうため、本文欄(textarea)から祖先を遡り、
        # 他の入力欄を2つ以上含む最初の要素だけをフォーム相当として扱う。
        if not any(c.find("textarea") for c in raw_containers):
            textarea = soup.find("textarea")
            if isinstance(textarea, Tag):
                for level, ancestor in enumerate(textarea.parents):
                    if level >= self.MAX_BARE_FORM_ANCESTOR_LEVELS or not isinstance(ancestor, Tag) or ancestor.name in ("body", "html"):
                        break
                    other_inputs = [inp for inp in ancestor.find_all(["input", "select"]) if self._is_fillable_field(inp)]
                    if len(other_inputs) >= 2:
                        raw_containers.append(ancestor)
                        break

        # 以前は<form>も疑似フォームのdivも無いページで<body>全体を1つの
        # コンテナとして扱っていたが、ラベル探索がページ全体に広がり、
        # ヘッダーのナビメニュー(「構成」列の内容)を問合せ項目として拾って
        # しまうため廃止した。

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
                # 選択内容に応じてJavaScriptで表示を切り替える分岐(<table class="branch is-hidden">等)の
                # 中の入力欄は、初期表示では見えない。依頼内容ごとの全分岐の項目まで並んでいた(実際に確認済み)
                if self._is_in_hidden_block(inp, form):
                    continue

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

                    # radio/checkboxは選択肢グループ全体の容器から探索する
                    if is_choice_input:
                        group = self._choice_group_container(inp, form)
                        if group is not None and group in parent_chain:
                            parent_chain = parent_chain[parent_chain.index(group) :]

                    # (a) dt/dd, th/td
                    for parent in parent_chain:
                        if parent.name == "dd":
                            prev_dts = [t for t in parent.find_previous_siblings("dt") if isinstance(t, Tag)]
                            if prev_dts:
                                # resolved_text = self._remove_required_marks(prev_dts[0].get_text(strip=True))
                                resolved_text = self._get_clean_element_text(prev_dts[0])
                                break

                        if parent.name == "td":
                            prev_ths = [t for t in parent.find_previous_siblings("th") if isinstance(t, Tag) and not self._is_mark_only_element(t)]
                            if prev_ths:
                                # <td><input name="kana"><div><label><input type="radio">協力会の会員</label>…</div></td>
                                # のように、見出し(th)をテキスト欄が使い、見出しの無い選択肢が同じセルに
                                # 同居している場合は、選択肢名そのものを項目にする(実際に確認済み)
                                option_label = self._choice_option_label_in_shared_cell(inp, parent, soup) if is_choice_input else ""
                                # resolved_text = self._remove_required_marks(prev_ths[0].get_text(strip=True))
                                resolved_text = option_label or self._get_clean_element_text(prev_ths[0])
                                break

                            # <th>を使わず、同じ行内の手前の<td>をラベルとして使う
                            # 古い表組みパターンに対応する
                            # (例: <tr><td class="label">氏名</td><td><input ...></td></tr>)。
                            # ラベル用の<td>は入力欄を含まないはずなので、それを条件に
                            # 別の項目の入力セルを誤って拾わないようにする。
                            prev_tds = [t for t in parent.find_previous_siblings("td") if isinstance(t, Tag) and not self._is_mark_only_element(t)]
                            if (
                                prev_tds
                                and not self._contains_fillable_field(prev_tds[0])
                                and not self._is_hidden_element(prev_tds[0])
                                and not self._looks_like_hint_element(prev_tds[0])
                                and not self._looks_like_navigation(prev_tds[0])
                            ):
                                # t = self._remove_required_marks(prev_tds[0].get_text(strip=True))
                                t = self._get_clean_element_text(prev_tds[0])
                                if t and len(t) < 50 and re.search(r"[A-Za-zぁ-んァ-ヶー一-龠々]", t):
                                    resolved_text = t
                                    break

                        # (a-2) 入力欄自身の直前の兄弟要素がラベルらしい要素の場合
                        # 例: <div class="contact__item"><p class="contact__label">お名前</p><input ...></div>
                        if not resolved_text:
                            own_siblings = [sib for sib in inp.find_previous_siblings(["p", "label", "span", "div", "dt", "th"]) if isinstance(sib, Tag) and not self._is_mark_only_element(sib)]
                            if own_siblings and isinstance(own_siblings[0], Tag):
                                candidate = own_siblings[0]
                                if (
                                    not self._contains_fillable_field(candidate)
                                    and not self._is_hidden_element(candidate)
                                    and not self._looks_like_hint_element(candidate)
                                    and not self._looks_like_navigation(candidate)
                                ):
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

                            # 見出しタグ(<h3>お名前</h3><p><input></p>のような、
                            # ブロックエディタで組まれたフォーム)も対象にする。また、
                            # 間に挟まった空の要素(<p><label></label></p>等)は読み飛ばす。
                            siblings = [
                                sib
                                for sib in parent.find_previous_siblings(["div", "span", "label", "dt", "th", "p", "table", "h2", "h3", "h4", "h5", "h6"])
                                if isinstance(sib, Tag) and (sib.get_text(strip=True) or self._contains_fillable_field(sib)) and not self._is_mark_only_element(sib)
                            ]
                            if siblings:
                                candidate = siblings[0]
                                if (
                                    not self._contains_fillable_field(candidate)
                                    and not self._is_hidden_element(candidate)
                                    and not self._looks_like_hint_element(candidate)
                                    and not self._looks_like_navigation(candidate)
                                ):
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
                                    legend_text = "".join(str(c) if isinstance(c, NavigableString) else c.get_text() for c in legend.children if not (isinstance(c, Tag) and c.name == "i")).strip()
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

                # "Δ"は、WordPressの「Contact Form 7」プラグインがアクセシビリティ用に
                # 自動挿入するアンカー記号(スクリーンリーダー向けの目印で、見た目には
                # 表示されないことが多い)。この記号自体を持つinput/textarea/select要素の
                # ラベルとして誤って抽出されてしまうことがあり、実際の入力項目とは
                # 無関係なので除外する。
                if resolved_text and not self._looks_like_button_text(resolved_text):
                    # 「都道府県<br>市区郡・町村」のように改行で並べた複数の項目名は別々の項目にする
                    for part in resolved_text.split("\n"):
                        if part not in form_fields:
                            form_fields.append(part)

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
            "SSL証明書エラー（期限切れ等）によりセキュリティ機器がアクセスをブロックしたため、実際のサイト内容を取得できませんでした。別ネットワークからの再調査、または手動確認をお願いします。",
        ),
        (
            # 「Web Page Blocked!」型のFortiGate標準ブロックページ用。
            # カテゴリ名（フィッシング等）は _extract_fortinet_category() で別途抽出し、
            # 検知できた場合はこのデフォルト文言より詳細な理由文で上書きする。
            ["web page blocked"],
            "ネットワーク側のセキュリティ機器（Fortinet等）によりアクセスがブロックされました。実際のサイト内容を取得できませんでした。別ネットワークからの再調査、または手動確認をお願いします。",
        ),
    ]

    # ネットワーク機器ではなく、アクセス先のサーバー自身がボット対策等で返してくる
    # 汎用的なエラーページ（Apache/Nginxの標準403ページ等）の検知用シグネチャ。
    # 「Forbidden / You don't have permission to access this resource.」のように、
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
        (実際に確認済み: トップページが同一ドメイン内の実コンテンツURLへ
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

        # 「Forbidden / You don't have permission to access this resource.」のように、
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
        負うため、あえて注記に留める方針とした)。
        """
        if not start_url.startswith(("http://", "https://")):
            primary_url = f"https://{start_url}"
            fallback_url = f"http://{start_url}"
        else:
            primary_url = start_url
            fallback_url = start_url.replace("https://", "http://") if start_url.startswith("https://") else ""

        base_domain_clean = self._get_clean_domain(primary_url)

        # bare(www無し)ドメインだけSSL証明書の対象がwwwに限定されていたり(SNI不一致)、
        # サーバー側のバーチャルホスト振り分けがwww付きのリクエストしか正しく認識せず
        # bareには404を返したりするサイトがある(実際に確認済み: bareのhttpsは
        # verify_ssl=Falseで接続自体はできてもサーバーが404を返す専用ホストにしか
        # 到達できず、bareのhttpもそのbareのhttpsへ301リダイレクトして同じ404に
        # 行き着く。一方www付きのhttpsだけは正常に200でサイト本体へ到達できる)。
        # primary_url/fallback_url(どちらもbare)が両方失敗した場合の最終手段として、
        # www付きのURLも試す。
        www_fallback_urls: list[str] = []
        if not base_domain_clean.lower().startswith("www."):
            www_fallback_urls = [f"https://www.{base_domain_clean}", f"http://www.{base_domain_clean}"]
        start_time = time.time()

        visited: set[str] = set()
        # 「訪問した(リダイレクト前の)URL」とは別に、「リダイレクト後の実際のコンテンツURL」を
        # 正規化した形で記録する集合。href="ur"のようなルート相対のつもりで書かれた素の
        # 相対リンクが、ページごとに異なる基準(/nt/, /am/, /se/等)で解決されて
        # "/nt/ur", "/am/ur"...という別々の(実在しない)URLを生み、WordPress側の
        # 「近いスラッグへの自動リダイレクト」機能で結局同じ実ページ(/ur/)に
        # 302/301で着地する、というケースがあった(実際に確認済み)。この場合
        # visitedはリダイレクト前のURL単位でユニークになってしまい、内容が
        # 完全に同じページが何件も別ページとしてカウントされてしまう。
        # リダイレクト後の正規化URLが既にこの集合にある場合は、今回の訪問を
        # 「新しいページ」としてカウントしない。
        canonical_visited: set[str] = set()
        queued_urls: set[str] = set()
        queue: deque[tuple[str, int]] = deque()
        # 本巡回でタイムアウトしたページ(追加巡回で取り直す)
        timed_out_urls: list[str] = []
        is_over_100 = False

        max_depth = 0
        contact_fields = ""
        # お問い合わせフォームが無いサイトで、L列に"mailto"と出すための目印
        has_mailto_link = False
        contact_page_renders = 0
        # 別ドメイン(フォーム専用サービス・共用SSL等)にあるお問い合わせページのURL
        external_contact_urls: list[str] = []
        # 「問い合わせフォームなし」と断定できない手がかり(contact_form_uncertainの判定用)
        contact_unread_pages: list[str] = []
        contact_link_keys: set[str] = set()
        has_form_service_link = False
        self.contact_form_uncertain = False
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
            # 正規化され、別ページとして二重カウントされてしまっていた(実際に確認済み)。
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
            clean_query = self._clean_query(parsed.query)
            # 注意: ここではネットロック(www有無)・スキーム(http/https)ともに
            # 元のまま保持する。この関数の戻り値はそのままキューに積まれ、
            # 実際にclient.get()でリクエストされるURLになる。
            #
            # 以前はここでnetlocから"www."を強制除去していた(www有無の両方で
            # 内部リンクが書かれているサイトでの二重カウント対策)。
            # しかしこれは、href="/contact/"のような相対リンクを
            # urljoin()で絶対URL化した結果まで巻き込んでwwwを剥がしてしまう。
            # www無しの裸ドメインが単純なドメイン転送専用(パスを保持せず
            # トップページへ丸めてリダイレクトするだけ)になっているサイト
            # サイトでは、相対リンクで書かれた実在するページへの
            # リンクがことごとく裸ドメイン向けに書き換えられてしまい、その結果
            # 毎回トップページへ301/302で送り返されて「発見したリンクがどれも
            # 既訪問のトップページに丸め込まれる」状態になり、ほぼ全ページが
            # 巡回できなくなっていた。
            #
            # http/https スキームについても同様の理由(https非対応のサイトで
            # 発見した内部リンクが軒並みhttpsに書き換えられ、全てhttpx.RequestErrorで
            # 失敗して(except節でログも残らず握りつぶされるため気づきにくい)
            # トップページ1件しか巡回できなくなる重大な回帰を起こしていた)で、
            # 既に実URLへの書き換えはしない方針になっている。
            # www有無・http/https混在サイトでの二重カウント対策は、実URLではなく
            # 重複判定専用のキーである
            # _dedup_key() 側でのみ行う。
            return parsed._replace(path=clean_path, query=clean_query, fragment="").geturl()

        def _dedup_key(u: str) -> str:
            """visited/queued_urls/canonical_visitedの重複判定にのみ使うキーを作る。

            normalize_url()の戻り値(実際にリクエストするURL)とは別に、
            スキーム(http/https)を常にhttpsへ、ネットロックのwww有無を
            常に無し(www.除去)へ統一した文字列を返す。http://とhttps://の
            両方が有効なサイトや、www有無の両方で内部
            リンクが書かれているサイトで、同じページが
            異なる生URLとして発見されても、この関数を通した比較・登録では
            同一ページとして扱われ、二重カウントされない。
            実際のリクエストにはこの関数の戻り値を使ってはならない
            (www有無を無条件で書き換えると、www無しの裸ドメインが単純な
            ドメイン転送専用になっているサイトで実在するページへのリンクが
            軒並み壊れるため。normalize_url()側の
            コメントも参照)。
            """
            p = urlparse(u)
            scheme = "https" if p.scheme in ("http", "https") else p.scheme
            netloc = re.sub(r"^www\.", "", p.netloc, flags=re.IGNORECASE)
            # 共用SSLホスト(例: etp-jp-com.prm-ssl.jp)は調査対象ドメインと同じサイトの
            # 別名なので、同じキーにまとめて二重カウントを防ぐ
            if self._is_shared_ssl_alias(netloc, base_domain_clean):
                netloc = re.sub(r"^www\.", "", base_domain_clean, flags=re.IGNORECASE)
            return p._replace(scheme=scheme, netloc=netloc).geturl()

        # ナビゲーション等のリンクが href="privacy/" のような素の相対パスで書かれた
        # サイトでは、ページを開くたびに "/elements/privacy/", "/company/privacy/"...
        # という実在しないURLが生まれ、WordPressの「近いスラッグへの自動リダイレクト」で
        # 結局 "/privacy/" に着地する(実際に確認済み: リクエストの4割以上がこの
        # リダイレクトで、応答の遅いサーバーでは時間予算を使い切ってページ数が
        # 確定しなかった)。着地先は既訪問として数えられないため集計結果は正しいが、
        # 1ページごとにナビ項目の数だけ無駄なリクエストが発生する。
        # 着地先はルート直下とは限らず、"/contact/forged_steel_valves/" が
        # "/product_introduction/forged_steel_valves/" のように別階層の実ページへ
        # 転送されることもある(実際に確認済み: ナビ約25項目がすべて素の相対リンクの
        # サイトで、追加巡回に600秒かけてもページ数が確定しなかった)。
        # 「最後のパス要素が同じまま別のパスへ転送された」リダイレクトを一度観測したら、
        # 最後のパス要素がそれと同じで、着地先とは異なるパスのURLはそれ以降巡回候補にしない。
        # ただし対象は href="privacy/" のような素の相対リンク(先頭が"/"でもスキームでも
        # ないもの)から生まれたURLに限る。"/contact-shinko/industry" のように別階層に
        # 同名の実ページがあるサイトで、"/"始まりや絶対URLで書かれた正規のリンクまで
        # 除外してしまい、ページ数を過少に数えていた(実際に確認済み)。
        misresolved_segments: dict[str, str] = {}  # 最後のパス要素 -> 着地先のパス
        # 素の相対リンクから生まれたURLと、それ以外("/"始まり・絶対URL等)のリンクから
        # 見つかったURL(いずれも_dedup_keyの値)。後者でも見つかったURLは正規の
        # リンク先とみなし、除外しない。
        bare_relative_keys: set[str] = set()
        explicit_link_keys: set[str] = set()

        def _is_misresolved_relative_link(url: str) -> bool:
            key = _dedup_key(url)
            if key not in bare_relative_keys or key in explicit_link_keys:
                return False
            path = urlparse(url).path.rstrip("/")
            last_segment = path.rsplit("/", 1)[-1]
            landing_path = misresolved_segments.get(last_segment)
            return landing_path is not None and path != landing_path

        def _slugify_path(path: str) -> str:
            """WordPressがスラッグを作るときと同様に、英数字以外を"-"に寄せたパスを返す。

            静的サイトからWordPressへ移行したサイトでは、旧URLの"privacy.html"が
            スラッグ"privacy-html"のページになっている(実際に確認済み)。
            """
            segments = [re.sub(r"[^a-z0-9%]+", "-", s.lower()).strip("-") for s in path.split("/") if s]
            return "/" + "/".join(segments)

        # サイトの引っ越しで残った旧ディレクトリへのリンク("/wp/flow/" → "/flow/",
        # "/cs2020/services/" → "/services/" 等)は、毎回リダイレクトで既訪問の
        # ページに着地するだけだが、応答の遅いサーバーでは1件ごとに数秒を費やし、
        # 時間予算を使い切ってページ数が確定しない原因になっていた(実際に確認済み:
        # 86リクエスト中23件がこの種のリダイレクト)。「パス末尾は同じまま先頭の
        # ディレクトリだけが置き換わる」リダイレクトを一度観測したら、以降は同じ
        # 旧ディレクトリ配下のURLの転送先を推定し、それが既訪問・巡回予定であれば
        # リクエスト自体を省く。転送先が未知のURLは従来どおり取得する(ページ数の
        # 数え方は変えない)。
        redirect_prefix_aliases: dict[str, str] = {}  # 旧ディレクトリ -> 転送先ディレクトリ

        def _learn_redirect_prefix(requested_path: str, final_path: str) -> None:
            requested_segments = [s for s in requested_path.split("/") if s]
            final_segments = [s for s in final_path.split("/") if s]
            common = 0
            while common < min(len(requested_segments), len(final_segments)) and requested_segments[-1 - common] == final_segments[-1 - common]:
                common += 1
            if common == 0:
                return
            old_prefix = requested_segments[: len(requested_segments) - common]
            new_prefix = final_segments[: len(final_segments) - common]
            # 旧ディレクトリが無い("/satei/" → "/services/satei/")場合に学習すると、
            # 全URLが転送対象と誤認されるため対象外
            if not old_prefix or old_prefix == new_prefix:
                return
            redirect_prefix_aliases.setdefault("/" + "/".join(old_prefix), "/" + "/".join(new_prefix) if new_prefix else "")

        def _is_known_redirect_alias(url: str) -> bool:
            parsed = urlparse(url)
            path = parsed.path
            for old_prefix, new_prefix in redirect_prefix_aliases.items():
                if not path.startswith(old_prefix + "/"):
                    continue
                rest = path[len(old_prefix) :]
                if rest.strip("/") == "":
                    continue
                alias_key = _dedup_key(normalize_url(parsed._replace(path=new_prefix + rest).geturl()))
                if alias_key in visited or alias_key in queued_urls or alias_key in canonical_visited:
                    return True
            return False

        canonical_link_re = re.compile(r"<link\b[^>]*\brel=[\"']?canonical\b[^>]*>", re.IGNORECASE)
        href_attr_re = re.compile(r"\bhref=[\"']([^\"']+)[\"']", re.IGNORECASE)

        def _page_count_key(page_html: str, resolved_url: str) -> str:
            """ページ数の集計(canonical_visited)に使うキーを返す。

            WordPressは"/privacy.html/"と"/privacy-html/"のようにスラッグの表記揺れを
            両方とも200で表示し、rel=canonicalで後者を正規URLとして示すことがある
            (実際に確認済み)。canonicalがスラッグ表記揺れだけの違いを示している
            場合に限り、canonical側のURLで数えて二重カウントを防ぐ。全ページの
            canonicalがトップページを指すような設定ミスのサイトもあるため、それ以外の
            canonicalは使わない。
            """
            resolved_key = _dedup_key(normalize_url(resolved_url))
            link_match = canonical_link_re.search(page_html)
            href_match = href_attr_re.search(link_match.group(0)) if link_match else None
            if not href_match:
                return resolved_key
            canonical_url = normalize_url(urljoin(resolved_url, html_lib.unescape(href_match.group(1).strip())))
            canonical_key = _dedup_key(canonical_url)
            if canonical_key == resolved_key:
                return resolved_key
            if urlparse(canonical_key).netloc != urlparse(resolved_key).netloc:
                return resolved_key
            if _slugify_path(urlparse(canonical_key).path) != _slugify_path(urlparse(resolved_key).path):
                return resolved_key
            # 正規URL側は取得しなくてよい(取得しても同じページ)
            visited.add(canonical_key)
            return canonical_key

        def _is_bare_relative_href(href: str) -> bool:
            return not re.match(r"^\s*(/|[a-zA-Z][a-zA-Z0-9+.-]*:|#|\?)", href)

        def enqueue(url: str, path_depth: int, priority: bool, bare_relative: bool = False) -> None:
            key = _dedup_key(url)
            if bare_relative:
                bare_relative_keys.add(key)
            else:
                explicit_link_keys.add(key)
            if key in visited or key in queued_urls or len(canonical_visited) >= 100:
                return
            if _is_misresolved_relative_link(url) or _is_known_redirect_alias(url):
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

                def _try_initial_fetch(url: str) -> Exception | None:
                    """指定URLへの初回アクセスを1回試みる。

                    成功時(ブロックページ検知を含む)はfirst_url/first_html/
                    blocked_reason/has_basic_authを更新してNoneを返す。例外発生時は
                    握りつぶさずそのまま返す(呼び出し側で次候補へのフォールバック
                    判断や、最終的なエラー分類に使うため)。
                    """
                    nonlocal first_url, first_html, blocked_reason, has_basic_auth
                    try:
                        response = client.get(url)
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
                        return None
                    except Exception as e:
                        return e

                primary_error = _try_initial_fetch(primary_url)
                if not first_url and fallback_url:
                    fallback_error = _try_initial_fetch(fallback_url)
                # bare(www無し)のhttps/httpが両方失敗した場合、www付きも試す
                # (www_fallback_urlsの説明を参照)。
                for www_url in www_fallback_urls:
                    if first_url:
                        break
                    fallback_error = _try_initial_fetch(www_url) or fallback_error

                # httpx側でブロックページらしきものを検知していても(blocked_reasonが
                # 非空でも)、ここでは即断せずPlaywright(実ブラウザ)での再確認を試みる。
                # Fortinet Webfilter等のセキュリティ機器が、ブラウザからの通常アクセス
                # ではなく本ツールのような自動化ツールのアクセスだけを狙い撃ちで
                # ブロックするケースが実際にあり(実際に確認済み)、httpx側の検知
                # だけで確定させると、人間なら普通に見られるサイトを誤って
                # 「アクセス不可」扱いにしてしまう。
                if not first_url and self.render_js:
                    # httpx側と同様、bareドメインが失敗する場合はwww付きも試す
                    # (実ブラウザでもbare側のSNI不一致・バーチャルホスト未設定に
                    # よる失敗は避けられないため)。
                    for render_url in [primary_url, *www_fallback_urls]:
                        rendered_html, rendered_url, rendered_status, nav_error_reason = self._fetch_rendered_html(render_url)
                        if rendered_html:
                            # Playwrightのgoto()はhttpxのraise_for_status()と違い、4xx/5xxでも
                            # 例外を投げずにエラーページのHTML本体をそのまま返してしまうため、
                            # ここでも同様にブロックページ検知を行う。
                            detected_block_reason = self._detect_access_blocked_page(rendered_html)
                            if not detected_block_reason and rendered_status is not None:
                                detected_block_reason = self._reason_for_status_code(rendered_status)

                            if detected_block_reason:
                                # このURLではブロックされた。他に試す候補が残っていれば
                                # 続行し、無ければ最終的に「アクセス不可」と確定する。
                                blocked_reason = detected_block_reason
                                continue

                            # 実ブラウザでは正常に取得できた(=httpx側の検知は
                            # 自動化ツール狙い撃ちの誤検知だった)ため、ブロック扱いを解除する。
                            blocked_reason = ""
                            first_url = rendered_url or render_url
                            first_html = rendered_html
                            first_html_from_playwright = True
                            break
                        elif nav_error_reason:
                            # ナビゲーション自体が失敗した(DNS失敗・接続拒否等で
                            # chrome-error://chromewebdata/に遷移した)ケース。
                            # このURLを移転先やfirst_urlとして絶対に使わないよう、
                            # ここでは何も採用せず、理由だけをblocked_reasonに残す。
                            # (以前はこれを「移転案内ページへのリダイレクト」と誤認し、
                            #  「すでにリニューアル済のため / 移転先：chrome-error://chromewebdata/」
                            #  という誤った結果になっていた)
                            blocked_reason = nav_error_reason

                    if not first_url and blocked_reason:
                        # 実ブラウザ(Playwright)でも(bare/www両方を試した上で)ブロック
                        # された場合のみ、最終的に「アクセス不可」と確定する。
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
                            blocked_reason,
                            "",
                            True,
                            "",
                        )

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

                # トップページ自体が"index.php"/"index.html"へのmeta refreshだけの
                # 薄いスタブになっているサイトへの対応(実際に確認済み)。
                # normalize_url()は"/index.php"等をルート"/"と同一視するよう
                # 正規化する("/"と"/index.html"が別ページとして
                # 二重カウントされていた問題の対策)。このため、meta refresh先が
                # 同一ディレクトリの"index.php"/"index.html"だった場合、正規化後は
                # 遷移元(トップページ自身)と全く同じURL扱いになり、通常の
                # enqueue()の「既訪問なら無視」ガードに阻まれて一度もリクエストされず、
                # スタブの中身(実質空)だけがトップページとして扱われてしまっていた。
                # このケースだけは特別に、通常のキュー経由ではなくここで直接
                # 遷移先を取得し、スタブの代わりに実際のコンテンツを採用する。
                self_redirect_target = self._extract_delayed_redirect_target(first_html, first_url)
                if self_redirect_target and _dedup_key(normalize_url(self_redirect_target)) == _dedup_key(normalize_url(first_url)):
                    try:
                        self_redirect_response = client.get(self_redirect_target, headers=self.headers)
                        if self_redirect_response.status_code == 200:
                            resolved_first_html = self._decode_response(self_redirect_response)
                            if resolved_first_html:
                                first_html = resolved_first_html
                                first_url = str(self_redirect_response.url)
                    except httpx.RequestError:
                        pass

                # normalize_url()は末尾スラッシュを削るため、WordPress等の「/about →
                # /about/ へ301」するサイトでは全ページで301→200の2往復が発生し、
                # 応答の遅いサーバーでは巡回時間の約半分をリダイレクトに費やしていた
                # (実際に確認済み: 186リクエスト中94件が末尾スラッシュ補完の301)。
                # 一度でもそのリダイレクトを観測したサイトでは、以降は拡張子の無い
                # パスに最初から末尾スラッシュを付けてリクエストする。付けた結果
                # 404になった場合は元のURLで取り直す。リクエストするURLを変える
                # だけで、重複判定・ページ数の数え方(normalize_url/_dedup_key)は変えない。
                #
                # ただし、サイト内の一部(例: 別システムの"/global/"配下)だけが末尾スラッシュ付きで、
                # 他は逆に"/torachan/" → "/torachan"へ末尾スラッシュを外す301を返すサイトもある
                # (実際に確認済み: 一度の観測で全ページにスラッシュを付け続けたため、以降の
                # ほぼ全リクエストが301→200の2往復になり、時間切れでページ数が確定しなかった)。
                # 付ける/外すの両方向のリダイレクトを数え、付ける方が多い間だけ付ける。
                trailing_slash_votes = 0

                def _get_page(url: str, headers: dict[str, str] | None = None, timeout: float | None = None) -> httpx.Response:
                    nonlocal trailing_slash_votes
                    target = url
                    parsed = urlparse(url)
                    if trailing_slash_votes > 0 and parsed.path and not parsed.path.endswith("/") and "." not in parsed.path.rsplit("/", 1)[-1]:
                        target = parsed._replace(path=parsed.path + "/").geturl()
                    response = client.get(target, headers=headers, timeout=timeout or self.page_timeout)
                    if target != url and response.status_code == 404:
                        response = client.get(url, headers=headers, timeout=timeout or self.page_timeout)
                    if response.history:
                        final = urlparse(str(response.url))
                        if final.query == parsed.query:
                            if final.path == parsed.path + "/":
                                trailing_slash_votes += 1
                            elif target != url and final.path == parsed.path:
                                # こちらで付けた末尾スラッシュを、サーバーが外して転送してきた
                                trailing_slash_votes -= 1
                        # 素の相対リンクの誤解決(misresolved_segmentsのコメント参照)の学習
                        requested_path = parsed.path.rstrip("/")
                        final_path = final.path.rstrip("/")
                        last_segment = final_path.rsplit("/", 1)[-1]
                        requested_last_segment = requested_path.rsplit("/", 1)[-1]
                        # "/contact-html/privacy.html" → "/privacy-html/" のように、着地先で
                        # スラッグ化されて最後のパス要素の表記が変わる場合も同じ誤解決とみなす
                        # (_slugify_pathのコメント参照。この学習が無いと、ページを開くたびに
                        # "/<各ページ>/<各ナビ項目>.html"の組み合わせが生まれ続け、キューが
                        # 空にならずページ数が確定しなかった)
                        if last_segment and requested_path != final_path and _slugify_path(requested_last_segment) == _slugify_path(last_segment):
                            misresolved_segments.setdefault(requested_last_segment, final_path)
                        if _dedup_key(final._replace(path="", query="", fragment="").geturl()) == _dedup_key(parsed._replace(path="", query="", fragment="").geturl()) and final.query == parsed.query:
                            _learn_redirect_prefix(requested_path, final_path)
                    return response

                queue.append((first_url, 0))
                queued_urls.add(_dedup_key(first_url))

                previous_url = ""
                sitemap_tried = False

                while True:
                    if not queue:
                        # リンクを辿り尽くしても数ページしか見つからなかった場合、
                        # ページ間の移動をJSで行っていて<a href>から辿れないサイトの
                        # 可能性がある(実際に確認済み: Canva製サイトはリンクが
                        # href="#page-2"のようなページ内リンクで、実URL"/page-2"へは
                        # JSで切り替えるため、トップページ1ページだけと判定されていた)。
                        # その場合に限り、sitemap.xmlに載っているURLで補完して巡回を続ける。
                        # 通常のサイトで常にサイトマップを使うと、リンクからは辿れない
                        # タグ一覧・投稿者ページ等まで数えてしまうため、補完に留める。
                        if sitemap_tried or len(canonical_visited) > self.SITEMAP_FALLBACK_MAX_PAGES:
                            break
                        sitemap_tried = True
                        for sitemap_url in self._fetch_sitemap_page_urls(client, first_url):
                            if self._is_valid_internal_link(first_url, sitemap_url, base_domain_clean):
                                norm_sitemap_url = normalize_url(urljoin(first_url, sitemap_url))
                                sitemap_path_depth = len([p for p in urlparse(norm_sitemap_url).path.split("/") if p])
                                enqueue(norm_sitemap_url, sitemap_path_depth, False)
                        if not queue:
                            break
                        logger.info("リンクから辿れるページが少ないため、sitemap.xmlのURL(%d件)で補完します: %s", len(queue), first_url)

                    if len(canonical_visited) >= 100:
                        is_over_100 = True
                        break
                    if time.time() - start_time > self.timeout:
                        break

                    current_url, depth = queue.popleft()
                    queued_urls.discard(_dedup_key(current_url))
                    norm_current = normalize_url(current_url)

                    if _dedup_key(norm_current) in visited or _is_misresolved_relative_link(norm_current) or _is_known_redirect_alias(norm_current):
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
                            try:
                                response = _get_page(current_url, req_headers)
                            except httpx.RequestError:
                                # https接続自体が失敗した(SSL証明書不備・ハンドシェイク
                                # 失敗等)場合、トップページ自体はhttpsで200が返っていても、
                                # サイトの他の部分は実質httpでしか機能していないことがある
                                # (実際に確認済み: トップページはhttpsで取得できるが
                                # 中身は"index.phpへmeta refresh"するだけの薄いページで、
                                # そのindex.phpへのhttps接続だけがSSLハンドシェイク失敗で
                                # 例外になり、他に発見できるリンクが無いために巡回対象が
                                # 丸ごと消え1ページ扱いになっていた)。初回アクセス時と同じ
                                # 考え方で、httpへのフォールバックを1回だけ試みる。
                                # (except節で握りつぶされて気づきにくくなることを避けるため、
                                # ここで明示的にフォールバックする)
                                if not current_url.startswith("https://"):
                                    raise
                                current_url = "http://" + current_url[len("https://") :]
                                response = _get_page(current_url, req_headers)
                            if response.status_code == 401:
                                has_basic_auth = True
                            if response.status_code != 200:
                                continue
                            response = self._retry_if_path_dropped(client, current_url, response, req_headers)
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
                        # canonical_visited(正規化済みの実URLの集合、スキーム・
                        # www有無は_dedup_key()で統一)だけを使い、visited(生URLの
                        # 重複リクエスト防止用)からは意図的に取り消さない。
                        # normalize_url()自体はhttp/httpsのスキームやwww有無を
                        # 書き換えない(実際のリクエストに使うURLを勝手に書き換えると、
                        # https非対応のサイトで接続が軒並み失敗したり、
                        # www無しの裸ドメインがドメイン転送専用でパスを保持しない
                        # サイトでリンクが軒並み壊れたりする
                        # 重大な回帰になることがあったため)。そのため重複判定専用の
                        # _dedup_key()でのみスキーム・www有無を揃える。
                        norm_resolved = normalize_url(resolved_url)
                        resolved_key = _page_count_key(current_html, norm_resolved)
                        if resolved_key in canonical_visited and resolved_key != _dedup_key(norm_current):
                            continue
                        canonical_visited.add(resolved_key)

                        # httpxで取得済みのHTMLが静的（JSフレームワーク未使用）な場合、わざわざ
                        # Playwrightで再レンダリングし直すと全体タイムアウト予算(self.timeout)の
                        # 大半を1ページ目だけで使い果たし、2ページ目以降を巡回できず総ページ数が
                        # 極端に少ない「要確認」判定に化けてしまう。JSフレームワークを検知した
                        # ページ（＝静的HTMLだけでは内容が欠落する可能性が高いページ）のみに限定する。
                        # ヘッダー(グローバルナビ)だけをJavaScriptで後から差し込むページ(<header>が空)も、
                        # 静的HTMLではナビが無く、本文中のリストを拾ってしまうため描画し直す
                        if self.render_js and len(visited) == 1 and not first_html_from_playwright and (self._detect_js_framework(current_html) or self._has_empty_header(current_html)):
                            rendered, _rendered_url, _rendered_status, _nav_error = self._fetch_rendered_html(current_url)
                            if rendered:
                                current_html = rendered

                        previous_url = resolved_url
                        soup = BeautifulSoup(current_html, HTML_PARSER)
                        # 先頭ページ(トップページ)だけを対象にする判定(スクロールアニメーション)
                        # のために、ページの境界に区切りマーカーを挟む
                        if combined_html_src:
                            combined_html_src += f"\n{RenewalEvaluator.PAGE_SEPARATOR}"
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

                            # ナビ用のiframeがあるページは、ナビがiframeの中にあるとみなして優先する
                            # (トップページ自体から別のリンクの並びを拾っても、それはナビではない)
                            top_menus = self._extract_iframe_nav_menus(soup, resolved_url, client, cms_name) or self._extract_global_nav_menus(soup, cms_name)
                            for menu_text in top_menus:
                                if menu_text not in global_nav_menus:
                                    global_nav_menus.append(menu_text)

                        # お問い合わせ判定
                        is_contact_url = self._url_has_keyword(resolved_url, self.CONTACT_KEYWORDS)
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

                        # URLにもリンクテキストにもお問い合わせを示す語が無いページ
                        # (例: "/materials/"の資料請求フォーム)でも、本文欄(textarea)を
                        # 持つページはフォーム抽出を試みる(採用可否は下の
                        # _looks_like_genuine_contact_form()で厳しめに検証する)。
                        has_textarea = soup.find("textarea") is not None
                        if (is_contact_url or has_contact_text or has_textarea) and not contact_fields:
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
                            if self._looks_like_genuine_contact_form(soup, candidate_fields, is_contact_page=is_contact_url):
                                contact_fields, has_attachment = candidate_fields, candidate_attachment

                        # Next.js等のJSフレームワーク製サイトでは、お問い合わせページの
                        # フォームがクライアント側で描画され、静的HTMLには入力欄が1つも
                        # 無いことがある(実際に確認済み)。Playwrightでのレンダリングは
                        # 通常トップページにしか行わないため、お問い合わせページに限り
                        # レンダリングし直してフォームを探す。
                        if (
                            is_contact_url
                            and not contact_fields
                            and self.render_js
                            and contact_page_renders < self.MAX_CONTACT_PAGE_RENDERS
                            and not soup.find(["input", "textarea", "select"])
                            and self._detect_js_framework(current_html)
                        ):
                            contact_page_renders += 1
                            rendered_contact_html, _rendered_url, _rendered_status, _nav_error = self._fetch_rendered_html(resolved_url)
                            if rendered_contact_html:
                                candidate_fields, candidate_attachment = self._extract_form_fields(
                                    rendered_contact_html,
                                    resolved_url,
                                    client,
                                    depth=0,
                                )
                                rendered_soup = BeautifulSoup(rendered_contact_html, HTML_PARSER)
                                if self._looks_like_genuine_contact_form(rendered_soup, candidate_fields, is_contact_page=True):
                                    contact_fields, has_attachment = candidate_fields, candidate_attachment
                                elif self._has_form_like_controls(rendered_soup):
                                    contact_unread_pages.append(resolved_url)

                        # お問い合わせページに入力欄があるのに項目を取れなかった(項目名が
                        # 画像・placeholderだけ等)場合は、フォームが無いとは断定できない。
                        # is_contact_urlは"support"/"help"等も含む広い判定で、Q&Aページの絞り込み
                        # 検索欄に反応していた(実際に確認済み)ため、URLがお問い合わせページらしいものに限る
                        if not contact_fields and self.CONTACT_PAGE_PATH_RE.search(urlparse(resolved_url).path) and self._has_form_like_controls(soup) and resolved_url not in contact_unread_pages:
                            contact_unread_pages.append(resolved_url)
                        if not has_form_service_link and any(self.FORM_SERVICE_URL_RE.search(str(tag.get("href") or tag.get("src") or "")) for tag in soup.find_all(["a", "iframe"])):
                            has_form_service_link = True

                        # メールアドレスへのmailtoリンクの有無(フォームが無い場合の代替の問い合わせ手段)
                        if not has_mailto_link and soup.find("a", href=self.MAILTO_HREF_PATTERN):
                            has_mailto_link = True

                        # 別ドメインにあるお問い合わせページへのリンクを記録しておく
                        # (巡回はしないが、サイト内でフォームが見つからなかった場合に取得する)
                        if not contact_fields:
                            for ext_url in self._find_external_contact_links(soup, resolved_url, base_domain_clean):
                                if ext_url not in external_contact_urls and len(external_contact_urls) < self.MAX_EXTERNAL_CONTACT_PAGES:
                                    external_contact_urls.append(ext_url)

                        # ログイン・マイページ機能の検知
                        # (パスワード欄の有無 + サイト共通ヘッダーの有無という構造的シグナルで判定)
                        if not has_login and self._has_password_login_form(current_html):
                            has_login = True

                        # 内部リンク巡回
                        anchor_tags = soup.find_all("a", href=True)
                        contact_text_hrefs = {link["href"] for link in anchor_tags if self.CONTACT_LINK_TEXT_RE.search(link.get_text(" ", strip=True))}
                        candidate_hrefs = self._collect_link_candidates(soup, current_html, resolved_url)

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
                                # 実態より過大に表示される不具合があった(実際に確認済み:
                                # 実際に訪問した最深ページは5セグメントしかないのに、階層数が
                                # 10と表示されていた)。max_depthは、実際にキューから取り出して
                                # 処理した(=本当に訪問した)ページの深さのみを根拠にする
                                # (visited時点の更新は本ループの先頭付近、
                                # `if depth > max_depth: max_depth = depth` を参照)。
                                # ここではenqueue()の優先度判定に使うpath_depthの算出のみ行う。

                                is_priority = href in contact_text_hrefs or self._url_has_keyword(
                                    norm_abs,
                                    (
                                        "contact",
                                        "inquiry",
                                        "otoiawase",
                                        "form",
                                        "login",
                                        "mypage",
                                        "member",
                                        "account",
                                    ),
                                )
                                enqueue(norm_abs, path_depth, is_priority, _is_bare_relative_href(href))
                                if href in contact_text_hrefs or self.CONTACT_PAGE_PATH_RE.search(parsed_abs.path):
                                    contact_link_keys.add(_dedup_key(norm_abs))

                        time.sleep(0.04)

                    except httpx.TimeoutException:
                        timed_out_urls.append(norm_current)
                        continue
                    except httpx.RequestError:
                        continue

                # 本巡回でpage_timeoutを超えて取得できなかったページは、そのまま
                # 捨てるとページ数が実行のたびにぶれる(実際に確認済み: 応答の遅い
                # サーバーで同じサイトが77ページになったり72ページになったりした)。
                # 未訪問扱いに戻し、タイムアウトを長めに取る追加巡回で取り直す。
                for timed_out_url in timed_out_urls:
                    visited.discard(_dedup_key(timed_out_url))
                    enqueue(timed_out_url, 0, False)

                # 本巡回が時間切れで打ち切られた(100ページ未満のまま未訪問のリンクが
                # 残っている)場合、ページ数を確定させるためだけの追加巡回を行う。
                # 重複排除(visited/canonical_visited/_dedup_key)とリンクの採否判定
                # (_is_valid_internal_link/enqueue)は本巡回と共通のものを使い、
                # 数え方自体は本巡回と変えない。階層数(max_depth)はここでは更新しない
                # (後述のdisplay_depthのコメント参照)。
                if queue and len(canonical_visited) < 100:
                    count_deadline = time.time() + self.COUNT_ONLY_EXTRA_TIMEOUT
                    logger.info(
                        "%dページを数えた時点で未訪問リンクが%d件残っているため、ページ数カウント専用の追加巡回を行います: %s",
                        len(canonical_visited),
                        len(queue),
                        first_url,
                    )

                    def _fetch_for_count(url: str) -> tuple[str, str] | None:
                        try:
                            try:
                                response = _get_page(url, timeout=self.COUNT_ONLY_PAGE_TIMEOUT)
                            except httpx.RequestError:
                                # 本巡回と同様、https接続自体が失敗した場合は1回だけhttpを試す
                                if not url.startswith("https://"):
                                    return None
                                url = "http://" + url[len("https://") :]
                                response = _get_page(url, timeout=self.COUNT_ONLY_PAGE_TIMEOUT)
                            if response.status_code != 200:
                                return None
                            response = self._retry_if_path_dropped(client, url, response, dict(self.headers))
                            return str(response.url), self._decode_response(response)
                        except Exception:
                            # 1ページの取得失敗で追加巡回全体(ひいてはクロール結果全体)を止めない
                            return None

                    def _next_count_target() -> str | None:
                        """キューから次に取得すべきURLを取り出す(既訪問等で読み飛ばしたものは返さない)。"""
                        while queue:
                            url, _depth = queue.popleft()
                            queued_urls.discard(_dedup_key(url))
                            norm_url = normalize_url(url)
                            if _dedup_key(norm_url) in visited or _is_misresolved_relative_link(norm_url) or _is_known_redirect_alias(norm_url):
                                continue
                            visited.add(_dedup_key(norm_url))
                            if self._has_repeating_path_pattern(urlparse(norm_url).path):
                                continue
                            return url
                        return None

                    # 数件ずつまとめて取得して全件の完了を待つ方式だと、応答の遅い1件に
                    # 他のワーカーが待たされるため、1件完了するたびに次のURLを投入する。
                    # 時間予算を使い切った後は新規投入だけを止め、取得中のものは結果を
                    # 反映してから終える(取り出し済みのURLの結果を捨てると、キューが
                    # 空になって「巡回し尽くした」と誤判定されうるため)。
                    with ThreadPoolExecutor(max_workers=self.COUNT_ONLY_WORKERS) as pool:
                        in_flight: set[Future[tuple[str, str] | None]] = set()
                        while True:
                            while len(in_flight) < self.COUNT_ONLY_WORKERS and len(canonical_visited) < 100 and time.time() < count_deadline:
                                next_url = _next_count_target()
                                if next_url is None:
                                    break
                                in_flight.add(pool.submit(_fetch_for_count, next_url))
                            if not in_flight:
                                break
                            done, in_flight = wait(in_flight, return_when=FIRST_COMPLETED)
                            for future in done:
                                result = future.result()
                                if result is None or len(canonical_visited) >= 100:
                                    continue
                                resolved_url, page_html = result
                                resolved_key = _page_count_key(page_html, resolved_url)
                                if resolved_key in canonical_visited:
                                    continue
                                canonical_visited.add(resolved_key)

                                page_soup = BeautifulSoup(page_html, HTML_PARSER)
                                for href in self._collect_link_candidates(page_soup, page_html, resolved_url):
                                    if self._is_valid_internal_link(resolved_url, href, base_domain_clean):
                                        norm_abs = normalize_url(urljoin(resolved_url, href))
                                        path_depth = len([p for p in urlparse(norm_abs).path.split("/") if p])
                                        enqueue(norm_abs, path_depth, False, _is_bare_relative_href(href))

                    if len(canonical_visited) >= 100:
                        is_over_100 = True

                # サイト内でお問い合わせフォームが見つからなかった場合、別ドメインに
                # あるお問い合わせページ(例: 共用SSLホストのcontact.html)のフォームを拾う。
                # ページ数には数えず、そのページから先の巡回もしない。
                if not contact_fields and external_contact_urls:
                    contact_fields, external_attachment = self._extract_external_contact_form(client, external_contact_urls)
                    has_attachment = has_attachment or external_attachment

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

        # キューが空 = 発見できた内部リンクはすべて訪問し終えて自然に終了したことを意味する。
        # 逆にキューに未訪問のURLが残っている場合、タイムアウトや最大ページ数(100件)到達等の
        # 理由で途中で打ち切っただけであり、「本当にページ数の少ないサイト」とは区別する必要がある。
        queue_exhausted = not queue

        # お問い合わせフォームが見つからず、メールアドレスにmailtoリンクが
        # 設定されている場合は、問い合わせ手段としてL列に"mailto"と出す。
        # mailtoも無い場合は「問い合わせフォームなし」と出す。ただし、フォームが別ページ・
        # 外部サービス・JavaScript描画で取れなかっただけのサイトも混ざる(実際に確認済み)ため、
        # その疑いがある場合はcontact_form_uncertainをTrueにし、Excelで背景色を付けて
        # AI補完の対象にする。疑いの無い場合(お問い合わせページが電話番号の案内だけ等)は
        # 「なし」が妥当なことが多い(実際に確認済み)ため、色は付けない
        if not contact_fields:
            if has_mailto_link:
                contact_fields = "mailto"
            else:
                contact_fields = self.NO_CONTACT_FORM_TEXT

                # 同じページへのリンクが末尾の"/"・"index.html"の有無だけ違う形で書かれている
                # こともあるため、それらをそろえて巡回済みかを判定する
                def _page_key(key: str) -> str:
                    return re.sub(r"/(?:index\.(?:html?|php))?$", "", key.split("#")[0])

                visited_pages = {_page_key(key) for key in visited}
                unvisited_contact_pages = [key for key in contact_link_keys if key in queued_urls and _page_key(key) not in visited_pages]
                reasons = {
                    f"お問い合わせページに入力欄があるが項目を取れなかった({', '.join(contact_unread_pages[:3])})": bool(contact_unread_pages),
                    f"未巡回のお問い合わせページがある({', '.join(unvisited_contact_pages[:3])})": bool(unvisited_contact_pages),
                    "外部のフォームサービスへのリンクがある": has_form_service_link,
                    f"別ドメインのお問い合わせページからフォームを取れなかった({', '.join(external_contact_urls)})": bool(external_contact_urls),
                }
                found = [reason for reason, hit in reasons.items() if hit]
                self.contact_form_uncertain = bool(found)
                if found:
                    logger.info("[%s] 問い合わせフォームなし(取り損ねの疑いあり): %s", start_url, " / ".join(found))

        # 10項目を超えるのは、子メニューまで展開したスマホ用メニューやサイトマップ的な
        # リンク集を拾った場合がほとんどで、先頭10項目に切り詰めると中途半端な内容になる
        # (実際に確認済み)。空欄にして、Excel上で要確認と分かるようにし、AI補完の対象にする
        site_structure = "\n".join(global_nav_menus) if len(global_nav_menus) <= self.MAX_STRUCTURE_ITEMS else ""
        # 総ページ数は、リダイレクト前の生URL(visited)の件数ではなく、実際に
        # 表示された実体ページ(canonical_visited)の件数で数える。生URLは
        # http/httpsの混在や壊れた相対リンクによって同じページに対して
        # 複数存在しうるため、visitedの件数をそのまま使うと過大カウントに
        # なる(実際に確認済み)。
        final_page_count: int | str = "100以上" if is_over_100 or len(canonical_visited) >= 100 else len(canonical_visited)

        # 内部的にはトップページ=0階層目として深さを数えているが、これをそのまま
        # 「階層数」として返すと、1ページだけの正常なサイトでも"0"と表示されてしまい、
        # クロールが失敗したように見えて紛らわしい。「◯階層」という言い回しに合わせ、
        # トップページ=1階層目として+1した値を、evaluator側の判定にも使う統一の値として返す。
        # (evaluator.py側の「3階層以上でNG」というしきい値も、この1始まりの値を
        # 前提に max_depth > 2 として判定するようになっている)
        display_depth: int | str = max_depth + 1
        if max_depth > 10:
            display_depth = "要確認"

        # queue_exhaustedがFalse(=タイムアウトで途中終了)かつ100ページ上限にも
        # 達していない場合、final_page_countは「キューに未訪問のリンクを
        # 残したまま打ち切った時点の、道半ばの数値」でしかない。
        # 1ページあたりのリダイレクト回数が異常に多いサイト(www有無や
        # 末尾スラッシュ補正で1ページに2〜3回のリダイレクトが発生する)
        # では、時間切れになってもそれなりの件数まで進んでしまう。
        #
        # 以前はこれをそのままExcelに出力すると正確な集計結果に見えて紛らわしい
        # という理由で一律「要確認」に置き換えていたが、それでは他ツール
        # (WEB Explorer等)で別途調べ直さない限り数値が一切埋まらなくなって
        # しまっていた。訪問済みページ数はクロールを続けるほど増える一方
        # (減ることはない)ため、途中終了時点の値でも「少なくともこれだけは
        # 確実にある」という下限値としてはそのまま使える。100ページ上限到達時の
        # "100以上"と表記を揃え、"{数値}以上"として出力する。
        #
        # 一方、階層数(display_depth)はここでは書き換えない。ナビゲーション
        # メニュー等サイトの主要な階層構造は、ページ数が多いサイトでも
        # 巡回のごく早い段階(=浅い階層のリンクほど先に見つかる)でほぼ判明する
        # ため、時間切れで打ち切られたとしても、そこまでに観測できたmax_depthは
        # 実際の最大階層とほぼ一致していることが多い。ページ数のように
        # 「打ち切られたせいで大きく過小評価している」状況とは性質が異なるため、
        # 確定していないことを示す"以上"を付けず、素の数値のまま出力する。
        if not is_over_100 and not queue_exhausted and isinstance(final_page_count, int):
            final_page_count = f"{final_page_count}以上"

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
