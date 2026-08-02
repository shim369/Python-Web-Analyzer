from web_analyzer.core.evaluator import RenewalEvaluator


def test_evaluate_rank_and_reason() -> None:
    evaluator = RenewalEvaluator()

    # ○になるケースの検証 (引数に html_src と page_threshold を追加)
    rank, reason = evaluator.evaluate(total_pages=5, max_depth=2, has_login=False, has_attachment=False, html_src="", page_threshold=10)
    assert rank == "◯"
    assert reason == ""


def test_decide_returns_needs_review_when_zero_pages():
    evaluator = RenewalEvaluator()
    result, reason = evaluator.decide(
        total_pages=0,
        max_depth=1,
        has_login=False,
        has_attachment=False,
    )
    assert result == "要確認"
    assert "接続不可" in reason


def test_decide_returns_needs_review_when_pages_too_few():
    evaluator = RenewalEvaluator()
    result, reason = evaluator.decide(
        total_pages=2,
        max_depth=1,
        has_login=False,
        has_attachment=False,
    )
    assert result == "要確認"
    assert "極端に少ない" in reason


def test_decide_returns_needs_review_when_depth_unmeasurable():
    evaluator = RenewalEvaluator()
    result, reason = evaluator.decide(
        total_pages=20,
        max_depth="要確認",
        has_login=False,
        has_attachment=False,
    )
    assert result == "要確認"
    assert "階層が深すぎる" in reason


def test_decide_delegates_to_evaluate_for_normal_case():
    evaluator = RenewalEvaluator()
    result, reason = evaluator.decide(
        total_pages=5,
        max_depth=1,
        has_login=False,
        has_attachment=False,
    )
    assert result == "◯"
    assert reason == ""
