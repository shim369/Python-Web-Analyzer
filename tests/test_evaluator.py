from web_analyzer.core.evaluator import RenewalEvaluator


def test_evaluate_and_reason() -> None:
    evaluator = RenewalEvaluator()

    # max_depth は「トップページ=1階層目」とする1始まりの表示値。
    # 2階層（トップページ + 1階層下まで）は「3階層以上」に該当しないため◯になるケース：
    # タプルの1番目（評価結果）を比較
    status, _ = evaluator.evaluate(
        total_pages=5,
        max_depth=2,
        has_login=False,
        has_attachment=False,
    )
    assert status == "◯"

    # ページ数オーバー
    status, _ = evaluator.evaluate(
        total_pages=11,
        max_depth=2,
        has_login=False,
        has_attachment=False,
    )
    assert status == "×"

    # ログインあり
    status, reason = evaluator.evaluate(
        total_pages=5,
        max_depth=2,
        has_login=True,
        has_attachment=False,
    )
    assert status == "×"
    assert "ログイン" in reason

    # 添付あり
    status, _ = evaluator.evaluate(
        total_pages=5,
        max_depth=2,
        has_login=False,
        has_attachment=True,
    )
    assert status == "×"

    # 階層数が3（＝3階層以上）になるとNGになる境界値の確認
    status, reason = evaluator.evaluate(
        total_pages=5,
        max_depth=3,
        has_login=False,
        has_attachment=False,
    )
    assert status == "×"
    assert "3階層以上" in reason
