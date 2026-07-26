from web_analyzer.core.evaluator import RenewalEvaluator


def test_evaluate_rank_and_reason() -> None:
    evaluator = RenewalEvaluator()

    # ○になるケース
    assert (
        evaluator.evaluate_rank(
            total_pages=5,
            max_depth=2,
            has_login=False,
            has_attachment=False,
        )
        == "〇"
    )

    # ページ数オーバー
    assert (
        evaluator.evaluate_rank(
            total_pages=11,
            max_depth=2,
            has_login=False,
            has_attachment=False,
        )
        == "×"
    )

    # ログインあり
    assert (
        evaluator.evaluate_rank(
            total_pages=5,
            max_depth=2,
            has_login=True,
            has_attachment=False,
        )
        == "×"
    )

    # 添付あり
    assert (
        evaluator.evaluate_rank(
            total_pages=5,
            max_depth=2,
            has_login=False,
            has_attachment=True,
        )
        == "×"
    )

    reason = evaluator.compile_rejection_reason(
        total_pages=5,
        max_depth=2,
        has_login=True,
        has_attachment=False,
    )

    assert "ログイン" in reason
