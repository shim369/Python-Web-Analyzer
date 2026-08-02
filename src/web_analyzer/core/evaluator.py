class RenewalEvaluator:
    """Webサイトのリニューアル可否を複数の判定基準から総合的に判定する。"""

    CAPTCHA_KEYWORDS = ["captcha", "認証コード", "ccm-captcha-image"]
    CHATBOT_KEYWORDS = ["sinclo", "chamo", "zendesk", "channel.io", "hubspot-messages", "chatbot"]
    CALENDAR_KEYWORDS = ["wp-calendar", "xo-event-calendar", "calendar"]
    SEARCH_KEYWORDS = ["絞り込み検索", "条件検索", "サイト内検索"]
    RICH_UI_KEYWORDS = ["lightbox", "fancybox", "data-lightbox"]
    MULTILANG_KEYWORDS = ["translate.google", "language-list", "lang-select", "言語切り替え"]
    SCROLL_ANIMATION_KEYWORDS = ["gsap", "scrolltrigger", "data-aos", "locomotive-scroll"]
    VIDEO_KEYWORDS = ["<video", "youtube.com/embed", "vimeo.com"]
    FLOATING_BUTTON_KEYWORDS = ["floating", "fixed-btn", "pagetop", "to-top", "follow-window"]
    ACCORDION_KEYWORDS = ["accordion"]
    TAB_KEYWORDS = ["tab-content", "nav-tabs"]
    MODAL_KEYWORDS = ["modal-window", "modal-dialog"]

    PDF_LINK_THRESHOLD = 5  # この件数以上PDFリンクがあれば「資料が多い」と判定

    def decide(
        self,
        total_pages: int | str,
        max_depth: int | str,
        has_login: bool,
        has_attachment: bool,
        has_basic_auth: bool = False,
        html_src: str = "",
        page_threshold: int = 10,
    ) -> tuple[str, str]:
        """クロール結果から調査結果（◯/×/要確認）と理由を一括で決定する。

        「そもそも判定不能として要確認に倒すか」という、接続失敗・ページ数極小・
        階層数計測不能といった特殊ケースの判断もここに集約する。
        evaluate() を直接呼ぶより、こちらを呼ぶことを推奨する。
        """
        if total_pages == 0:
            return "要確認", "接続不可またはアクセス拒否のため、判定を保留しました。"

        if isinstance(total_pages, int) and 1 <= total_pages <= 2:
            return "要確認", f"クロールできたページ数が極端に少ないため判定を保留しました (取得数: {total_pages}ページ)。"

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
            reasons.append(f"ページ数が多いため ({total_pages}ページ)")

        if max_depth > 2:
            reasons.append("サイト構成が3階層以上のため")

        # --- ログイン・添付・認証機能（クローラー側で検知済みの値を使用） ---
        if has_login:
            reasons.append("ログイン・マイページ機能があるため")

        if has_attachment:
            reasons.append("お問い合わせフォームに添付機能があるため")

        if has_basic_auth:
            reasons.append("ベーシック認証がかかっているページがあるため")

        if not html_lower:
            if reasons:
                return "×", "\n".join(reasons)
            return "◯", ""

        # --- お問い合わせフォームの特殊仕様 ---
        if any(k in html_lower for k in self.CAPTCHA_KEYWORDS):
            reasons.append("お問い合わせフォームで画像認証（Captcha）を使用しているため")

        if "step" in html_lower and ("form" in html_lower or "入力" in html_lower):
            reasons.append("お問い合わせフォームがステップ型入力のため")

        # --- 専用機能の検知 ---
        if any(k in html_lower for k in self.CALENDAR_KEYWORDS) or "カレンダー" in html_lower:
            reasons.append("カレンダー機能が実装されているため")

        if 'type="search"' in html_lower or any(k in html_lower for k in self.SEARCH_KEYWORDS):
            reasons.append("サイト内検索・絞り込み検索機能があるため")

        if any(k in html_lower for k in self.CHATBOT_KEYWORDS):
            reasons.append("チャットボットが導入されているため")

        if html_lower.count(".pdf") >= self.PDF_LINK_THRESHOLD:
            reasons.append("PDF資料・ダウンロードデータが多いため")

        # --- デザイン・ギミック面 ---
        if any(k in html_lower for k in self.VIDEO_KEYWORDS):
            reasons.append("トップイメージ等に動画が使用されているため")

        if any(k in html_lower for k in self.FLOATING_BUTTON_KEYWORDS):
            reasons.append("フローティングボタン（画面追従ボタン）があるため")

        if any(k in html_lower for k in self.ACCORDION_KEYWORDS):
            reasons.append("アコーディオンが使用されているため")

        if any(k in html_lower for k in self.TAB_KEYWORDS):
            reasons.append("タブ切り替えが使用されているため")

        if any(k in html_lower for k in self.MODAL_KEYWORDS):
            reasons.append("モーダルウィンドウが使用されているため")

        # --- リッチコンテンツ ---
        if any(k in html_lower for k in self.RICH_UI_KEYWORDS):
            reasons.append("ギャラリーコンテンツ（Lightbox等）が導入されているため")

        if any(k in html_lower for k in self.MULTILANG_KEYWORDS):
            reasons.append("多言語対応（言語切り替え機能）があるため")

        if any(k in html_lower for k in self.SCROLL_ANIMATION_KEYWORDS):
            reasons.append("スクロールアニメーション（GSAP等）が多用されているため")

        if reasons:
            return "×", "\n".join(reasons)
        return "◯", ""
