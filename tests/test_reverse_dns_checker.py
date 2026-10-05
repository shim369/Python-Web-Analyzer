from typing import Any

import httpx
import pytest
from _pytest.monkeypatch import MonkeyPatch

from web_analyzer.core.reverse_dns_checker import ReverseDnsChecker


def _patch_rdap(monkeypatch: MonkeyPatch, result: dict[str, Any] | Exception | httpx.Response) -> list[str]:
    """httpx.getを差し替え、実際に外部へ通信せずにRDAPの応答を返す。呼ばれたURLの一覧を返す。"""
    calls: list[str] = []

    def fake_get(url: str, **kwargs: Any) -> httpx.Response:
        calls.append(url)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, httpx.Response):
            return result
        return httpx.Response(200, json=result, request=httpx.Request("GET", url))

    monkeypatch.setattr("httpx.get", fake_get)
    return calls


def _patch_dns(monkeypatch: MonkeyPatch, ip: str | None, hostname: str | None) -> list[str]:
    """正引き・逆引きを差し替える。Noneの場合はOSError(解決失敗)を送出する。正引きされたホスト名の一覧を返す。"""
    resolved: list[str] = []

    def fake_gethostbyname(host: str) -> str:
        resolved.append(host)
        if ip is None:
            raise OSError("Name or service not known")
        return ip

    def fake_gethostbyaddr(address: str) -> tuple[str, list[str], list[str]]:
        if hostname is None:
            raise OSError("Unknown host")
        return hostname, [], [address]

    monkeypatch.setattr("socket.gethostbyname", fake_gethostbyname)
    monkeypatch.setattr("socket.gethostbyaddr", fake_gethostbyaddr)
    return resolved


ARIN_STYLE: dict[str, Any] = {
    "name": "EXAMPLE-NET",
    "entities": [
        {"roles": ["abuse"], "vcardArray": ["vcard", [["fn", {}, "text", "Abuse Desk"]]]},
        {"roles": ["registrant"], "vcardArray": ["vcard", [["version", {}, "text", "4.0"], ["fn", {}, "text", "Example Hosting Inc."]]]},
    ],
    "remarks": [{"description": ["Remark should not be used"]}],
}

JPNIC_STYLE: dict[str, Any] = {
    "name": "XSERVER-NET",
    "entities": [{"roles": ["administrative"], "vcardArray": ["vcard", [["fn", {}, "text", "JPNIC"]]]}],
    "remarks": [{"description": ["XServer Cloud Inc.", "Osaka"]}],
}


@pytest.mark.parametrize(
    ("rdap", "expected"),
    [
        (ARIN_STYLE, "Example Hosting Inc."),
        (JPNIC_STYLE, "XServer Cloud Inc."),
        # registrantもremarksも無い場合はネットワーク名で代用する
        ({"name": "SOME-NET", "entities": [], "remarks": [{"description": ["  "]}]}, "SOME-NET"),
        ({}, ""),
    ],
)
def test_ip_organization_extracts_name_by_priority(monkeypatch: MonkeyPatch, rdap: dict[str, Any], expected: str) -> None:
    calls = _patch_rdap(monkeypatch, rdap)

    assert ReverseDnsChecker()._ip_organization("203.0.113.10") == expected
    assert calls == ["https://rdap.org/ip/203.0.113.10"]


@pytest.mark.parametrize(
    "result",
    [
        httpx.ConnectTimeout("timed out"),
        httpx.Response(404, request=httpx.Request("GET", "https://rdap.org/ip/203.0.113.10")),
        httpx.Response(200, text="not json", request=httpx.Request("GET", "https://rdap.org/ip/203.0.113.10")),
    ],
)
def test_ip_organization_returns_empty_on_failure(monkeypatch: MonkeyPatch, result: Exception | httpx.Response) -> None:
    _patch_rdap(monkeypatch, result)

    assert ReverseDnsChecker()._ip_organization("203.0.113.10") == ""


@pytest.mark.parametrize(
    ("url_or_domain", "expected_host"),
    [
        ("https://www.example.co.jp:8443/path/?q=1", "www.example.co.jp"),
        ("example.com", "example.com"),
        ("example.com/contact", "example.com"),
    ],
)
def test_resolve_ip_uses_hostname_only(monkeypatch: MonkeyPatch, url_or_domain: str, expected_host: str) -> None:
    resolved = _patch_dns(monkeypatch, "203.0.113.10", None)

    assert ReverseDnsChecker()._resolve_ip(url_or_domain) == "203.0.113.10"
    assert resolved == [expected_host]


@pytest.mark.parametrize(
    ("hostname", "rdap", "expected"),
    [
        ("sv1.example-hosting.jp.", ARIN_STYLE, "sv1.example-hosting.jp（Example Hosting Inc.）"),
        ("sv1.example-hosting.jp", {}, "sv1.example-hosting.jp"),
        (None, JPNIC_STYLE, "XServer Cloud Inc."),
        (None, {}, ""),
    ],
)
def test_get_server_info_combines_available_parts(
    monkeypatch: MonkeyPatch,
    hostname: str | None,
    rdap: dict[str, Any],
    expected: str,
) -> None:
    _patch_dns(monkeypatch, "203.0.113.10", hostname)
    _patch_rdap(monkeypatch, rdap)

    assert ReverseDnsChecker().get_server_info("https://example.com/") == expected


def test_get_server_info_skips_lookups_when_ip_unresolvable(monkeypatch: MonkeyPatch) -> None:
    _patch_dns(monkeypatch, None, "should-not-be-used.example")
    calls = _patch_rdap(monkeypatch, ARIN_STYLE)

    assert ReverseDnsChecker().get_server_info("https://nonexistent.invalid/") == ""
    assert calls == []
