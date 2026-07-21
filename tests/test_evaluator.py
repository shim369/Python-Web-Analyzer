from web_analyzer.core.evaluator import RenewalEvaluator


def test_evaluate_rank_and_reason() -> None:
    # しきい値: ◎(<=10), ◯(<=15), △(<=20)
    evaluator = RenewalEvaluator(threshold_1=10, threshold_2=15, threshold_3=20)

    # ケース1: 10ページ以下、CMS関係なし -> CMS未検出なら◎
    assert evaluator.evaluate_rank(cms_name="", total_pages=5) == "◎"

    # 仕様変更に合わせて、CMS（WordPress）が検出された場合は「×」が返ることを期待する
    assert evaluator.evaluate_rank(cms_name="WordPress", total_pages=5) == "×"
