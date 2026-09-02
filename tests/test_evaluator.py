from typing import Any

from web_analyzer.core.evaluator import RenewalEvaluator


def test_evaluate_ok_when_within_thresholds() -> None:
    evaluator = RenewalEvaluator()

    # max_depth は「トップページ=1階層目」とする1始まりの表示値。
    # 2階層（トップページ + 1階層下まで）は「3階層以上」に該当しないため◯になるケース。
    status, reason = evaluator.evaluate(
        total_pages=5,
        max_depth=2,
        has_login=False,
        has_attachment=False,
    )
    assert status == "◯"
    assert reason == ""


def test_evaluate_ng_reasons() -> None:
    evaluator = RenewalEvaluator()

    # ページ数オーバー（デフォルトのpage_threshold=10）
    status, reason = evaluator.evaluate(total_pages=11, max_depth=2, has_login=False, has_attachment=False)
    assert status == "×"
    assert "ページ数が多い" in reason

    # 階層数が3（＝3階層以上）になるとNGになる境界値の確認
    status, reason = evaluator.evaluate(total_pages=5, max_depth=3, has_login=False, has_attachment=False)
    assert status == "×"
    assert "3階層以上" in reason

    # ログインあり
    status, reason = evaluator.evaluate(total_pages=5, max_depth=2, has_login=True, has_attachment=False)
    assert status == "×"
    assert "ログイン" in reason

    # 添付あり
    status, reason = evaluator.evaluate(total_pages=5, max_depth=2, has_login=False, has_attachment=True)
    assert status == "×"
    assert "添付" in reason

    # ベーシック認証あり
    status, reason = evaluator.evaluate(total_pages=5, max_depth=2, has_login=False, has_attachment=False, has_basic_auth=True)
    assert status == "×"
    assert "ベーシック認証" in reason

    # 多言語対応あり
    status, reason = evaluator.evaluate(total_pages=5, max_depth=2, has_login=False, has_attachment=False, has_multilang=True)
    assert status == "×"
    assert "多言語" in reason


def test_evaluate_respects_custom_page_threshold() -> None:
    evaluator = RenewalEvaluator()

    # page_threshold=5 の場合、6ページ以上でNGになる（デフォルトの10ではNGにならない値）
    status, reason = evaluator.evaluate(total_pages=6, max_depth=1, has_login=False, has_attachment=False, page_threshold=5)
    assert status == "×"
    assert "ページ数が多い" in reason


def test_evaluate_html_based_detectors() -> None:
    evaluator = RenewalEvaluator()
    base_kwargs: dict[str, Any] = dict(total_pages=5, max_depth=1, has_login=False, has_attachment=False)

    cases = {
        "captcha": ('<div class="g-recaptcha"></div>', "画像認証"),
        "chatbot": ('<script src="https://widget.intercom.io/x.js"></script>', "チャットボット"),
        "calendar": ('<div class="wp-calendar"></div>', "カレンダー"),
        "search": ('<input type="search" name="q">', "検索"),
        "video": ('<video src="movie.mp4"></video>', "動画"),
        "floating": ('<div class="floating">', "フローティング"),
        "accordion": ("<details><summary>Q</summary>A</details>", "アコーディオン"),
        "tabs": ('<div role="tab">tab1</div>', "タブ"),
        "modal": ('<div class="modal"></div>', "モーダル"),
        "gallery": ('<a data-lightbox="group1" href="a.jpg">img</a>', "ギャラリー"),
        "multilang_keyword": ('<div class="lang-select"></div>', "多言語"),
        "scroll_animation": ('<div data-aos="fade-up"></div>', "スクロールアニメーション"),
    }

    for name, (html_fragment, expected_reason_substr) in cases.items():
        status, reason = evaluator.evaluate(html_src=f"<html><body>{html_fragment}</body></html>", **base_kwargs)
        assert status == "×", f"{name}: 検知されるはずがNGにならなかった"
        assert expected_reason_substr in reason, f"{name}: 理由に'{expected_reason_substr}'が含まれない -> {reason}"


def test_evaluate_ok_with_clean_html() -> None:
    evaluator = RenewalEvaluator()
    status, reason = evaluator.evaluate(
        total_pages=5,
        max_depth=1,
        has_login=False,
        has_attachment=False,
        html_src="<html><body><p>普通のサイトです。</p></body></html>",
    )
    assert status == "◯"
    assert reason == ""


def test_decide_pending_when_connection_failed() -> None:
    evaluator = RenewalEvaluator()

    # total_pages=0 は「接続不可またはアクセス拒否」を意味し、要確認に倒す
    status, reason = evaluator.decide(total_pages=0, max_depth=0, has_login=False, has_attachment=False)
    assert status == "要確認"


def test_decide_ng_when_zero_pages_with_basic_auth() -> None:
    evaluator = RenewalEvaluator()

    # 0ページでも、ベーシック認証が原因と分かっている場合は「要確認」ではなく明確に×
    status, reason = evaluator.decide(total_pages=0, max_depth=0, has_login=False, has_attachment=False, has_basic_auth=True)
    assert status == "×"
    assert "ベーシック認証" in reason


def test_decide_pending_when_too_few_pages_and_queue_not_exhausted() -> None:
    evaluator = RenewalEvaluator()

    # 1〜2ページしか取れず、かつキューが自然消化していない(=タイムアウト等で打ち切られた疑いがある)場合は要確認
    status, _ = evaluator.decide(total_pages=1, max_depth=1, has_login=False, has_attachment=False, queue_exhausted=False)
    assert status == "要確認"


def test_decide_proceeds_normally_when_queue_exhausted_even_if_few_pages() -> None:
    evaluator = RenewalEvaluator()

    # ページ数が少なくても、内部リンクを巡回し尽くして自然に終わっていれば
    # 「本当にページ数の少ないサイト」とみなし通常の判定ロジックへ進む
    status, _ = evaluator.decide(total_pages=1, max_depth=1, has_login=False, has_attachment=False, queue_exhausted=True)
    assert status == "◯"


def test_decide_ng_when_page_cap_reached_even_if_queue_not_exhausted() -> None:
    evaluator = RenewalEvaluator()

    # 100ページ上限に達した(total_pages=100)場合、queue_exhaustedは通常False
    # (上限到達時点でまだキューに未訪問URLが残っているため)になるが、
    # 「少なくとも100ページある」こと自体は確定情報であり、「要確認」に
    # 握りつぶさず通常通り「ページ数が多いため」で×判定するべき
    # (fcs.or.jp/ferie.co.jp等で確認)。
    status, reason = evaluator.decide(total_pages=100, max_depth=1, has_login=False, has_attachment=False, queue_exhausted=False)
    assert status == "×"
    assert "ページ数が多い" in reason


def test_decide_ng_when_login_confirmed_even_if_queue_not_exhausted() -> None:
    evaluator = RenewalEvaluator()

    # ログイン機能の有無はクロールが途中終了していても既に確定した事実であり、
    # 続きを巡回しても覆らないため「要確認」に握りつぶすべきではない。
    status, reason = evaluator.decide(total_pages=3, max_depth=1, has_login=True, has_attachment=False, queue_exhausted=False)
    assert status == "×"
    assert "ログイン" in reason


def test_decide_pending_when_max_depth_unmeasurable() -> None:
    evaluator = RenewalEvaluator()

    status, reason = evaluator.decide(total_pages=5, max_depth="要確認", has_login=False, has_attachment=False)
    assert status == "要確認"
    assert "階層" in reason


def test_decide_delegates_to_evaluate_for_normal_case() -> None:
    evaluator = RenewalEvaluator()

    status, reason = evaluator.decide(total_pages=20, max_depth=1, has_login=False, has_attachment=False)
    assert status == "×"
    assert "ページ数が多い" in reason
