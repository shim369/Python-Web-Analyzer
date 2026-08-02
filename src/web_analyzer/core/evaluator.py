class RenewalEvaluator:
    """Webサイトのリニューアル可否を複数の判定基準から総合的に判定する。"""

    CAPTCHA_KEYWORDS = ["captcha", "認証コード", "ccm-captcha-image"]
    CHATBOT_KEYWORDS = ["sinclo", "chamo", "zendesk", "channel.io", "hubspot-messages", "chatbot"]
    CALENDAR_KEYWORDS = ["wp-calendar", "xo-event-calendar", "calendar"]
    SEARCH_KEYWORDS = ["絞り込み検索", "条件検索", "サイト内検索"]
    RICH_UI_KEYWORDS = ["lightbox", "fancybox", "data-lightbox"]
    MULTILANG_KEYWORDS = ["translate.google", "language-list", "lang-select", "言語切り替え"]
    SCROLL_ANIMATION_KEYWORDS = ["gsap", "scrolltrigger", "data-aos", "locomotive-scroll"]

    PDF_LINK_THRESHOLD = 5  # この件数以上PDFリンクがあれば「資料が多い」と判定

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
        """判定結果(◯/×)と、NGの場合の理由（改行区切り文字列）を一括で返す。"""
        reasons: list[str] = []
        html_lower = html_src.lower()

        # --- ページ数・階層構成 ---
        if total_pages > page_threshold:
            reasons.append(f"ページ数が多いため ({total_pages}ページ)")

        if max_depth > 2:
            reasons.append("サイト構成が3階層以上のため")

        # --- ログイン・添付機能（クローラー側で検知済みの値を使用） ---
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
