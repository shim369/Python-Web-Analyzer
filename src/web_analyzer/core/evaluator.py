class RenewalEvaluator:
    """収集したサイトデータに基づき、Webサイトのリニューアル適性ランク（◎/〇/×/要確認）および不可理由を判定するクラス。"""

    def __init__(self, threshold_1: int = 30, threshold_2: int = 50, threshold_3: int = 100) -> None:
        self.threshold_1 = threshold_1
        self.threshold_2 = threshold_2
        self.threshold_3 = threshold_3

    def compile_rejection_reason(self, total_pages: int, has_login: bool, html_src: str = "") -> str:
        """判定ロジックに基づいて不適合の理由テキストを構築する。"""
        reasons = []

        if total_pages > self.threshold_3:
            reasons.append("ページ数が多いため")

        if has_login:
            reasons.append("ログイン機能（マイページ、会員システムなど）があるため")

        # Lightbox や Fancybox などのギャラリーコンテンツ検知
        html_lower = html_src.lower()
        if "lightbox" in html_lower or "fancybox" in html_lower or "data-lightbox" in html_lower:
            reasons.append("ギャラリーコンテンツ（Lightbox等）が導入されているため")

        # 多言語対応（言語切り替え機能）の検知
        if "translate.google" in html_lower or any(k in html_lower for k in ["language-list", "lang-select", "言語切り替え", 'aria-label="メニュー"']):
            # ※より確実にするため、今回問題になったクラス名やキーワードをフックにします
            reasons.append("多言語対応（言語切り替え機能）があるため")

        if reasons:
            return "\n".join(reasons)
        return ""

    def evaluate_rank(self, cms_name: str, total_pages: int, site_purpose: str = "") -> str:
        """適性ランクを評価（◎、◯、×）"""
        # 物件検索サイトなど、埋め込みシステムが存在する場合は即座に不可
        if site_purpose == "物件検索サイト":
            return "×"

        if cms_name != "":
            return "×"

        if total_pages <= self.threshold_1:
            return "◎"
        elif total_pages <= self.threshold_2:
            return "◯"
        else:
            return "×"
