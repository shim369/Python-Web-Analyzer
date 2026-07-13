from web_analyzer.core.ssl_checker import SslChecker


def test_ssl_checker_init() -> None:
    checker = SslChecker()
    assert checker.timeout == 10.0
