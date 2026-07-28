class RenewalEvaluator:
    """Webサイトのリニューアル可否を判定する。"""

    def compile_rejection_reason(
        self,
        total_pages: int,
        max_depth: int,
        has_login: bool,
        has_attachment: bool,
        html_src: str = "",
    ) -> str:
        reasons = []

        if total_pages > 10:
            reasons.append("ページ数が多いため")

        if max_depth > 2:
            reasons.append("サイト構成が3階層以上のため")

        if has_login:
            reasons.append("ログイン・マイページ機能があるため")

        if has_attachment:
            reasons.append("お問い合わせフォームに添付機能があるため")

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

        return "\n".join(reasons)

    def evaluate_rank(
        self,
        total_pages: int,
        max_depth: int,
        has_login: bool,
        has_attachment: bool,
        html_src: str = "",
    ) -> str:

        if total_pages > 10:
            return "×"

        if max_depth > 2:
            return "×"

        if has_login:
            return "×"

        if has_attachment:
            return "×"

        if html_src:
            html_lower = html_src.lower()

            if "lightbox" in html_lower or "fancybox" in html_lower or "data-lightbox" in html_lower:
                return "×"

            if "translate.google" in html_lower or any(k in html_lower for k in ["language-list", "lang-select", "言語切り替え"]):
                return "×"

            if any(
                k in html_lower
                for k in [
                    "gsap",
                    "scrolltrigger",
                    "data-aos",
                    "locomotive-scroll",
                ]
            ):
                return "×"

        return "〇"
