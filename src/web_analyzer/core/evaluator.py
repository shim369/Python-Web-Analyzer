class RenewalEvaluator:
    """収集したサイトデータに基づき、Webサイトのリニューアル適性ランク（◎/〇/×）および不可理由を判定するクラス。"""

    def __init__(
        self,
        threshold_1: int = 30,
        threshold_2: int = 50,
        threshold_3: int = 100,
    ) -> None:
        self.threshold_1 = threshold_1
        self.threshold_2 = threshold_2
        self.threshold_3 = threshold_3

    def compile_rejection_reason(
        self,
        total_pages: int,
        has_login: bool,
        site_purpose: str = "",
        html_src: str = "",
    ) -> str:
        """判定ロジックに基づいて不適合の理由テキストを構築する。"""
        reasons = []

        # 1. ページ数に関する判定（0ページ = 取得不可、上限超え = 多すぎる）
        if total_pages == 0:
            reasons.append("Webサイトへのアクセス・データ取得ができなかったため")
        elif total_pages > self.threshold_3:
            reasons.append("ページ数が多いため")

        # 2. 特定のサイト用途（物件検索など）
        if site_purpose == "物件検索サイト":
            reasons.append("独自の埋め込みシステム（物件検索等）が存在するため")

        # 3. ログイン機能
        if has_login:
            reasons.append("ログイン機能（マイページ、会員システムなど）があるため")

        # 4. 各種リッチコンテンツ・機能の検知 (HTMLソース解析)
        if html_src:
            html_lower = html_src.lower()

            # Lightbox や Fancybox などのギャラリーコンテンツ検知
            if "lightbox" in html_lower or "fancybox" in html_lower or "data-lightbox" in html_lower:
                reasons.append("ギャラリーコンテンツ（Lightbox等）が導入されているため")

            # 多言語対応（言語切り替え機能）の検知
            if "translate.google" in html_lower or any(k in html_lower for k in ["language-list", "lang-select", "言語切り替え"]):
                reasons.append("多言語対応（言語切り替え機能）があるため")

            # スクロールアニメーション（GSAP等）の検知
            # gsap, scrolltrigger, aos (Animate On Scroll), locomotive-scroll などを捕捉
            if any(
                k in html_lower
                for k in [
                    "gsap",
                    "scrolltrigger",
                    "data-aos",
                    "locomotive-scroll",
                ]
            ):
                reasons.append("スクロールアニメーション（GSAP等）が多用されているため")

        if reasons:
            return "\n".join(reasons)
        return ""

    def evaluate_rank(
        self,
        total_pages: int,
        has_login: bool,
        site_purpose: str = "",
        html_src: str = "",
    ) -> str:
        """適性ランクを評価（◎、◯、×）。"""
        reason = self.compile_rejection_reason(
            total_pages=total_pages,
            has_login=has_login,
            site_purpose=site_purpose,
            html_src=html_src,
        )

        if reason != "":
            return "×"

        if total_pages <= self.threshold_1:
            return "◎"
        else:
            return "◯"
