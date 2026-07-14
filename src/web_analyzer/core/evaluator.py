class RenewalEvaluator:
    """しきい値とサイトの特徴から、リニューアル移行の容易さを判定するクラス。"""

    def __init__(self, threshold_1: int = 10, threshold_2: int = 15, threshold_3: int = 20) -> None:
        self.t1 = threshold_1
        self.t2 = threshold_2
        self.t3 = threshold_3

    def evaluate_rank(self, cms_name: str, total_pages: int | None) -> str:
        """しきい値とCMS有無に基づき、調査結果のランク (◎, ◯, △, ×) を動的に判定する。"""
        # ページ数計測がタイムアウトなどにより空欄（None）の場合は判定不可として "×" とする
        if total_pages is None:
            return "×"

        has_cms = bool(cms_name.strip())

        # (1) 使用CMSが空欄、かつ ページ数 ≦ 【閾値1】 ➔ ◎
        if not has_cms and total_pages <= self.t1:
            return "◎"
        # (2) 上記以外、かつ ページ数 ≦ 【閾値2】 ➔ ◯
        elif total_pages <= self.t2:
            return "◯"
        # (3) 上記以外、かつ ページ数 ≦ 【閾値3】 ➔ △
        elif total_pages <= self.t3:
            return "△"
        # (4) いずれにも該当しない（【閾値3】を超える）場合 ➔ ×
        else:
            return "×"

    def compile_rejection_reason(self, total_pages: int | None, has_login: bool) -> str:
        """リニューアル移行不可となる理由テキストを自動生成する（複数合致時は改行区切りで併記）。"""
        reasons: list[str] = []

        # (1) ページ数が 【閾値3】 以上の場合
        if total_pages is not None and total_pages >= self.t3:
            reasons.append("ページ数が多いため")

        # (2) 会員ログイン機能がある場合
        if has_login:
            reasons.append("会員ログイン機能があるため")

        return "\n".join(reasons)
