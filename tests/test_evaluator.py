from web_analyzer.core.evaluator import RenewalEvaluator


def test_evaluate_rank_and_reason() -> None:
    # しきい値: ◎(<=10), ◯(<=15), △(<=20)
    evaluator = RenewalEvaluator(threshold_1=10, threshold_2=15, threshold_3=20)

    # ケース1: 10ページ以下、CMS関係なし -> ◎
    assert evaluator.evaluate_rank(cms_name="", total_pages=5) == "◎"
    assert evaluator.evaluate_rank(cms_name="WordPress", total_pages=5) == "◎"  # CMSがあっても◎！
    assert evaluator.compile_rejection_reason(total_pages=5, has_login=False) == ""

    # ケース2: 11〜15ページ -> ◯
    assert evaluator.evaluate_rank(cms_name="", total_pages=12) == "◯"
    assert evaluator.evaluate_rank(cms_name="WordPress", total_pages=12) == "◯"

    # ケース3: 20ページ超過 -> ×
    assert evaluator.evaluate_rank(cms_name="", total_pages=25) == "×"
    assert evaluator.compile_rejection_reason(total_pages=25, has_login=False) == "ページ数が多いため"

    # ケース4: ログイン機能あり
    assert evaluator.compile_rejection_reason(total_pages=5, has_login=True) == "外部非公開のログイン機能（会員限定ページなど）が存在するため"
