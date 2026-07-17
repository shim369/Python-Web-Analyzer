class RenewalEvaluator:
    """Webサイトのリニューアル（移行）可否を、ページ数や機能制限の条件から判定・評価するクラス。"""

    def __init__(self, threshold_1: int = 10, threshold_2: int = 15, threshold_3: int = 20) -> None:
        self.threshold_1 = threshold_1
        self.threshold_2 = threshold_2
        self.threshold_3 = threshold_3

    def evaluate_rank(self, cms_name: str, total_pages: int) -> str:
        """ページ数に基づいてリニューアルの推奨度（◎, ◯, △, ×）のベース評価を行う。"""
        # 1. ページ数がしきい値1（デフォルト10）以下
        if total_pages <= self.threshold_1:
            return "◎"

        # 2. ページ数がしきい値2（デフォルト15）以下
        if total_pages <= self.threshold_2:
            return "◯"

        # 3. ページ数がしきい値3（デフォルト20）以下
        if total_pages <= self.threshold_3:
            return "△"

        # 4. それ以上は移行非推奨
        return "×"

    def compile_rejection_reason(
        self,
        total_pages: int,
        has_login: bool,
        has_search: bool = False,
        has_heavy_animation: bool = False,
        has_floating_sidebar: bool = False,
        is_multilingual: bool = False,
        has_file_upload: bool = False,
    ) -> str:
        """判定が「×」や「△」となる対象外・懸念となる具体的な理由を複合的に判定して出力する。"""

        reasons = []

        # --- 即時対象外（一発で×にする致命的条件） ---
        if has_login:
            reasons.append("ログイン機能・マイページ（会員限定ページなど）が存在するため")

        if is_multilingual:
            reasons.append("多言語サイト対応の構造であるため")

        if has_file_upload:
            reasons.append("フォームにファイル添付機能（履歴書や図面等）があるため")

        if total_pages > self.threshold_1:  # 10P超え（実務判断基準の10P以内をベースに判定）
            reasons.append(f"総ページ数が小規模サイトの基準（10P以内）を超えているため(現在: {total_pages}P)")

        if has_search:
            reasons.append("サイト内検索機能が存在するため")

        if has_heavy_animation:
            reasons.append("高度なリッチアニメーションが多用されているため")

        if has_floating_sidebar:
            reasons.append("追従型のフローティングサイドバーを利用しているため")

        # 理由を綺麗に結合して返却
        if reasons:
            return "【対象外・要確認の理由】 " + " / ".join(reasons)

        return "高度なCMS運用を前提としない、移行に最適なシンプル構成です。"
