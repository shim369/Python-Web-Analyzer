from typing import Any

import httpx
import pytest

from web_analyzer.core.site_summarizer import SiteSummarizer

TOP_HTML = """
<html><head><title>山田工業株式会社</title></head>
<body><header><nav>ホーム 会社概要 お問い合わせ</nav></header>
<main><h1>山田工業株式会社</h1><p>精密金属加工で地域のものづくりを支えます。</p></main>
<footer>Copyright</footer></body></html>
"""
PROFILE_HTML = "<html><head><title>会社概要 | 山田工業</title></head><body><p>事業内容: 自動車部品の切削加工</p></body></html>"


@pytest.fixture(autouse=True)
def _reset_rate_state() -> None:
    SiteSummarizer._last_request_at = 0.0
    SiteSummarizer._cooldown_until = 0.0


def test_build_source_text_combines_top_and_profile_pages() -> None:
    summarizer = SiteSummarizer(api_key="dummy")
    text = summarizer.build_source_text(TOP_HTML, [PROFILE_HTML])

    assert "精密金属加工" in text
    assert "自動車部品の切削加工" in text
    # ナビ・フッター等の定型部分は含めない
    assert "Copyright" not in text
    assert "お問い合わせ" not in text


def test_summarize_disabled_without_api_key() -> None:
    assert SiteSummarizer(api_key="").summarize("example.com", TOP_HTML) == ""


def test_summarize_returns_cleaned_text(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> httpx.Response:
        captured["url"] = url
        captured["json"] = kwargs["json"]
        body = {"candidates": [{"content": {"parts": [{"text": "「山田工業株式会社のコーポレートサイト。\n自動車部品の切削加工を手がける。」"}]}}]}
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    summarizer = SiteSummarizer(api_key="dummy", model="test-model")
    summarizer.min_interval = 0

    result = summarizer.summarize("yamada.example", TOP_HTML, [PROFILE_HTML], current_description="山田工業株式会社")

    assert result == "山田工業株式会社のコーポレートサイト。自動車部品の切削加工を手がける。"
    assert "test-model" in captured["url"]
    assert "yamada.example" in captured["json"]["contents"][0]["parts"][0]["text"]


def test_summarize_returns_empty_on_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(500, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    summarizer = SiteSummarizer(api_key="dummy")
    summarizer.min_interval = 0

    assert summarizer.summarize("example.com", TOP_HTML) == ""


def test_persistent_rate_limit_triggers_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake_post(url: str, **kwargs: Any) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(SiteSummarizer, "RETRY_WAIT_SEC", 0.0)
    monkeypatch.setattr(SiteSummarizer, "_cooldown_until", 0.0)
    summarizer = SiteSummarizer(api_key="dummy")
    summarizer.min_interval = 0
    summarizer.pop_rate_limited()

    assert summarizer.summarize("a.example", TOP_HTML) == ""
    assert calls["n"] == SiteSummarizer.MAX_RETRIES + 1
    # 失敗の原因が利用上限だったことを呼び出し側が確認できる(確認すると記録は消える)
    assert summarizer.pop_rate_limited() is True
    assert summarizer.pop_rate_limited() is False

    # クールダウン中はAPIを呼ばずに即スキップする(これも利用上限による失敗として記録する)
    assert summarizer.select_global_nav("b.example", [["ホーム", "会社概要", "採用情報"]]) is None
    assert calls["n"] == SiteSummarizer.MAX_RETRIES + 1
    assert summarizer.pop_rate_limited() is True


def test_other_api_errors_are_not_recorded_as_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(500, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(SiteSummarizer, "_cooldown_until", 0.0)
    summarizer = SiteSummarizer(api_key="dummy")
    summarizer.min_interval = 0
    summarizer.pop_rate_limited()

    assert summarizer.summarize("example.com", TOP_HTML) == ""
    assert summarizer.pop_rate_limited() is False
