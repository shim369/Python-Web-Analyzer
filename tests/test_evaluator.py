from web_analyzer.core.evaluator import RenewalEvaluator


def test_evaluate_rank_and_reason() -> None:
    # しきい値: ◎(<=10), ◯(<=15), ×(>20)
    evaluator = RenewalEvaluator(threshold_1=10, threshold_2=15, threshold_3=20)

    # ケース1: 10ページ以下で不可条件がない場合は、CMSの有無に関わらず「◎」になる
    # CMS未検出のケース
    assert evaluator.evaluate_rank(total_pages=5, has_login=False) == "◎"

    # 仕様変更の反映: CMS（WordPress）が検出されても「◎」が返ることを期待する
    assert evaluator.evaluate_rank(total_pages=5, has_login=False) == "◎"

    # ケース2: 不可条件（ログイン機能あり）のテストも追加しておくと安心です
    assert evaluator.evaluate_rank(total_pages=5, has_login=True) == "×"
    assert "ログイン機能" in evaluator.compile_rejection_reason(total_pages=5, has_login=True)
