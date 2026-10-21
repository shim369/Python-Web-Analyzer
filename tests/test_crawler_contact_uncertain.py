import httpx
import pytest

from web_analyzer.core import crawler as crawler_module
from web_analyzer.core.crawler import WebCrawler

TOP = '<html><body><header><nav><a href="/">ホーム</a><a href="/company/">会社概要</a><a href="/contact/">お問い合わせ</a></nav></header><p>本文</p></body></html>'
COMPANY = "<html><body><p>会社概要</p></body></html>"


def _crawl(monkeypatch: pytest.MonkeyPatch, pages: dict[str, str]) -> tuple[str, bool]:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url).rstrip("/")
        if url in pages:
            return httpx.Response(200, text=pages[url], headers={"content-type": "text/html; charset=utf-8"})
        return httpx.Response(404)

    real_client = httpx.Client
    monkeypatch.setattr(crawler_module.httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), follow_redirects=True))
    crawler = WebCrawler(timeout=20.0)
    result = crawler.crawl_and_analyze("https://a.example/")
    return result[2], crawler.contact_form_uncertain


def _site(contact_html: str, top: str = TOP) -> dict[str, str]:
    return {"https://a.example": top, "https://a.example/company": COMPANY, "https://a.example/contact": contact_html}


@pytest.mark.parametrize(
    ("contact_html", "expected_uncertain"),
    [
        # お問い合わせページが電話番号の案内だけ: フォームなしと言い切れる
        ("<html><body><p>お電話でお問い合わせください。TEL 03-1234-5678</p></body></html>", False),
        # 入力欄があるのに項目名を取れない(項目名を画像で表示している等)
        (
            '<html><body><form><img src="name.png"><input type="text" name="a"><img src="mail.png"><input type="text" name="b"><img src="body.png"><textarea name="c"></textarea></form></body></html>',
            True,
        ),
        # Googleフォームへのリンク(リンクテキスト無しの画像リンク)
        ('<html><body><p>下記フォームからどうぞ</p><a href="https://docs.google.com/forms/d/e/xxx/viewform"><img src="btn.png"></a></body></html>', True),
    ],
)
def test_no_contact_form_is_flagged_only_when_form_may_be_missed(monkeypatch: pytest.MonkeyPatch, contact_html: str, expected_uncertain: bool) -> None:
    contact_fields, uncertain = _crawl(monkeypatch, _site(contact_html))
    assert contact_fields == WebCrawler.NO_CONTACT_FORM_TEXT
    assert uncertain is expected_uncertain


def test_contact_form_found_is_not_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    contact_html = """<html><body><form><table>
        <tr><th>お名前</th><td><input type="text" name="n"></td></tr>
        <tr><th>メールアドレス</th><td><input type="email" name="m"></td></tr>
        <tr><th>お問い合わせ内容</th><td><textarea name="b"></textarea></td></tr>
        </table></form></body></html>"""
    contact_fields, uncertain = _crawl(monkeypatch, _site(contact_html))
    assert contact_fields == "お名前\nメールアドレス\nお問い合わせ内容"
    assert uncertain is False
