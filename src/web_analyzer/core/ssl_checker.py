from urllib.parse import urlparse

import httpx


class SslChecker:
    """WebサイトのSSL状態（SSLあり・常時SSL）を判定するクラス。"""

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout
        self.headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) python-web-analyzer"}

    def _normalize_url(self, url_or_domain: str) -> str:
        """入力された文字列からドメインを抽出し、検証用のhttp:// URLを生成する。"""
        if not url_or_domain.startswith(("http://", "https://")):
            url_or_domain = f"http://{url_or_domain}"

        parsed = urlparse(url_or_domain)
        domain = parsed.netloc if parsed.netloc else parsed.path
        domain = domain.split(":")[0].split("/")[0]

        return f"http://{domain}"

    def check_ssl_status(self, target: str) -> tuple[bool, bool]:
        """対象サイトの『SSLあり』と『常時SSL』を判定する。

        Args:
            target: 検検証対象のURLまたはドメイン名

        Returns:
            tuple[bool, bool]: (has_ssl, is_always_ssl) の真偽値ペア
        """
        test_url = self._normalize_url(target)

        try:
            with httpx.Client(headers=self.headers, timeout=self.timeout, follow_redirects=True) as client:
                response = client.get(test_url)

                final_url = str(response.url)
                is_https = final_url.startswith("https://")
                has_redirects = len(response.history) > 0

                # C列用：最終的にHTTPSに繋がれば、有効なSSL証明書があるとみなす
                has_ssl = is_https

                # D列用：最終URLがhttpsであり、かつhttpからリダイレクトされた履歴がある場合
                is_always_ssl = is_https and has_redirects

                return has_ssl, is_always_ssl

        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError):
            # 既存のエラーハンドリングを活かしつつ、失敗時は両方Falseを返す
            return False, False
        except httpx.RequestError:
            return False, False
