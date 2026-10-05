import logging
import socket
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# IPアドレスの管理組織を問い合わせるRDAP(IPアドレス版のWHOIS)の入口。
# rdap.orgはIPアドレスを管轄するレジストリ(JPNIC/APNIC/ARIN等)の
# RDAPサーバーへリダイレクトしてくれるため、地域ごとの個別対応が不要になる。
_RDAP_IP_URL = "https://rdap.org/ip/{ip}"


class ReverseDnsChecker:
    """サイトが実際に設置されているサーバーを、IPアドレスから調べるクラス。

    ネームサーバー(DNSの管理先)は必ずしもWebサーバーの運用会社と一致しない
    (例: DNSだけCloudflareやお名前.comで管理し、実体は別のレンタルサーバー)ため、
    A/AAAAレコードのIPアドレスを起点に以下の2つを調べる。
    - PTRで逆引きしたホスト名(例: "sv14061.xserver.jp")
    - RDAPで調べたIPアドレスの管理組織名(例: "XServer Cloud Inc.")
    """

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout

    def _resolve_ip(self, url_or_domain: str) -> str:
        """指定URL(またはドメイン)を正引きしたIPアドレスを返す。失敗時は空文字。"""
        parsed = urlparse(url_or_domain if "://" in url_or_domain else f"http://{url_or_domain}")
        domain = parsed.hostname or ""
        if not domain:
            return ""
        try:
            return socket.gethostbyname(domain)
        except OSError as e:
            logger.info(f"[{domain}] IPアドレスを取得できませんでした: {e}")
            return ""

    def _reverse_hostname(self, ip_address: str) -> str:
        """IPアドレスを逆引きしたホスト名を返す。PTR未登録等で取得できない場合は空文字。"""
        try:
            hostname, _, _ = socket.gethostbyaddr(ip_address)
            return hostname.rstrip(".")
        except OSError as e:
            logger.info(f"[{ip_address}] 逆引きホスト名を取得できませんでした: {e}")
            return ""

    def _ip_organization(self, ip_address: str) -> str:
        """RDAPでIPアドレスの管理組織名を調べる。取得できない場合は空文字。"""
        try:
            response = httpx.get(_RDAP_IP_URL.format(ip=ip_address), timeout=self.timeout, follow_redirects=True)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as e:
            logger.info(f"[{ip_address}] RDAPでの管理組織の取得に失敗しました: {e}")
            return ""

        # ARIN等は割当先組織を"registrant"ロールのエンティティとして返す。
        for entity in data.get("entities", []):
            if "registrant" not in entity.get("roles", []):
                continue
            vcard = entity.get("vcardArray", [None, []])[1]
            for field in vcard:
                if field[0] == "fn" and field[3]:
                    return str(field[3])

        # JPNIC配下のアドレスはエンティティがJPNIC自身になっており、
        # 割当先の組織名(運営組織)はremarksの先頭に入っている。
        for remark in data.get("remarks", []):
            descriptions = remark.get("description", [])
            if descriptions and descriptions[0].strip():
                return str(descriptions[0]).strip()

        # どちらも無い場合はネットワーク名(例: "XSERVER")で代用する。
        return str(data.get("name") or "")

    def get_server_info(self, url_or_domain: str) -> str:
        """「逆引きホスト名（管理組織名）」形式の文字列を返す。

        片方しか取れない場合は取れた方だけを返し、どちらも取れない場合は空文字を返す
        (呼び出し側で「取得できませんでした」等に読み替える)。
        """
        ip_address = self._resolve_ip(url_or_domain)
        if not ip_address:
            return ""

        hostname = self._reverse_hostname(ip_address)
        organization = self._ip_organization(ip_address)

        if hostname and organization:
            return f"{hostname}（{organization}）"
        return hostname or organization
