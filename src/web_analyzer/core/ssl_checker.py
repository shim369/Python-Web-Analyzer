import logging
from urllib.parse import urlparse

import requests

from web_analyzer.utils.decorators import measure_time

logger = logging.getLogger(__name__)


class SslChecker:
    """WebサイトのSSL状態（SSLあり・常時SSL）を判定するクラス。"""

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout
        # WAF/ボット検知を回避するため、標準的なChromeブラウザのリクエストヘッダーを完全模倣
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Sec-Ch-Ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }

    def _get_session(self) -> requests.Session:
        """Simple Session without unnecessary adapters."""
        session = requests.Session()
        # ConnectionResetErrorやタイムアウト時はすぐに次の判定に進むため、自動リトライは外す
        return session

    def _safe_get(
        self, session: requests.Session, url: str, allow_redirects: bool = True
    ) -> requests.Response:
        """ConnectionResetError 発生時に 1 度だけ再試行するメソッド。"""
        try:
            return session.get(
                url,
                headers=self.headers,
                timeout=self.timeout,
                allow_redirects=allow_redirects,
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError) as e:
            # 10054(Reset)はサーバーが応答を拒否しているため、Headerを切り替えて1回だけリトライ
            if "10054" in str(e) or "Connection aborted" in str(e):
                headers_copy = self.headers.copy()
                headers_copy["Connection"] = "close"
                return session.get(
                    url,
                    headers=headers_copy,
                    timeout=self.timeout,
                    allow_redirects=allow_redirects,
                )
            raise e

    def _normalize_url(self, url_or_domain: str) -> str:
        """入力された文字列からドメインを抽出し、検証用の http:// URLを生成する。"""
        if not url_or_domain.startswith(("http://", "https://")):
            url_or_domain = f"http://{url_or_domain}"

        parsed = urlparse(url_or_domain)
        domain = parsed.netloc if parsed.netloc else parsed.path
        # ポート番号やスラッシュ以降を削る
        domain = domain.split(":")[0].split("/")[0]

        return f"http://{domain}"

    def _is_ssl_verification_error(self, error: Exception) -> bool:
        """例外が証明書検証エラー（ホスト名不一致・期限切れ等）によるものかを判定する。"""
        if isinstance(error, requests.exceptions.SSLError):
            return True
        err_text = str(error).lower()
        return "certificate" in err_text or "ssl" in err_text

    @measure_time
    def check_ssl_status(self, domain: str) -> tuple[bool | None, bool | None]:
        """ドメインのSSL対応状況をチェックする。

        戻り値:
            (True, True)   -> SSL対応、常時SSL対応
            (False, False) -> SSL非対応（通信はできたがHTTPのみなど）
            (None, None)   -> 接続エラー、ボットブロック、タイムアウトなど（判定不能）
        """
        result, ssl_verification_failed = self._check_ssl_status_once(domain)
        if result != (None, None) or not ssl_verification_failed:
            return result

        normalized_domain = domain.strip()
        if normalized_domain.lower().startswith("www."):
            return result

        www_domain = f"www.{normalized_domain}"
        logger.info(
            f"[{domain}] 証明書のホスト名不一致の可能性があるため、www付きドメインで再試行します: {www_domain}"
        )
        result, _ = self._check_ssl_status_once(www_domain)
        return result

    def _check_ssl_status_once(
        self, domain: str
    ) -> tuple[tuple[bool | None, bool | None], bool]:
        """SSL対応状況を1回分チェックする内部メソッド。"""
        start_url = self._normalize_url(domain)
        session = self._get_session()

        try:
            # 1. http:// でアクセスし、リダイレクトを追跡する
            response = self._safe_get(session, start_url, allow_redirects=True)

            final_url = response.url
            parsed_final = urlparse(final_url)

            # 最終的なURLが https:// であれば「常時SSL対応」
            if parsed_final.scheme == "https":
                return (True, True), False

            # httpsにリダイレクトされなかったが、個別で https:// 接続を試みる
            try:
                https_url = start_url.replace("http://", "https://")
                https_response = self._safe_get(
                    session, https_url, allow_redirects=False
                )
                if https_response.status_code < 400:
                    return (True, False), False
            except Exception:
                pass

            # 通信はできたがHTTPS化されていない場合
            return (False, False), False

        except requests.exceptions.RequestException as e:
            logger.warning(
                f"[{domain}] http://での接続に失敗したため、https://への直接接続を試みます: {e}"
            )
            ssl_error_seen = self._is_ssl_verification_error(e)

            try:
                https_url = start_url.replace("http://", "https://")
                https_response = self._safe_get(
                    session, https_url, allow_redirects=True
                )

                final_url = https_response.url
                parsed_final = urlparse(final_url)

                if parsed_final.scheme == "https":
                    return (True, True), False

                return (False, False), False

            except requests.exceptions.RequestException as https_e:
                logger.warning(
                    f"[{domain}] https://への接続にも失敗したため判定不能: {https_e}"
                )
                ssl_error_seen = ssl_error_seen or self._is_ssl_verification_error(
                    https_e
                )
                return (None, None), ssl_error_seen

        except Exception as e:
            logger.exception(f"[{domain}] SSLチェック中に予期せぬエラー: {e}")
            return (None, None), self._is_ssl_verification_error(e)
        finally:
            session.close()