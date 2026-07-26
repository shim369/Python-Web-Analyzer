class RenewalEvaluator:
    """Webサイトのリニューアル可否を判定する。"""

    def compile_rejection_reason(
        self,
        total_pages: int,
        max_depth: int,
        has_login: bool,
        has_attachment: bool,
    ) -> str:
        reasons = []

        if total_pages > 10:
            reasons.append("ページ数が11ページ以上のため")

        if max_depth > 2:
            reasons.append("サイト構成が3階層以上のため")

        if has_login:
            reasons.append("ログイン・マイページ機能があるため")

        if has_attachment:
            reasons.append("お問い合わせフォームに添付機能があるため")

        return "\n".join(reasons)

    def evaluate_rank(
        self,
        total_pages: int,
        max_depth: int,
        has_login: bool,
        has_attachment: bool,
    ) -> str:

        if total_pages > 10:
            return "×"

        if max_depth > 2:
            return "×"

        if has_login:
            return "×"

        if has_attachment:
            return "×"

        return "〇"
