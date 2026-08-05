from bs4 import BeautifulSoup


class RenewalEvaluator:
    """Webサイトのリニューアル可否を複数の判定基準から総合的に判定する。"""

    CAPTCHA_KEYWORDS = ["captcha", "g-recaptcha", "hcaptcha", "認証コード", "ccm-captcha-image"]
    CHATBOT_KEYWORDS = ["sinclo", "chamo", "zendesk", "channel.io", "hubspot-messages", "chatbot", "intercom", "crisp", "tidio", "chatplus"]
    CALENDAR_KEYWORDS = ["wp-calendar", "xo-event-calendar"]
    SEARCH_KEYWORDS = ["絞り込み検索", "条件検索", "サイト内検索", "キーワード検索"]
    RICH_UI_KEYWORDS = ["lightbox", "fancybox", "data-lightbox"]
    MULTILANG_KEYWORDS = ["translate.google", "language-list", "lang-select", "言語切り替え"]
    SCROLL_ANIMATION_KEYWORDS = ["gsap", "scrolltrigger", "data-aos", "locomotive-scroll"]
    VIDEO_KEYWORDS = ["<video", "youtube.com/embed", "vimeo.com", "youtu.be", "player.vimeo"]

    def _soup(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html or "", "html.parser")

    def _has_calendar(self, soup: BeautifulSoup) -> bool:
        return bool(soup.select(".wp-calendar,.xo-event-calendar,iframe[src*='calendar.google' i],[class*='calendar' i],[id*='calendar' i]"))

    def _has_accordion(self, soup: BeautifulSoup) -> bool:
        # クラス名の完全一致(.accordion)だけだと、WordPressテーマ等でよくある
        # "js-accordion"・"elementor-accordion"のような接頭辞/接尾辞付きクラス名を
        # 見逃してしまうため、calendarと同様に部分一致([class*=...])も併用する。
        return bool(soup.select(".accordion,[data-bs-toggle='collapse'],details,[class*='accordion' i],[id*='accordion' i]"))

    def _has_tabs(self, soup: BeautifulSoup) -> bool:
        return bool(soup.select("[role='tab'],.nav-tabs,[data-bs-toggle='tab'],[class*='tabs' i],[class*='tab-content' i],[id*='tabs' i]"))

    def _has_modal(self, soup: BeautifulSoup) -> bool:
        return bool(soup.select(".modal,.modal-dialog,[data-bs-toggle='modal'],[class*='modal' i],[id*='modal' i]"))

    def _has_floating(self, soup: BeautifulSoup) -> bool:
        for tag in soup.find_all(True):
            cls = " ".join(tag.get("class", [])).lower()
            # "floating"を復活。外部CSS側でposition:fixedを指定する実装が多く、
            # インラインスタイルのチェックだけでは見逃しやすいクラス名ベースの判定を主にする。
            if any(x in cls for x in ("floating", "pagetop", "to-top", "fixed-btn", "follow-window")):
                return True
            style = (tag.get("style") or "").replace(" ", "").lower()
            if "position:fixed" in style:
                return True
        return False

    def _has_search(self, soup: BeautifulSoup, html: str) -> bool:
        if soup.find("input", {"type": "search"}):
            return True
        return any(x in html for x in self.SEARCH_KEYWORDS)

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
        残したまま打ち切ったか(False)を表す。total_pages が1〜2件と少ない場合、
        この値がTrueであれば「サイトに元々他の内部リンクが存在しない＝本当に
        ページ数の少ないサイト」である可能性が高いと判断し、要確認にせず
        通常の評価ロジック（evaluate）に進む。Falseの場合は、タイムアウトや
        アクセス制限等でクロールが途中終了した可能性が高いため、従来通り
        要確認として保留する。
        """
        if total_pages == 0:
            if has_basic_auth:
                # ベーシック認証で弾かれている場合、クロール自体は0ページで終わるが、
                # これは「接続不可」ではなく明確に判定可能な理由なので、
                # 汎用の「要確認」メッセージより優先する。
                return "×", "ベーシック認証がかかっているページがあるため"
            return "要確認", "接続不可またはアクセス拒否のため、判定を保留しました。"

        if isinstance(total_pages, int) and 1 <= total_pages <= 2 and not queue_exhausted:
            return "要確認", "クロールできたページ数が極端に少ないため判定を保留しました。"

        if max_depth == "要確認":
            return "要確認", "サイト階層が深すぎるため、別途サイトエクスプローラー等での確認をお願いします。"

        try:
            max_depth_int = int(max_depth)
        except (ValueError, TypeError):
            max_depth_int = 0

        return self.evaluate(
            total_pages=int(total_pages),
            max_depth=max_depth_int,
            has_login=has_login,
            has_attachment=has_attachment,
            has_basic_auth=has_basic_auth,
            has_multilang=has_multilang,
            html_src=html_src,
            page_threshold=page_threshold,
        )

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
        if any(k in html_lower for k in self.CAPTCHA_KEYWORDS):
            reasons.append("お問い合わせフォームで画像認証（Captcha）を使用しているため")

        # --- 専用機能の検知 ---
        if self._has_calendar(soup):
            reasons.append("カレンダー機能が実装されているため")

        if self._has_search(soup, html_lower):
            reasons.append("サイト内検索・絞り込み検索機能があるため")

        if any(k in html_lower for k in self.CHATBOT_KEYWORDS):
            reasons.append("チャットボットが導入されているため")

        # --- デザイン・ギミック面 ---
        if any(k in html_lower for k in self.VIDEO_KEYWORDS):
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
        if any(k in html_lower for k in self.RICH_UI_KEYWORDS):
            reasons.append("ギャラリーコンテンツ（Lightbox等）が導入されているため")

        if not has_multilang and any(k in html_lower for k in self.MULTILANG_KEYWORDS):
            reasons.append("多言語対応（言語切り替え機能）があるため")

        if any(k in html_lower for k in self.SCROLL_ANIMATION_KEYWORDS):
            reasons.append("スクロールアニメーション（GSAP等）が多用されているため")

        if reasons:
            return "×", "\n".join(reasons)
        return "◯", ""
