from web_analyzer.core.evaluator import RenewalEvaluator


def test_evaluate_rank_and_reason() -> None:
    evaluator = RenewalEvaluator()

    # ○になるケースの検証 (引数に html_src と page_threshold を追加)
    rank, reason = evaluator.evaluate(total_pages=5, max_depth=2, has_login=False, has_attachment=False, html_src="", page_threshold=10)
    assert rank == "◯"
    assert reason == ""
