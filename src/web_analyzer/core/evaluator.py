class RenewalEvaluator:
    """Webサイトのリニューアル（移行）可否を、ページ数などの条件から判定・評価するクラス。"""

    def __init__(self, threshold_1: int = 10, threshold_2: int = 15, threshold_3: int = 20) -> None:
        self.threshold_1 = threshold_1
        self.threshold_2 = threshold_2
        self.threshold_3 = threshold_3

    def evaluate_rank(self, cms_name: str, total_pages: int) -> str:
        """ページ数のみに基づいてリニューアルの推奨度（◎, ◯, △, ×）を評価する。

        ※以前にあった「CMSが導入されていたらランクダウン」という要件は、
          簡素なサイトにおける移行メリットを考慮して撤廃されました。
        """
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

    def compile_rejection_reason(self, total_pages: int, has_login: bool) -> str:
        """判定が「×」となった場合の具体的な理由を出力する。"""
        # ログイン機能がある場合は、最優先でその理由を返却
        if has_login:
            return "外部非公開のログイン機能（会員限定ページなど）が存在するため"

        # ページ数がしきい値3を超えている場合
        if total_pages > self.threshold_3:
            return "ページ数が多いため"

        return ""
