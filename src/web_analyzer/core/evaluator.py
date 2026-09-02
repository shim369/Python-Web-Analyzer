from bs4 import BeautifulSoup, Tag


class RenewalEvaluator:
    """Improved RenewalEvaluator (excerpt). Replace your class with this version as needed."""

    # reCAPTCHA/hCaptcha自体はウィジェットが読み込まれているだけで、
    # 実際にユーザーへ「画像内の文字を入力させる」認証があるとは限らないため
    # (invisible/チェックボックス型のことも多い)、判定対象には含めない。
    # ここでは、実際にそうした画像認証を実装していると確認できている
    # プラグイン固有のクラス名/識別子のみを対象にする。
    CAPTCHA_KEYWORDS = [
        "image_auth_jp",  # WordPress: 画像認証(ひらがな)プラグイン (Contact Form 7用)
        "ccm-captcha-image",  # Concrete CMS: 画像認証のimgタグ
        "ccm-input-captcha",  # Concrete CMS: 画像認証の入力欄
    ]
    # ブランド名だけの単語は一般名詞や無関係な文脈と衝突しやすいため
    # (例: "chatbot"はそれ自体が一般用語、"intercom"は「インターホン」の意味でも使われる)、
    # 可能な限り各サービスのスクリプト読み込み元ドメインなど、実際に埋め込まれた
    # ときにしか出現しない固有の文字列を使う。
    CHATBOT_KEYWORDS = [
        "ws1.sinclo.jp",  # sinclo
        "chamo",  # Chamo
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
    SCROLL_ANIMATION_KEYWORDS = ["gsap", "scrolltrigger", "data-aos", "locomotive-scroll"]
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

    def _soup(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html or "", "html.parser")

    def _body(self, soup: BeautifulSoup) -> Tag | BeautifulSoup:
        """判定対象を<body>内に限定する。bodyが無ければ従来通り全体を返す。"""
        return soup.body or soup

    def _select_ui_elements(self, soup: BeautifulSoup, selector: str) -> list[Tag]:
        """<body>内のUI要素を検索し、<link>や<script>などのアセットタグを除外する"""
        body = self._body(soup)
        elements = body.select(selector)
        return [el for el in elements if el.name not in ("link", "script", "style", "meta")]

    def _has_calendar(self, soup: BeautifulSoup) -> bool:
        return bool(self._select_ui_elements(soup, ".wp-calendar,.xo-event-calendar,iframe[src*='calendar.google' i],[class*='calendar' i],[id*='calendar' i]"))

    def _has_accordion(self, soup: BeautifulSoup) -> bool:
        return bool(self._select_ui_elements(soup, ".accordion,[data-bs-toggle='collapse'],details,[class*='accordion' i],[id*='accordion' i]"))

    def _has_tabs(self, soup: BeautifulSoup) -> bool:
        return bool(self._select_ui_elements(soup, "[role='tab'],.nav-tabs,[data-bs-toggle='tab'],[class*='tabs' i],[class*='tab-content' i],[id*='tabs' i]"))

    def _has_modal(self, soup: BeautifulSoup) -> bool:
        return bool(self._select_ui_elements(soup, ".modal,.modal-dialog,[data-bs-toggle='modal'],[class*='modal' i],[id*='modal' i]"))

    def _has_floating(self, soup: BeautifulSoup) -> bool:
        body = self._body(soup)

        for tag in body.find_all(True):
            # <link> や <script> などのアセットタグはアコーディオン等の属性ID誤検知を防ぐため除外
            if tag.name in ("link", "script", "style", "meta"):
                continue

            cls = " ".join(tag.get("class", [])).lower()

            if any(
                x in cls
                for x in (
                    "floating",
                    "fixed-btn",
                    "follow-window",
                )
            ):
                return True

            style = (tag.get("style") or "").replace(" ", "").lower()
            if "position:fixed" in style:
                return True

        return False

    def _has_search(self, soup: BeautifulSoup, html: str) -> bool:
        body = self._body(soup)

        if body.find("input", {"type": "search"}):
            return True

        body_html = str(body).lower()
        return any(x in body_html for x in self.SEARCH_KEYWORDS)

    def _has_captcha(self, soup: BeautifulSoup) -> bool:
        """<body>内にCaptcha関連の実装が存在するか判定する。"""
        body = self._body(soup)

        # reCAPTCHA / hCaptcha
        if self._select_ui_elements(
            soup,
            ".g-recaptcha,.h-captcha,[class*='captcha' i],[id*='captcha' i]",
        ):
            return True

        # CMS / プラグイン固有の画像認証
        body_html = str(body).lower()

        return any(keyword in body_html for keyword in self.CAPTCHA_KEYWORDS)

    def _has_chatbot(self, soup: BeautifulSoup) -> bool:
        """<body>内にチャットボット本体が存在するか判定する。"""
        body = self._body(soup)
        body_html = str(body).lower()

        return any(keyword in body_html for keyword in self.CHATBOT_KEYWORDS)

    def _has_video(self, soup: BeautifulSoup) -> bool:
        """<body>内に実際の動画要素・埋め込みが存在するか判定する。"""
        body = self._body(soup)

        # HTML要素としてのvideo
        if body.find("video"):
            return True

        # YouTube / Vimeo等のiframe
        for iframe in body.find_all("iframe"):
            src = (iframe.get("src") or "").lower()

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
        return bool(
            self._select_ui_elements(
                soup,
                ".lightbox,.fancybox,[data-lightbox],[class*='lightbox' i],[class*='fancybox' i],[id*='lightbox' i],[id*='fancybox' i]",
            )
        )

    def _has_multilang(self, soup: BeautifulSoup) -> bool:
        """<body>内に実際の多言語切り替えUIが存在するか判定する。"""
        body = self._body(soup)
        body_html = str(body).lower()

        # 明確な言語切り替え用クラス・ID・属性
        if self._select_ui_elements(
            soup,
            "[class*='language' i],[class*='lang-' i],[class*='lang_' i],[id*='language' i],[id*='lang-' i],[id*='lang_' i],[class*='言語' i],[id*='言語' i]",
        ):
            return True

        return any(keyword in body_html for keyword in self.MULTILANG_KEYWORDS)

    def _has_scroll_animation(self, soup: BeautifulSoup) -> bool:
        """<body>内にスクロールアニメーション関連の実装が存在するか判定する。"""
        body = self._body(soup)
        body_html = str(body).lower()

        return any(keyword in body_html for keyword in self.SCROLL_ANIMATION_KEYWORDS)

    #: クロール側(crawler.py)が、実際に確認できた件数・深さの下限値を
    #: 表すために使う共通のサフィックス("100以上"/"36以上"等)。
    LOWER_BOUND_SUFFIX = "以上"

    @classmethod
    def parse_lower_bound(cls, value: int | str) -> tuple[int, bool]:
        """int、または"{数値}以上"形式の文字列を(数値, 信頼できる下限値かどうか)に変換する。

        クロール側(crawler.py)は、100ページ上限到達時や、時間切れで途中
        打ち切られた時("要確認"にはならず件数が確定しているケース)に、
        "100以上"/"36以上"のような「{数値}以上」形式の文字列を返してくる
        (ページ数・階層数どちらも同じ表記)。これは実際に確認できた件数・
        深さの下限値(クロールを続ければ増えることはあっても減ることはない)
        であり、判定の根拠として信頼してよい確定情報である。一方、"要確認"
        のように「以上」が付かない文字列や数値化できない文字列は、下限値
        としてすら信用できない値のため(0, False)を返す。
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
        """クロール結果から調査結果（◯/×/要確認）と理由を一括で決定する。

        「そもそも判定不能として要確認に倒すか」という、接続失敗・ページ数極小・
        階層数計測不能といった特殊ケースの判断もここに集約する。
        evaluate() を直接呼ぶより、こちらを呼ぶことを推奨する。

        queue_exhausted は、クロール側が発見した内部リンクをすべて訪問し終えて
        自然にキューが空になったか(True)、タイムアウト等で未訪問のリンクを
        残したまま打ち切ったか(False)を表す。

        以前は、total_pagesが整数値として返っている(=100ページ上限には
        達していない)のにqueue_exhaustedがFalseの場合、ページ数・階層数の
        多寡に関わらず一律「要確認」に倒していた。しかしこれには不具合があった。
        100ページ上限に達したことで打ち切られたケース(total_pagesは
        "100以上"の確定値としてこの時点では既にintの100に変換済み)
        でもqueue_exhaustedはほぼ必ずFalse(=上限到達時点でまだキューに
        未訪問URLが残っている)になるため、「少なくとも100ページある」という
        確定情報にもかかわらず判定そのものが「要確認」に握りつぶされ、
        本来M列に出るべき「ページ数が多いため」等サイト固有の理由が
        一切表示されなくなっていた(fcs.or.jp・ferie.co.jp等、多数の
        個別記事ページを持つサイトで確認)。ページ数・ログイン機能の有無
        等、一度確定した「×」の根拠は、クロールを続けても消えたり減ったり
        しない(訪問ページが増える方向にしか動かない)ため、queue_exhausted
        がFalseであっても、既に確定している判定結果はそのまま採用すべき
        である。

        そのため現在は、まず（信頼できる情報の範囲で）通常の評価ロジック
        （evaluate）を実行し、それだけで既に「×」の根拠が1つでも
        見つかっていればqueue_exhaustedの状態に関わらずそれを採用する。
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

        # ページ数・階層数が"要確認"(=1ページあたりのリダイレクトが異常に
        # 多いサイト等で、到達件数自体が実態の下限値としてすら信用できない
        # ケース)の場合は、それを根拠にした判定は行わない(0件扱いで
        # evaluateへ渡す)。それ以外("36以上"のような時間切れ時の下限値
        # 確定表記や、通常の整数値)は、実際にクロールできた件数・到達できた
        # 深さの下限値として信頼できるため、そのまま根拠に使う。
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

        return eval_result, reason

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

        接続失敗・ページ数極小・階層数計測不能などの特殊ケースを考慮せず、
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

        if self._has_search(soup, html_lower):
            reasons.append("サイト内検索・絞り込み検索機能があるため")

        if self._has_chatbot(soup):
            reasons.append("チャットボットが導入されているため")

        # --- デザイン・ギミック面 ---
        if self._has_video(soup):
            reasons.append("トップイメージ等に動画が使用されているため")

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

        if self._has_scroll_animation(soup):
            reasons.append("スクロールアニメーション（GSAP等）が多用されているため")

        if reasons:
            return "×", "\n".join(reasons)
        return "◯", ""
