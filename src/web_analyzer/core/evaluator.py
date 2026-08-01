class RenewalEvaluator:
    """Webサイトのリニューアル可否を複数の判定基準から総合的に判定する。"""

    def evaluate(
        self,
        total_pages: int,
        max_depth: int,
        has_login: bool,
        has_attachment: bool,
        html_src: str = "",
        page_threshold: int = 10,
    ) -> tuple[str, str]:
        """判定結果(◯/×)と、NGの場合の理由（改行区切り文字列）を一括で返す。"""
        reasons = []

        if total_pages > page_threshold:
            reasons.append(f"ページ数が多いため ({total_pages}ページ)")

        if max_depth > 2:
            reasons.append("サイト構成が3階層以上のため")

        if has_login:
            reasons.append("ログイン・マイページ機能があるため")

        if has_attachment:
            reasons.append("お問い合わせフォームに添付機能があるため")

        if html_src:
            html_lower = html_src.lower()

            if "lightbox" in html_lower or "fancybox" in html_lower or "data-lightbox" in html_lower:
                reasons.append("ギャラリーコンテンツ（Lightbox等）が導入されているため")

            if "translate.google" in html_lower or any(k in html_lower for k in ["language-list", "lang-select", "言語切り替え"]):
                reasons.append("多言語対応（言語切り替え機能）があるため")

            if any(k in html_lower for k in ["gsap", "scrolltrigger", "data-aos", "locomotive-scroll"]):
                reasons.append("スクロールアニメーション（GSAP等）が多用されているため")

        if reasons:
            return "×", "\n".join(reasons)
        return "◯", ""
