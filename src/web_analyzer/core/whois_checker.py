import logging
import re
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# IANAのルートWHOISサーバー。ここに問い合わせると、TLDごとの権威WHOISサーバーの
# 場所(refer:行)を教えてもらえる。個別TLDのサーバーをハードコードし続けると
# メンテナンスが大変なため、まずここへ問い合わせて動的に解決する。
_IANA_WHOIS_HOST = "whois.iana.org"

# JPRS(.jp)はIANAのrefer先(whois.jprs.jp)に問い合わせても、ドメイン名だけでは
# 日本語形式の応答しか返ってこない。末尾に "/e" を付けると英語形式(組織名などが
# ラテン文字のみになる)で返ってくるため、ドメイン名の組み立て時に付与する。
_JPRS_WHOIS_HOST = "whois.jprs.jp"

# ネームサーバーを抜き出すための候補フィールド名。
# WHOISの応答フォーマットはレジストリによってまちまちなため、複数パターンを
# 上から順に試し、最初にヒットしたものを採用する。
_NAME_SERVER_FIELD_PATTERNS = [
    r"Name Server:\s*(\S+)",
    r"nserver:\s*(\S+)",
    r"\[Name Server\]\s*(\S+)",
]

# 日本語ccTLDのうち、"co.jp"のように第2レベルまでを合わせて1つの単位とみなす
# べき代表的なもの。これらは末尾2ラベルだけでは会社名部分が判別できない
# (例: "xserver.co.jp"の"co.jp"だけでは意味がない)ため、末尾3ラベルを採用する。
_JP_SECOND_LEVEL_SUFFIXES = {"co.jp", "ne.jp", "or.jp", "ac.jp", "go.jp", "ad.jp", "ed.jp", "gr.jp", "lg.jp"}


def _registrable_domain(host: str) -> str:
    """ネームサーバーのホスト名から、サービス提供元を表す代表的なドメイン部分を抜き出す。

    例: "sv14061.xserver.jp" -> "xserver.jp"、"ns02.rakuten.co.jp" -> "rakuten.co.jp"
    """
    labels = host.rstrip(".").lower().split(".")
    if len(labels) <= 2:
        return host.rstrip(".")

    last_two = ".".join(labels[-2:])
    if last_two in _JP_SECOND_LEVEL_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return last_two


class WhoisChecker:
    """ドメインのWHOIS情報(ネームサーバー)から、利用しているサーバー会社を調べるクラス。

    外部ライブラリを追加せず、WHOISプロトコル(TCP/43番ポート)へ生ソケットで
    問い合わせる方式にしている。IANAのルートWHOISサーバーへまず問い合わせて
    権威WHOISサーバーを動的に解決するため、TLDごとの個別対応をハードコードする
    必要がない。
    """

    def __init__(self, timeout: float = 5.0) -> None:
        self.timeout = timeout

    def _query(self, host: str, query: str) -> str:
        """WHOISサーバーへ1回問い合わせ、応答本文を返す。失敗時は空文字。"""
        try:
            with socket.create_connection((host, 43), timeout=self.timeout) as sock:
                sock.sendall((query + "\r\n").encode("utf-8", errors="ignore"))
                chunks: list[bytes] = []
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
                return b"".join(chunks).decode("utf-8", errors="replace")
        except OSError as e:
            logger.warning(f"WHOISサーバー({host})への問い合わせに失敗しました: {e}")
            return ""

    def _find_referral_server(self, iana_response: str, domain: str) -> str:
        """IANAの応答から、TLDを管轄する権威WHOISサーバーのホスト名を抜き出す。"""
        match = re.search(r"refer:\s*(\S+)", iana_response, re.IGNORECASE)
        if match:
            return match.group(1)

        # ".jp"はIANAのrefer先がJPRSであることが分かっているため、応答が
        # 得られなかった場合のフォールバックとして直接指定する。
        if domain.lower().endswith(".jp"):
            return _JPRS_WHOIS_HOST
        return ""

    def _find_thin_registry_referral(self, response: str) -> str:
        """.com/.net等の"thin"レジストリ応答から、レジストラ側WHOISサーバーを抜き出す。

        VeriSign等が管理するthinレジストリは、ドメインの登録者情報を持たず、
        「どのレジストラに登録されているか」だけを返す。実際の登録者情報を得るには
        レジストラ自身のWHOISサーバーへ再度問い合わせる必要がある。
        """
        match = re.search(r"Registrar WHOIS Server:\s*(\S+)", response, re.IGNORECASE)
        return match.group(1) if match else ""

    def _extract_name_servers(self, response: str) -> list[str]:
        """WHOIS応答テキストからネームサーバーのホスト名を全て抜き出す。"""
        servers: list[str] = []
        for pattern in _NAME_SERVER_FIELD_PATTERNS:
            for match in re.finditer(pattern, response, re.IGNORECASE):
                value = match.group(1).strip().rstrip(".")
                if value and value not in servers:
                    servers.append(value)
        return servers

    def get_hosting_provider(self, url_or_domain: str) -> str:
        """指定URL(またはドメイン)のネームサーバーから、利用しているサーバー会社(の代表ドメイン)を調べる。

        レジストラ自身の登録者情報(WHOIS Privacy等)ではなく、実際にDNSを
        引いているネームサーバーのドメイン部分(例: "xserver.jp", "sakura.ne.jp")を
        返すことで、「どの会社のサーバーで運用されているか」の手がかりとする。
        特定できない場合は空文字を返す(呼び出し側で「取得できませんでした」等に読み替える)。
        """
        parsed = urlparse(url_or_domain if "://" in url_or_domain else f"http://{url_or_domain}")
        domain = (parsed.netloc or parsed.path).split(":")[0].split("/")[0]
        domain = domain.removeprefix("www.")
        if not domain:
            return ""

        try:
            iana_response = self._query(_IANA_WHOIS_HOST, domain)
            referral_host = self._find_referral_server(iana_response, domain)
            if not referral_host:
                logger.info(f"[{domain}] WHOISの権威サーバーを特定できませんでした。")
                return ""

            # JPRSは英語形式で返してもらうため "/e" を付与する。
            query = f"{domain}/e" if referral_host == _JPRS_WHOIS_HOST else domain
            response = self._query(referral_host, query)

            # ネームサーバーはレジストリ(thin)レベルの応答にも含まれるのが通常だが、
            # 万一空だった場合はレジストラ側WHOISサーバーへの再問い合わせも試す。
            name_servers = self._extract_name_servers(response)
            if not name_servers:
                thin_referral = self._find_thin_registry_referral(response)
                if thin_referral:
                    registrar_response = self._query(thin_referral, domain)
                    name_servers = self._extract_name_servers(registrar_response)

            if not name_servers:
                return ""

            return _registrable_domain(name_servers[0])

        except Exception as e:
            logger.warning(f"[{domain}] WHOIS情報の取得中に予期せぬエラーが発生しました: {e}")
            return ""
