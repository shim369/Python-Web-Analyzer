from urllib.parse import urlparse

import httpx


class SslChecker:
    """Webサイトの常時SSL（HTTPS化）状態を判定するクラス。"""

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout
        self.headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) python-web-analyzer"}

    def _normalize_url(self, url_or_domain: str) -> str:
        """入力された文字列からドメインを抽出し、検証用のhttp:// URLを生成する。"""
        # スキーマがない場合にurlparseが正しく挙動するよう暫定処理
        if not url_or_domain.startswith(("http://", "https://")):
            url_or_domain = f"http://{url_or_domain}"

        parsed = urlparse(url_or_domain)
        domain = parsed.netloc if parsed.netloc else parsed.path
        # ポート番号やパスが含まれる場合はドメイン名のみを抽出
        domain = domain.split(":")[0].split("/")[0]

        return f"http://{domain}"

    def is_always_ssl(self, target: str) -> bool:
        """対象サイトが常時SSLに対応しているか判定する。

        Args:
            target: 検証対象のURLまたはドメイン名

        Returns:
            常時SSLに対応している場合はTrue、そうでない場合はFalse
        """
        test_url = self._normalize_url(target)

        try:
            # クライアントセッションを作成して通信（リダイレクトを追跡）
            with httpx.Client(headers=self.headers, timeout=self.timeout, follow_redirects=True) as client:
                response = client.get(test_url)

                final_url = str(response.url)
                is_https = final_url.startswith("https://")
                has_redirects = len(response.history) > 0

                # 最終URLがhttpsであり、かつhttpからリダイレクトされた履歴がある場合のみTrue
                return is_https and has_redirects

        except httpx.ConnectError:
            print(f"[Error] 接続できませんでした。ドメインが存在しないか、サーバーがダウンしています: {target}")
            return False
        except httpx.TimeoutException:
            print(f"[Error] タイムアウトしました。応答がありません: {target}")
            return False
        except httpx.HTTPStatusError as e:
            print(f"[Error] HTTPエラーが発生しました ({e.response.status_code}): {target}")
            return False
        except httpx.RequestError as e:
            print(f"[Error] 通信中に予期せぬエラーが発生しました: {e}")
            return False
