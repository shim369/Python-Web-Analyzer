from collections.abc import Callable

import pytest
import requests
from _pytest.monkeypatch import MonkeyPatch

from web_analyzer.core.ssl_checker import SslChecker


class FakeResponse:
    def __init__(self, url: str, status_code: int = 200) -> None:
        self.url = url
        self.status_code = status_code


class FakeSession:
    """URLごとに固定の戻り値/例外を返す偽のrequests.Session。

    routes は {url: FakeResponse か Exception のインスタンス} の辞書。
    実際に外部へ通信せず、SslCheckerの分岐ロジックだけを検証するために使う。
    """

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def get(
        self,
        url: str,
        headers: object = None,
        timeout: object = None,
        allow_redirects: bool = True,
    ) -> object:
        self.calls.append(url)
        result = self.routes.get(url)
        if result is None:
            raise AssertionError(f"想定外のURLへのアクセスがありました: {url}")
        if isinstance(result, Exception):
            raise result
        return result

    def close(self) -> None:
        pass


@pytest.fixture
def patch_session(
    monkeypatch: MonkeyPatch,
) -> Callable[[SslChecker, dict[str, object]], FakeSession]:
    def _patch(checker: SslChecker, routes: dict[str, object]) -> FakeSession:
        fake = FakeSession(routes)
        monkeypatch.setattr(checker, "_get_session", lambda: fake)
        return fake

    return _patch


def test_ssl_checker_init() -> None:
    checker = SslChecker()
    assert checker.timeout == 10.0


def test_always_ssl_when_http_redirects_to_https(
    patch_session: Callable[[SslChecker, dict[str, object]], FakeSession],
) -> None:
    checker = SslChecker()
    patch_session(
        checker,
        {
            "http://example.com": FakeResponse(url="https://example.com"),
        },
    )

    has_ssl, is_always_ssl = checker.check_ssl_status("example.com")
    assert has_ssl is True
    assert is_always_ssl is True


def test_ssl_available_but_not_forced(
    patch_session: Callable[[SslChecker, dict[str, object]], FakeSession],
) -> None:
    checker = SslChecker()
    patch_session(
        checker,
        {
            # httpのまま(リダイレクトなし)だが、httpsへ直接繋いだ場合は200が返るケース
            "http://example.com": FakeResponse(url="http://example.com"),
            "https://example.com": FakeResponse(url="https://example.com", status_code=200),
        },
    )

    has_ssl, is_always_ssl = checker.check_ssl_status("example.com")
    assert has_ssl is True
    assert is_always_ssl is False


def test_no_ssl_support_at_all(
    patch_session: Callable[[SslChecker, dict[str, object]], FakeSession],
) -> None:
    checker = SslChecker()
    patch_session(
        checker,
        {
            "http://example.com": FakeResponse(url="http://example.com"),
            "https://example.com": requests.exceptions.ConnectionError("https unreachable"),
        },
    )

    has_ssl, is_always_ssl = checker.check_ssl_status("example.com")
    assert has_ssl is False
    assert is_always_ssl is False


def test_connection_failure_on_both_schemes_returns_unknown(
    patch_session: Callable[[SslChecker, dict[str, object]], FakeSession],
) -> None:
    checker = SslChecker()
    patch_session(
        checker,
        {
            "http://example.com": requests.exceptions.ConnectionError("refused"),
            "https://example.com": requests.exceptions.ConnectionError("refused"),
        },
    )

    has_ssl, is_always_ssl = checker.check_ssl_status("example.com")
    assert has_ssl is None
    assert is_always_ssl is None


def test_cert_mismatch_retries_with_www_domain(monkeypatch: MonkeyPatch) -> None:
    """証明書のホスト名不一致(SSLError)の場合のみ、www付きドメインで自動的に再試行することを確認する。"""
    checker = SslChecker()

    routes = {
        "http://cert-mismatch.example.com": requests.exceptions.SSLError("hostname mismatch"),
        "https://cert-mismatch.example.com": requests.exceptions.SSLError("hostname mismatch"),
        # www付きでは正常にhttpsへリダイレクトする
        "http://www.cert-mismatch.example.com": FakeResponse(url="https://www.cert-mismatch.example.com"),
    }
    fake = FakeSession(routes)
    monkeypatch.setattr(checker, "_get_session", lambda: fake)

    has_ssl, is_always_ssl = checker.check_ssl_status("cert-mismatch.example.com")

    assert has_ssl is True
    assert is_always_ssl is True
    # www付きドメインへの再試行が実際に発生したことを確認
    assert "http://www.cert-mismatch.example.com" in fake.calls


def test_generic_connection_error_does_not_retry_with_www(
    patch_session: Callable[[SslChecker, dict[str, object]], FakeSession],
) -> None:
    """証明書エラーではない単純な接続失敗では、www付きへの再試行は行わないことを確認する。"""
    checker = SslChecker()
    fake = patch_session(
        checker,
        {
            "http://unreachable.example.com": requests.exceptions.ConnectionError("refused"),
            "https://unreachable.example.com": requests.exceptions.ConnectionError("refused"),
        },
    )

    has_ssl, is_always_ssl = checker.check_ssl_status("unreachable.example.com")

    assert has_ssl is None
    assert is_always_ssl is None
    assert not any("www." in url for url in fake.calls)


def test_www_domain_input_does_not_trigger_further_retry(
    patch_session: Callable[[SslChecker, dict[str, object]], FakeSession],
) -> None:
    """入力が既にwww付きの場合、さらにwww.www...と再試行しないことを確認する。"""
    checker = SslChecker()
    fake = patch_session(
        checker,
        {
            "http://www.example.com": requests.exceptions.SSLError("hostname mismatch"),
            "https://www.example.com": requests.exceptions.SSLError("hostname mismatch"),
        },
    )

    has_ssl, is_always_ssl = checker.check_ssl_status("www.example.com")

    assert has_ssl is None
    assert is_always_ssl is None
    assert not any("www.www." in url for url in fake.calls)
