import re
from typing import Any

from bs4 import BeautifulSoup, Tag


class RenewalEvaluator:
    """クロール結果とHTMLから、サイトリニューアル可否(×/要確認)と理由を判定する。"""

    # 画像認証は、_has_captcha()内で、表示型のreCAPTCHA v2/hCaptchaウィジェット、
    # および"captcha"を名前に含む画像・入力欄(IMAGE_CAPTCHA_PATTERN)から検出する
    # (reCAPTCHA v3/invisible型は画像認証を求めないため対象外)。
    # このリストはそれを補完するもので、画像認証を実装しているCMS/プラグイン
    # 固有のクラス名/識別子を、<body>内のタグの属性・スクリプトから部分一致で
    # 検出するために使う。
    CAPTCHA_KEYWORDS = [
        "image_auth_jp",  # WordPress: 画像認証(ひらがな)プラグイン (Contact Form 7用)
        "ccm-captcha-image",  # Concrete CMS: 画像認証のimgタグ
        "ccm-input-captcha",  # Concrete CMS: 画像認証の入力欄
    ]
    # 画像認証の<img>/<input>のclass・id・name・src・altに現れる"captcha"。
    # "recaptcha"/"grecaptcha"(reCAPTCHAのトークン受け渡し用)は除外する。
    IMAGE_CAPTCHA_PATTERN = re.compile(r"(?<!re)captcha")
    # ブランド名だけの単語は一般名詞や無関係な文脈と衝突しやすいため
    # (例: "chatbot"はそれ自体が一般用語、"intercom"は「インターホン」の意味でも使われる)、
    # 可能な限り各サービスのスクリプト読み込み元ドメインなど、実際に埋め込まれた
    # ときにしか出現しない固有の文字列を使う。
    CHATBOT_KEYWORDS = [
        "ws1.sinclo.jp",  # sinclo
        "chamo-chat.com",  # Chamo (GENIEE CHAT)
        "zdassets.com",
        "zopim.com",  # Zendesk Chat
        "cdn.channel.io",  # channel.io
        "hubspot-messages",  # HubSpot Conversations
        "widget.intercom.io",  # Intercom
        "client.crisp.chat",  # Crisp
        "code.tidio.co",  # Tidio
        "chatplus.jp",  # ChatPlus
        "salesiq.zohopublic",  # Zoho SalesIQ
        "dynameet.ai",  # Dynameet
        "larubot.tokyo",  # Larubot
    ]
    CALENDAR_KEYWORDS = ["wp-calendar", "xo-event-calendar"]
    SEARCH_KEYWORDS = ["絞り込み検索", "条件検索", "サイト内検索", "キーワード検索"]
    RICH_UI_KEYWORDS = ["lightbox", "fancybox", "data-lightbox"]
    MULTILANG_KEYWORDS = ["translate.google", "language-list", "lang-select", "言語切り替え"]
    SCROLL_ANIMATION_KEYWORDS = [
        "gsap",
        "scrolltrigger",
        "data-aos",  # AOS
        "/aos.js",  # "chaos.js"等に当たらないよう"/"から照合
        "/aos.css",
        "locomotive-scroll",
        "scrollreveal",  # ScrollReveal
        "scrollmagic",  # ScrollMagic
        "data-sal",  # sal.js ("sal.js"だと"universal.js"等に当たるため属性で照合)
        "/wow.js",  # WOW.js
        "/wow.min.js",
        "data-wow-",
        "jquery.inview",  # jQuery inview
    ]
    # スクロールアニメーションライブラリが要求するclass名(セグメント単位で照合)
    SCROLL_ANIMATION_CLASS_NAMES = ("wow", "inview")
    VIDEO_KEYWORDS = ["<video", "youtube.com/embed", "vimeo.com", "youtu.be", "player.vimeo"]

    # クロールが時間切れで途中終了し、判定を保留する場合の定型文。
    # これは「サイトの特徴に基づく不可の理由」ではなく、あくまで調査側の
    # 都合(時間切れ)を人間に伝えるための注記でしかないため、呼び出し側
    # (scraper_service.py)ではこの文言をrejection_reason(M列: 不可の理由)
    # ではなくremarks(N列: 備考)に振り分ける。scraper_service.py側でも
    # 同じ文言を重複定義しないよう、ここを単一の参照元にする。
    PENDING_TIMEOUT_REASON = (
        "クロールが時間切れで途中終了し、内部リンクを全て確認できなかったため判定を保留しました。"
        "1ページあたりのリダイレクト回数が多い等の理由で通常より時間がかかっているサイトの可能性があります。"
    )

    # 「×」の根拠が1つも見つからず、「◯」の代わりに「要確認」とする場合の定型文。
    # PENDING_TIMEOUT_REASONと同様、不可の理由ではなく人間への注記なので、
    # 呼び出し側(scraper_service.py)ではM列ではなくN列(備考)に振り分ける。
    NO_NG_REASON_NOTE = "×の根拠が見つからなかったため、目視で確認してください。"

    # crawler.pyが複数ページのHTMLを連結してhtml_srcを作る際に、ページの
    # 境界に挟む区切りマーカー。先頭(=トップページ)だけを対象にする判定で使う。
    # HTMLコメントなので、他の判定(属性・スクリプト・UIテキスト)には影響しない。
    PAGE_SEPARATOR = "<!-- web-analyzer:page-separator -->"

    # UI要素ではないため、セレクタ/クラス名による判定から除外するアセットタグ。
    ASSET_TAGS = ("link", "script", "style", "meta")

    # 表示テキストからキーワードを探す際の対象タグ。見出しや本文(<p>等)の
    # 文章中で機能名に言及しているだけのケースを拾わないよう、操作UIを
    # 構成する要素のテキストに限定する。
    UI_TEXT_TAGS = ("a", "button", "label", "option", "summary", "legend", "form")

    # position:fixedの要素のうち、フローティングボタンではなくヘッダー・
    # ナビゲーション・Cookie同意バナー等の固定表示UIとみなして除外する
    # class/idのセグメント。
    FIXED_NON_BUTTON_SEGMENTS = {
        "header",
        "nav",
        "navbar",
        "navigation",
        "menu",
        "drawer",
        "cookie",
        "cookies",
        "consent",
        "gdpr",
        "cmp",
        "modal",
        "overlay",
        "loading",
        "loader",
    }

    def _soup(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html or "", "html.parser")

    def _bodies(self, soup: BeautifulSoup) -> list[Tag | BeautifulSoup]:
        """判定対象を<body>内に限定するため、全ての<body>要素を返す。

        crawler.pyは巡回した複数ページのHTMLを連結してhtml_srcとして渡すため、
        <body>は複数存在しうる。soup.bodyは最初の1つしか返さず、2ページ目
        以降が判定対象から漏れてしまうため、全ての<body>を対象にする。
        閉じタグの欠落で入れ子になった<body>は外側の<body>の検索に含まれる
        ため除外する。bodyが無ければ全体を返す。
        """
        bodies = [b for b in soup.find_all("body") if b.find_parent("body") is None]
        return bodies or [soup]

    def _body_tags(self, soup: BeautifulSoup, *args: Any, **kwargs: Any) -> list[Tag]:
        """全ての<body>内でfind_all(*args, **kwargs)した結果を連結して返す。"""
        return [tag for body in self._bodies(soup) for tag in body.find_all(*args, **kwargs)]

    def _select_ui_elements(self, soup: BeautifulSoup, selector: str) -> list[Tag]:
        """<body>内のUI要素を検索し、<link>や<script>などのアセットタグを除外する"""
        elements = [el for body in self._bodies(soup) for el in body.select(selector)]
        return [el for el in elements if el.name not in self.ASSET_TAGS]

    @staticmethod
    def _name_segments(name: str) -> list[str]:
        """class名/idを"-"/"_"/camelCaseの区切りで小文字のセグメントに分割する。

        "form-floating" → ["form", "floating"]、"tabContent" → ["tab", "content"]
        """
        name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", name)
        return [seg for seg in re.split(r"[-_\s]+", name.lower()) if seg]

    @staticmethod
    def _contains_phrase(segments: list[str], phrase: str) -> bool:
        """セグメント列に、"-"区切りのphraseが連続した並びとして含まれるか。"""
        words = phrase.split("-")
        n = len(words)
        return any(segments[i : i + n] == words for i in range(len(segments) - n + 1))

    def _has_named_element(
        self,
        soup: BeautifulSoup,
        phrases: tuple[str, ...],
        exclude: tuple[str, ...] = (),
    ) -> bool:
        """<body>内に、class/idがphrasesのいずれかをセグメント単位で含む要素があるか。

        "[class*='tabs']"のような部分一致では"stabs"や"tablets"等の無関係な
        クラス名にも当たってしまうため、区切り文字で分割したセグメント単位で
        照合する。excludeのいずれかを含むclass/idは対象外にする。
        """
        for tag in self._body_tags(soup, True):
            if tag.name in self.ASSET_TAGS:
                continue
            names = list(tag.get("class") or [])
            if tag.get("id"):
                names.append(str(tag["id"]))
            for name in names:
                segments = self._name_segments(name)
                if any(self._contains_phrase(segments, p) for p in exclude):
                    continue
                if any(self._contains_phrase(segments, p) for p in phrases):
                    return True
        return False

    def _markup_text(self, soup: BeautifulSoup) -> str:
        """<body>内の全タグの属性名・属性値と<script>の中身を連結した小文字文字列。

        str(body)をそのまま検索すると、本文テキストやHTMLコメント中の単語
        (記事中の「gsap」等)にも反応してしまうため、実装の痕跡が現れる
        属性とスクリプトだけに対象を絞る。
        """
        parts: list[str] = []
        for tag in self._body_tags(soup, True):
            for key, value in tag.attrs.items():
                parts.append(key)
                parts.append(" ".join(value) if isinstance(value, list) else str(value))
            if tag.name == "script" and tag.string:
                parts.append(tag.string)
        return "\n".join(parts).lower()

    def _ui_text(self, soup: BeautifulSoup) -> str:
        """<body>内の操作UI要素(UI_TEXT_TAGS)の表示テキストを連結した文字列。"""
        return "\n".join(tag.get_text(" ", strip=True) for tag in self._body_tags(soup, self.UI_TEXT_TAGS))

    def _has_calendar(self, soup: BeautifulSoup) -> bool:
        if self._select_ui_elements(soup, ".wp-calendar,.xo-event-calendar,iframe[src*='calendar.google' i]"):
            return True
        # "calendar-icon"のようなアイコン用クラスはカレンダー機能そのものではないため除外
        return self._has_named_element(soup, ("calendar",), exclude=("icon", "ico"))

    def _has_accordion(self, soup: BeautifulSoup) -> bool:
        # <details>/<summary>はブラウザ標準の開閉UIそのものなので、アコーディオンとして扱う
        if self._select_ui_elements(soup, "[data-bs-toggle='collapse'],details"):
            return True
        return self._has_named_element(soup, ("accordion",))

    def _has_tabs(self, soup: BeautifulSoup) -> bool:
        if self._select_ui_elements(soup, "[role='tab'],[role='tablist'],[data-bs-toggle='tab']"):
            return True
        return self._has_named_element(soup, ("tabs", "tab-content", "tab-pane", "tablist"))

    def _has_modal(self, soup: BeautifulSoup) -> bool:
        if self._select_ui_elements(soup, "[data-bs-toggle='modal'],[role='dialog'][aria-modal='true' i]"):
            return True
        return self._has_named_element(soup, ("modal",))

    def _has_floating(self, soup: BeautifulSoup) -> bool:
        # Bootstrap 5の"form-floating"や"floating-label"はフォームのラベル表現で
        # あり、画面追従ボタンではないため除外する。
        if self._has_named_element(
            soup,
            ("floating", "fixed-btn", "follow-window"),
            exclude=("form-floating", "label"),
        ):
            return True

        for tag in self._body_tags(soup, style=True):
            if tag.name in self.ASSET_TAGS:
                continue
            style = str(tag["style"]).replace(" ", "").lower()
            if "position:fixed" not in style:
                continue
            if self._is_fixed_non_button(tag):
                continue
            # 固定表示の要素のうち、リンクやボタンを持つもの(=押せるもの)だけを
            # フローティングボタンとみなす
            if tag.name in ("a", "button") or tag.find(["a", "button"]):
                return True

        return False

    def _is_fixed_non_button(self, tag: Tag) -> bool:
        """position:fixedの要素が、ヘッダー・ナビ・Cookie同意バナー等か判定する。"""
        if tag.name in ("header", "nav") or tag.find(["header", "nav"]):
            return True
        names = list(tag.get("class") or [])
        if tag.get("id"):
            names.append(str(tag["id"]))
        return any(seg in self.FIXED_NON_BUTTON_SEGMENTS for name in names for seg in self._name_segments(name))

    def _has_search(self, soup: BeautifulSoup) -> bool:
        if self._body_tags(soup, "input", {"type": "search"}):
            return True

        # 本文中で「サイト内検索」等に言及しているだけのケースを拾わないよう、
        # 操作UIのテキストと属性値(placeholder/aria-label等)に限定して探す
        text = self._ui_text(soup) + "\n" + self._markup_text(soup)
        return any(x in text for x in self.SEARCH_KEYWORDS)

    def _has_captcha(self, soup: BeautifulSoup) -> bool:
        """<body>内に、ユーザーが操作する画像認証(Captcha)が存在するか判定する。

        reCAPTCHA v3や、v2/hCaptchaのinvisible型は「reCAPTCHAで保護されています」
        というバッジ・注記が表示されるだけで、ユーザーに画像認証を求めない。
        そのため、バッジ(.grecaptcha-badge)や注記テキスト、トークン受け渡し用の
        g-recaptcha-response等、reCAPTCHA関連のclass/idがあるだけでは対象にしない。
        """
        # reCAPTCHA v2(チェックボックス型) / hCaptchaのウィジェット。
        # data-size="invisible"は画面に表示されない型なので除外する。
        for widget in self._select_ui_elements(soup, ".g-recaptcha,.h-captcha"):
            if str(widget.get("data-size") or "").lower() != "invisible":
                return True

        # 画像認証の画像・入力欄(Really Simple CAPTCHA等)。"recaptcha"を含む
        # 名前はreCAPTCHAのトークン受け渡し用なので除外する。
        for tag in self._body_tags(soup, ["img", "input"]):
            if tag.name == "input" and str(tag.get("type") or "").lower() == "hidden":
                continue
            attrs = " ".join(" ".join(v) if isinstance(v, list) else str(v) for k, v in tag.attrs.items() if k in ("class", "id", "name", "src", "alt")).lower()
            if self.IMAGE_CAPTCHA_PATTERN.search(attrs):
                return True

        # CMS / プラグイン固有の画像認証
        markup = self._markup_text(soup)
        return any(keyword in markup for keyword in self.CAPTCHA_KEYWORDS)

    def _has_chatbot(self, soup: BeautifulSoup) -> bool:
        """<body>内にチャットボット本体が存在するか判定する。"""
        markup = self._markup_text(soup)
        return any(keyword in markup for keyword in self.CHATBOT_KEYWORDS)

    def _has_video(self, soup: BeautifulSoup) -> bool:
        """<body>内に実際の動画要素・埋め込みが存在するか判定する。"""
        # HTML要素としてのvideo
        if self._body_tags(soup, "video"):
            return True

        # YouTube / Vimeo等のiframe
        for iframe in self._body_tags(soup, "iframe"):
            src = str(iframe.get("src") or "").lower()

            if any(
                keyword in src
                for keyword in (
                    "youtube.com/embed",
                    "youtu.be",
                    "vimeo.com",
                    "player.vimeo",
                )
            ):
                return True

        return False

    def _has_rich_ui(self, soup: BeautifulSoup) -> bool:
        """<body>内にLightbox等の実際のリッチUI要素が存在するか判定する。"""
        if self._select_ui_elements(soup, "[data-lightbox],[data-fancybox]"):
            return True
        return self._has_named_element(soup, ("lightbox", "glightbox", "fancybox"))

    def _has_multilang(self, soup: BeautifulSoup) -> bool:
        """<body>内に実際の多言語切り替えUIが存在するか判定する。"""
        # 明確な言語切り替え用クラス・ID("lang-select"/"language-list"等)
        if self._has_named_element(soup, ("lang", "language", "languages", "言語")):
            return True

        text = self._ui_text(soup) + "\n" + self._markup_text(soup)
        return any(keyword in text for keyword in self.MULTILANG_KEYWORDS)

    def _asset_text(self, soup: BeautifulSoup) -> str:
        """<head>を含むドキュメント全体の<script>/<link>のsrc・hrefと<script>の中身。

        GSAP/AOS等のライブラリは<head>で読み込まれることが多く、<body>内
        だけを見ると読み込みの痕跡を見落とすため、こちらは全体を対象にする。
        """
        parts: list[str] = []
        for tag in soup.find_all(["script", "link"]):
            parts.append(str(tag.get("src") or tag.get("href") or ""))
            if tag.name == "script" and tag.string:
                parts.append(tag.string)
        return "\n".join(parts).lower()

    def _top_page_soup(self, html_src: str) -> BeautifulSoup:
        """連結されたhtml_srcから、先頭ページ(トップページ)のHTMLだけをパースする。"""
        return self._soup(html_src.split(self.PAGE_SEPARATOR, 1)[0])

    def _has_scroll_animation(self, soup: BeautifulSoup) -> bool:
        """スクロールアニメーション関連の実装が存在するか判定する。

        evaluate()からはトップページのみのsoupが渡される。
        ライブラリの読み込み(<head>内を含む)、またはライブラリ固有の
        属性(data-aos等)・クラス名(WOW.jsの"wow"等)を<body>内から探す。
        """
        text = self._markup_text(soup) + "\n" + self._asset_text(soup)
        if any(keyword in text for keyword in self.SCROLL_ANIMATION_KEYWORDS):
            return True
        return self._has_named_element(soup, self.SCROLL_ANIMATION_CLASS_NAMES)

    #: クロール側(crawler.py)が、実際に確認できたページ数の下限値を
    #: 表すために使うサフィックス("100以上"/"36以上"等)。
    LOWER_BOUND_SUFFIX = "以上"

    @classmethod
    def parse_lower_bound(cls, value: int | str) -> tuple[int, bool]:
        """int、または"{数値}以上"形式の文字列を(数値, 信頼できる下限値かどうか)に変換する。

        クロール側(crawler.py)は、総ページ数について、100ページ上限到達時や
        時間切れで途中打ち切られた時に"100以上"/"36以上"のような
        「{数値}以上」形式の文字列を返してくる。これは実際に確認できた
        件数の下限値(クロールを続ければ増えることはあっても減ることはない)
        であり、判定の根拠として信頼してよい確定情報である。
        階層数は整数、または10階層超で計測不能を表す"要確認"のどちらかで
        返ってくる。"要確認"のように「以上」が付かない文字列や数値化できない
        文字列は、下限値としてすら信用できない値のため(0, False)を返す。
        """
        if isinstance(value, int):
            return value, True
        if isinstance(value, str) and value.endswith(cls.LOWER_BOUND_SUFFIX):
            try:
                return int(value[: -len(cls.LOWER_BOUND_SUFFIX)]), True
            except ValueError:
                return 0, False
        return 0, False

    def decide(
        self,
        total_pages: int | str,
        max_depth: int | str,
        has_login: bool,
        has_attachment: bool,
        has_basic_auth: bool = False,
        has_multilang: bool = False,
        html_src: str = "",
        page_threshold: int = 10,
        queue_exhausted: bool = False,
    ) -> tuple[str, str]:
        """クロール結果から調査結果（×/要確認）と理由を一括で決定する。

        調査結果は「×」か「要確認」のどちらかで、「◯」は返さない
        (evaluate()が「◯」の場合も「要確認」にする)。

        「そもそも判定不能として要確認に倒すか」という、接続失敗・階層数計測不能・
        クロールの途中終了といった特殊ケースの判断もここに集約する。
        evaluate() を直接呼ぶより、こちらを呼ぶことを推奨する。

        queue_exhausted は、クロール側が発見した内部リンクをすべて訪問し終えて
        自然にキューが空になったか(True)、タイムアウトや100ページ上限到達で
        未訪問のリンクを残したまま打ち切ったか(False)を表す。

        ページ数・ログイン機能の有無等、一度確定した「×」の根拠は、
        クロールを続けても消えたり減ったりしない(訪問ページが増える方向に
        しか動かない)。例えば100ページ上限で打ち切られたサイトは
        queue_exhaustedがほぼ必ずFalseになるが、「少なくとも100ページある」
        こと自体は確定情報である。

        そのため、まず（信頼できる情報の範囲で）通常の評価ロジック
        （evaluate）を実行し、「×」の根拠が1つでも見つかっていれば
        queue_exhaustedの状態に関わらずそれを採用する。
        「×」の根拠が1つも見つからず、かつ「続きを巡回すれば理由が
        見つかったかもしれない」という不確実性が残っている場合
        （ページ数・階層数を確定情報として使えない、またはクロールが
        途中終了している場合）に限り、「要確認」に倒す。
        """
        if total_pages == 0:
            if has_basic_auth:
                # ベーシック認証で弾かれている場合、クロール自体は0ページで終わるが、
                # これは「接続不可」ではなく明確に判定可能な理由なので、
                # 汎用の「要確認」メッセージより優先する。
                return "×", "ベーシック認証がかかっているページがあるため"
            return "要確認", "接続不可またはアクセス拒否のため、判定を保留しました。"

        pending_reason = self.PENDING_TIMEOUT_REASON

        # ページ数・階層数が数値化できない文字列(階層数の"要確認"=10階層超で
        # 計測不能、等)の場合は、それを根拠にした判定は行わない(0扱いで
        # evaluateへ渡す)。それ以外("36以上"のようなページ数の下限値表記や、
        # 通常の整数値)は、実際にクロールできた件数・到達できた深さとして
        # 信頼できるため、そのまま根拠に使う。
        page_count_num, page_count_reliable = self.parse_lower_bound(total_pages)
        max_depth_int, depth_reliable = self.parse_lower_bound(max_depth)

        eval_result, reason = self.evaluate(
            total_pages=page_count_num,
            max_depth=max_depth_int,
            has_login=has_login,
            has_attachment=has_attachment,
            has_basic_auth=has_basic_auth,
            has_multilang=has_multilang,
            html_src=html_src,
            page_threshold=page_threshold,
        )

        if eval_result == "×":
            # ページ数・階層数・ログイン機能の有無等、既に確定した「×」の
            # 根拠はクロールが途中終了していても覆らない事実なので、
            # queue_exhausted等の状態に関わらずそのまま採用する。
            return eval_result, reason

        if not page_count_reliable:
            return "要確認", pending_reason

        if not depth_reliable:
            return "要確認", "サイト階層が深すぎるため、別途サイトエクスプローラー等での確認をお願いします。"

        if not queue_exhausted:
            return "要確認", pending_reason

        # evaluate()が「◯」でも、自動判定で検知できない要素(JSで描画される
        # UI等)が残っている可能性があるため、調査結果としては「◯」を出さず
        # 「要確認」として人手での確認に回す。
        return "要確認", self.NO_NG_REASON_NOTE

    def evaluate(
        self,
        total_pages: int,
        max_depth: int,
        has_login: bool,
        has_attachment: bool,
        has_basic_auth: bool = False,
        has_multilang: bool = False,
        html_src: str = "",
        page_threshold: int = 10,
    ) -> tuple[str, str]:
        """判定結果(◯/×)と、NGの場合の理由（改行区切り文字列）を一括で返す。

        接続失敗・階層数計測不能・クロールの途中終了などの特殊ケースを考慮せず、
        純粋なビジネスルールのみで◯/×を決める。特殊ケースを含めた
        最終判定が必要な場合は decide() を使うこと。
        """
        reasons: list[str] = []
        html_lower = html_src.lower()

        # --- ページ数・階層構成 ---
        if total_pages > page_threshold:
            reasons.append("ページ数が多いため")

        # max_depth はクローラー側で「トップページ=1階層目」として1始まりで返される
        # 表示用の階層数そのもの。「3階層以上でNG」というビジネスルールに対し、
        # この1始まりの値であれば max_depth > 2 (=3以上) でそのまま正しく表現できる。
        if max_depth > 2:
            reasons.append("サイト構成が3階層以上のため")

        # --- ログイン・添付・認証機能（クローラー側で検知済みの値を使用） ---
        if has_login:
            reasons.append("ログイン・マイページ機能があるため")

        if has_attachment:
            reasons.append("お問い合わせフォームに添付機能があるため")

        if has_basic_auth:
            reasons.append("ベーシック認証がかかっているページがあるため")

        if has_multilang:
            reasons.append("多言語対応（言語切り替え機能）があるため")

        if not html_lower:
            if reasons:
                return "×", "\n".join(reasons)
            return "◯", ""

        # BeautifulSoupを生成
        soup = self._soup(html_src)

        # --- お問い合わせフォームの特殊仕様 ---
        if self._has_captcha(soup):
            reasons.append("お問い合わせフォームで画像認証（Captcha）を使用しているため")

        # --- 専用機能の検知 ---
        if self._has_calendar(soup):
            reasons.append("カレンダー機能が実装されているため")

        if self._has_search(soup):
            reasons.append("サイト内検索・絞り込み検索機能があるため")

        if self._has_chatbot(soup):
            reasons.append("チャットボットが導入されているため")

        # --- デザイン・ギミック面 ---
        if self._has_video(soup):
            reasons.append("動画（videoタグやYouTube/Vimeo埋め込み）が使用されているため")

        if self._has_floating(soup):
            reasons.append("フローティングボタン（画面追従ボタン）があるため")

        if self._has_accordion(soup):
            reasons.append("アコーディオンが使用されているため")

        if self._has_tabs(soup):
            reasons.append("タブ切り替えが使用されているため")

        if self._has_modal(soup):
            reasons.append("モーダルウィンドウが使用されているため")

        # --- リッチコンテンツ ---
        if self._has_rich_ui(soup):
            reasons.append("ギャラリーコンテンツ（Lightbox等）が導入されているため")

        if not has_multilang and self._has_multilang(soup):
            reasons.append("多言語対応（言語切り替え機能）があるため")

        # スクロールアニメーションはトップページのみで判定する
        if self._has_scroll_animation(self._top_page_soup(html_src)):
            reasons.append("スクロールアニメーション（GSAP等）が使用されているため")

        if reasons:
            return "×", "\n".join(reasons)
        return "◯", ""
