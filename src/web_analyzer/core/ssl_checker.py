import logging
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)


class SslChecker:
    """WebサイトのSSL状態（SSLあり・常時SSL）を判定するクラス。"""

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout
        # Google等にブロックされにくいよう、一般的なブラウザのUser-Agentを設定
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

    def _normalize_url(self, url_or_domain: str) -> str:
        """入力された文字列からドメインを抽出し、検証用の http:// URLを生成する。"""
        if not url_or_domain.startswith(("http://", "https://")):
            url_or_domain = f"http://{url_or_domain}"

        parsed = urlparse(url_or_domain)
        domain = parsed.netloc if parsed.netloc else parsed.path
        # ポート番号やスラッシュ以降を削る
        domain = domain.split(":")[0].split("/")[0]

        return f"http://{domain}"

    def check_ssl_status(self, domain: str) -> tuple[bool | None, bool | None]:
        """ドメインのSSL対応状況をチェックする。

        戻り値:
            (True, True)   -> SSL対応、常時SSL対応
            (False, False) -> SSL非対応（通信はできたがHTTPのみなど）
            (None, None)   -> 接続エラー、ボットブロック、タイムアウトなど（判定不能）
        """
        start_url = self._normalize_url(domain)

        try:
            # 1. http:// でアクセスし、リダイレクトを追跡する
            # (User-Agentヘッダーを付与してセキュリティブロックを緩和)
            response = requests.get(start_url, headers=self.headers, timeout=self.timeout, allow_redirects=True)

            final_url = response.url
            parsed_final = urlparse(final_url)

            # 最終的なURLが https:// であれば「常時SSL対応」
            if parsed_final.scheme == "https":
                return True, True

            # httpsにリダイレクトされなかったが、個別で https:// 接続を試みる
            try:
                https_url = start_url.replace("http://", "https://")
                https_response = requests.get(https_url, headers=self.headers, timeout=self.timeout, allow_redirects=False)
                if https_response.status_code < 400:
                    # HTTPSでの接続はできるが、常時リダイレクトはされていない場合
                    return True, False
            except Exception:
                # HTTPSでの接続に失敗した場合
                pass

            # 通信はできたがHTTPS化されていない場合
            return False, False

        except requests.exceptions.RequestException as e:
            # タイムアウト、DNSエラー、Google等の403/401ブロックなどの接続エラー時
            logger.warning(f"[{domain}] 接続エラーまたはボット拒否のため判定不能: {e}")
            return None, None
        except Exception as e:
            logger.exception(f"[{domain}] SSLチェック中に予期せぬエラー: {e}")
            return None, None
