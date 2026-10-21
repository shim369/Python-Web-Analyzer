from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.worksheet.worksheet import Worksheet

from web_analyzer.core.description_enricher import DescriptionEnricher, EnrichmentTarget, PageRecount, TargetCounts
from web_analyzer.core.evaluator import RenewalEvaluator
from web_analyzer.core.site_summarizer import SiteSummarizer

LONG_DESCRIPTION = "山田工業株式会社は、愛知県で自動車部品の精密切削加工を手がける会社です。試作から量産まで一貫して対応しています。"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", True),
        ("山田工業株式会社", True),
        ("山田工業株式会社 公式ホームページ", True),
        ("山田工業株式会社｜精密金属加工｜自動車部品の切削加工｜愛知県名古屋市｜試作・量産対応", True),
        ("Yamada Industry - Precision Machining - Automotive Parts - Nagoya Aichi Japan", True),
        (LONG_DESCRIPTION, False),
        # 区切り記号を含んでいても、句点のある文章になっていれば対象外
        ("山田工業株式会社｜愛知県で自動車部品の精密切削加工を手がけ、試作から量産まで一貫して対応しています。", False),
    ],
)
def test_needs_summary(text: str, expected: bool) -> None:
    assert DescriptionEnricher.needs_summary(text) is expected


def _write_result_excel(path: Path, rows: list[tuple[str, str]] | list[tuple[str, str, str]]) -> None:
    wb = Workbook()
    ws = wb.active
    assert isinstance(ws, Worksheet)
    headers = ["日付", "サイト名", "調査結果", "SSLあり", "SSL常時", "階層数", "svcmd", "構成", "ページ数", "使用CMS", "用途", "問合せ項目", "不可の理由", "備考", "担当"]
    ws.append(headers)
    for domain, purpose, *rejection in rows:
        ws.append(["10/15", domain, "◯", "", "", 2, "", "", 5, "", purpose, "", rejection[0] if rejection else "", "", "担当者"])
    ws.column_dimensions["K"].width = 60
    wb.save(str(path))


class _StubEnricher(DescriptionEnricher):
    """サイト取得・Gemini呼び出しを行わず、決め打ちの要約を返す。"""

    def __init__(self, summaries: dict[str, str]) -> None:
        super().__init__(summarizer=SiteSummarizer(api_key="dummy"))
        self.summaries = summaries
        self.called: list[str] = []

    def summarize_domain(self, domain: str, current_description: str = "") -> str:
        self.called.append(domain)
        return self.summaries.get(domain, "")


def test_enrich_file_rewrites_only_weak_rows(tmp_path: Path) -> None:
    input_path = tmp_path / "result.xlsx"
    output_path = tmp_path / "out.xlsx"
    _write_result_excel(
        input_path,
        [
            ("a.example", "A株式会社"),
            ("b.example", LONG_DESCRIPTION),
            ("c.example", ""),
        ],
    )
    enricher = _StubEnricher({"a.example": "A株式会社のコーポレートサイト。業務用厨房機器の製造・販売を手がける。"})
    progress: list[tuple[int, int]] = []

    result = enricher.enrich_file(input_path, output_path, progress_callback=lambda d, t: progress.append((d, t)))

    assert sorted(enricher.called) == ["a.example", "c.example"]
    assert result.total_rows == 3
    assert result.target_rows == 2
    assert result.summarized == 1
    assert result.failed_domains == ["c.example"]
    assert progress[-1] == (2, 2)

    ws = load_workbook(str(output_path)).active
    assert isinstance(ws, Worksheet)
    assert ws["K2"].value == "A株式会社のコーポレートサイト。業務用厨房機器の製造・販売を手がける。"
    assert ws["K3"].value == LONG_DESCRIPTION
    # 要約できなかった行は元の値(空欄)のまま
    assert ws["K4"].value in (None, "")
    # 他の列は書き換えない
    assert ws["O2"].value == "担当者"

    # 要約した行は黄色、失敗した行はピンク、対象外の行は塗らない
    assert ws["K2"].fill.fgColor.rgb.endswith(DescriptionEnricher.SUMMARIZED_FILL_COLOR)
    assert ws["K4"].fill.fgColor.rgb.endswith(DescriptionEnricher.FAILED_FILL_COLOR)
    assert ws["K3"].fill.fill_type is None


def test_enrich_file_force_all(tmp_path: Path) -> None:
    input_path = tmp_path / "result.xlsx"
    _write_result_excel(input_path, [("a.example", "A株式会社"), ("b.example", LONG_DESCRIPTION)])
    enricher = _StubEnricher({})

    result = enricher.enrich_file(input_path, tmp_path / "out.xlsx", force_all=True)

    assert sorted(enricher.called) == ["a.example", "b.example"]
    assert result.target_rows == 2


def test_find_profile_links_prefers_link_text() -> None:
    html = """
    <html><body>
      <a href="/">ホーム</a>
      <a href="/news/">お知らせ</a>
      <a href="/gaiyou.html">アクセス</a>
      <a href="/pdf/company.pdf">会社案内PDF</a>
      <a href="/corporate/">企業情報</a>
      <a href="/aboutus/">私たちについて</a>
    </body></html>
    """
    enricher = DescriptionEnricher(summarizer=SiteSummarizer(api_key=""))

    links = enricher._find_profile_links(html, "https://www.yamada.example/")

    # URLだけで一致した /gaiyou.html より、リンクテキストで一致したものを優先する
    assert links == ["https://www.yamada.example/corporate/", "https://www.yamada.example/aboutus/"]


def test_find_profile_links_follows_other_domain_only_by_link_text() -> None:
    html = """
    <html><body>
      <a href="https://partner.example/company/">取引先</a>
      <a href="//corp.yamada.example/jp/profile/">企業情報</a>
    </body></html>
    """
    enricher = DescriptionEnricher(summarizer=SiteSummarizer(api_key=""))

    links = enricher._find_profile_links(html, "https://www.yamada-products.example/")

    assert links == ["https://corp.yamada.example/jp/profile/"]


def test_enrich_file_skips_already_renewed_rows(tmp_path: Path) -> None:
    """M列が「すでにリニューアル済のため」の行は、全行を要約し直す場合でもK列を要約しない。"""
    input_path = tmp_path / "result.xlsx"
    output_path = tmp_path / "out.xlsx"
    _write_result_excel(
        input_path,
        [
            ("a.example", "A株式会社", ""),
            ("moved.example", "", "すでにリニューアル済のため"),
            ("b.example", "", "リニューアルのメリットがないため"),
        ],
    )
    enricher = _StubEnricher({})

    assert enricher.count_targets(input_path) == TargetCounts(total_rows=3, eligible_rows=2, purpose_rows=2, structure_rows=2, contact_rows=2)
    result = enricher.enrich_file(input_path, output_path, force_all=True)

    assert sorted(enricher.called) == ["a.example", "b.example"]
    assert result.total_rows == 3
    assert result.target_rows == 2
    ws = load_workbook(str(output_path)).active
    assert isinstance(ws, Worksheet)
    assert ws["K3"].value in (None, "")
    assert ws["K3"].fill.fill_type is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", True),
        ("HOME\n会社案内", True),
        ("HOME\n会社案内\n採用情報", False),
        ("HOME\n会社案内\n03-1234-5678", True),
        ("MESSAGE\nNEWS\nWORK\ninfo@example.co.jp", True),
        ("SDF技術協会↗\n給水システム協会↗\nInstagram↗", True),
        ("サービス\n会社情報\nCopyright © Example Inc.", True),
    ],
)
def test_needs_structure(text: str, expected: bool) -> None:
    assert DescriptionEnricher.needs_structure(text) is expected


NAV_HTML = """
<header><nav class="gnav"><ul>
  <li><a href="/">ホーム</a></li><li><a href="/service/">事業内容</a></li>
  <li><a href="/company/">会社概要</a></li><li><a href="/contact/">お問い合わせ</a></li>
</ul></nav></header>
<div class="links"><ul>
  <li><a href="https://a.example/">日本設備協会</a></li><li><a href="https://b.example/">給水システム協会</a></li>
  <li><a href="https://c.example/">地域商工会</a></li>
</ul></div>
"""


class _StubSummarizer(SiteSummarizer):
    """Geminiを呼ばず、グローバルナビの判定結果を決め打ちで返す。"""

    def __init__(self, nav_index: int | None) -> None:
        super().__init__(api_key="dummy")
        self.nav_index = nav_index
        self.nav_calls: list[list[list[str]]] = []

    def select_global_nav(self, domain: str, candidates: list[list[str]]) -> int | None:
        self.nav_calls.append(candidates)
        return self.nav_index

    def summarize(self, domain: str, top_html: str, profile_htmls: list[str] | None = None, current_description: str = "") -> str:
        return f"{domain}の運営会社のコーポレートサイト。"


class _StructureStubEnricher(DescriptionEnricher):
    def __init__(self, summarizer: SiteSummarizer, html: str) -> None:
        super().__init__(summarizer=summarizer)
        self.html = html
        self.fetch_count = 0

    def fetch_site_pages(self, domain: str) -> tuple[str, list[str]]:
        self.fetch_count += 1
        return self.html, []


def test_select_structure_uses_ai_choice_among_candidates() -> None:
    summarizer = _StubSummarizer(nav_index=1)
    enricher = _StructureStubEnricher(summarizer, NAV_HTML)

    structure = enricher.select_structure("a.example", NAV_HTML)

    # 候補はサイト上の項目名そのまま。AIは番号を選ぶだけ
    assert summarizer.nav_calls[0] == [["ホーム", "事業内容", "会社概要", "お問い合わせ"], ["日本設備協会", "給水システム協会", "地域商工会"]]
    assert structure == "日本設備協会\n給水システム協会\n地域商工会"


def test_select_structure_falls_back_to_rule_result_without_ai() -> None:
    enricher = _StructureStubEnricher(_StubSummarizer(nav_index=None), NAV_HTML)

    assert enricher.select_structure("a.example", NAV_HTML) == "ホーム\n事業内容\n会社概要\nお問い合わせ"


def test_select_structure_asks_ai_even_for_single_candidate() -> None:
    """候補が1つでもAIに判定させ、「該当なし」ならH列を埋めない(SNSリンク集等を入れないため)。"""
    html = '<nav class="gnav"><ul><li><a href="/">TOP</a></li><li><a href="/a/">会社案内</a></li><li><a href="/b/">採用情報</a></li></ul></nav>'
    summarizer = _StubSummarizer(nav_index=SiteSummarizer.NO_GLOBAL_NAV)
    enricher = _StructureStubEnricher(summarizer, html)

    assert enricher.select_structure("a.example", html) == ""
    assert summarizer.nav_calls == [[["TOP", "会社案内", "採用情報"]]]


def test_select_structure_drops_phone_and_sns_items() -> None:
    """電話番号・SNSの項目だけを取り除き、残りのナビ項目は使う。"""
    html = (
        '<nav class="gnav"><ul><li><a href="/">ホーム</a></li><li><a href="/p/">製品情報</a></li>'
        '<li><a href="/c/">企業情報</a></li><li><a href="tel:0612345678">06-1234-5678</a></li></ul></nav>'
        '<div class="sns"><ul><li><a href="https://x.com/a">Twitter</a></li><li><a href="https://fb.com/a">Facebook</a></li>'
        '<li><a href="https://ig.com/a">Instagram</a></li></ul></div>'
    )
    summarizer = _StubSummarizer(nav_index=0)
    enricher = _StructureStubEnricher(summarizer, html)

    assert enricher.select_structure("a.example", html) == "ホーム\n製品情報\n企業情報"
    # SNSだけのリストは候補にもならない
    assert summarizer.nav_calls == [[["ホーム", "製品情報", "企業情報"]]]


def test_select_structure_skips_candidates_over_max_items() -> None:
    """上限を超えるリスト(子メニューまで展開したスマホ用メニュー等)は、途中で切らずに候補から外す。"""
    long_items = "".join(f'<li><a href="/p/{i}/">項目{i}</a></li>' for i in range(DescriptionEnricher.MAX_STRUCTURE_ITEMS + 2))
    html = f"""
    <nav class="gnav"><ul>{long_items}</ul></nav>
    <div id="main_btn"><a href="/"><img alt="HOME"></a><a href="/item/"><img alt="製品情報"></a><a href="/company/"><img alt="会社概要"></a></div>
    """
    summarizer = _StubSummarizer(nav_index=0)
    enricher = _StructureStubEnricher(summarizer, html)

    assert enricher.select_structure("a.example", html) == "HOME\n製品情報\n会社概要"
    assert summarizer.nav_calls == [[["HOME", "製品情報", "会社概要"]]]


def test_select_structure_returns_empty_when_no_menu() -> None:
    enricher = _StructureStubEnricher(_StubSummarizer(nav_index=None), "<p>準備中</p>")

    assert enricher.select_structure("a.example", "<p>準備中</p>") == ""


def test_enrich_file_fills_structure_and_purpose_with_one_fetch(tmp_path: Path) -> None:
    input_path = tmp_path / "result.xlsx"
    output_path = tmp_path / "out.xlsx"
    _write_result_excel(input_path, [("a.example", "A株式会社"), ("b.example", LONG_DESCRIPTION)])
    ws_in = load_workbook(str(input_path))
    sheet = ws_in.active
    assert isinstance(sheet, Worksheet)
    sheet["H3"] = "トップ\n製品情報\n会社概要\n採用情報"  # b.example の構成は正常
    ws_in.save(str(input_path))

    enricher = _StructureStubEnricher(_StubSummarizer(nav_index=None), NAV_HTML)
    result = enricher.enrich_file(input_path, output_path, fill_purpose=True, fill_structure=True)

    assert result.target_rows == 1
    assert result.structure_target_rows == 1
    assert result.structure_filled == 1
    # 用途・構成の両方が対象の a.example でも、サイトの取り直しは1回
    assert enricher.fetch_count == 1

    ws = load_workbook(str(output_path)).active
    assert isinstance(ws, Worksheet)
    assert ws["H2"].value == "ホーム\n事業内容\n会社概要\nお問い合わせ"
    assert ws["H2"].fill.fgColor.rgb.endswith(DescriptionEnricher.SUMMARIZED_FILL_COLOR)
    assert ws["K2"].value == "a.exampleの運営会社のコーポレートサイト。"
    assert ws["H3"].value == "トップ\n製品情報\n会社概要\n採用情報"
    assert ws["H3"].fill.fill_type is None


def test_enrich_file_structure_only_leaves_purpose_untouched(tmp_path: Path) -> None:
    input_path = tmp_path / "result.xlsx"
    _write_result_excel(input_path, [("a.example", "A株式会社")])
    enricher = _StructureStubEnricher(_StubSummarizer(nav_index=None), "<p>準備中</p>")

    result = enricher.enrich_file(input_path, tmp_path / "out.xlsx", fill_purpose=False, fill_structure=True)

    assert result.target_rows == 0
    assert result.structure_failed_domains == ["a.example"]
    ws = load_workbook(str(tmp_path / "out.xlsx")).active
    assert isinstance(ws, Worksheet)
    assert ws["K2"].value == "A株式会社"
    assert ws["K2"].fill.fill_type is None
    assert ws["H2"].fill.fgColor.rgb.endswith(DescriptionEnricher.FAILED_FILL_COLOR)


def test_enrichment_target_defaults_to_purpose_only() -> None:
    target = EnrichmentTarget(row=2, domain="a.example", current_description="")
    assert target.needs_purpose and not target.needs_structure


# ----------------------------------------------------------------------
# ページ数(I列)の再計測
# ----------------------------------------------------------------------

HEADERS = ["日付", "サイト名", "調査結果", "SSLあり", "SSL常時", "階層数", "svcmd", "構成", "ページ数", "使用CMS", "用途", "問合せ項目", "不可の理由", "備考", "担当"]


def _write_pages_excel(path: Path, rows: list[tuple[str, str, int | str, str, str]]) -> None:
    """(サイト名, 調査結果, ページ数, 不可の理由, 備考)の行でExcelを作る。用途・構成は補完対象外の値にしておく。"""
    wb = Workbook()
    ws = wb.active
    assert isinstance(ws, Worksheet)
    ws.append(HEADERS)
    for domain, result, pages, rejection, remarks in rows:
        ws.append(["10/16", domain, result, "", "", 2, "", "TOP\n会社概要\n採用情報", pages, "", LONG_DESCRIPTION, "", rejection, remarks, "担当者"])
    wb.save(str(path))


class _RecountStubEnricher(DescriptionEnricher):
    """巡回せず、ドメインごとに決め打ちの再計測結果を返す。"""

    def __init__(self, recounts: dict[str, PageRecount | None]) -> None:
        super().__init__(summarizer=SiteSummarizer(api_key="dummy"))
        self.recounts = recounts
        self.called: list[str] = []

    def recount_pages(self, domain: str) -> PageRecount | None:
        self.called.append(domain)
        return self.recounts.get(domain)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("54以上", True), ("99以上", True), ("100以上", False), (54, False), ("要確認", False), ("", False), (None, False)],
)
def test_needs_page_recount(value: object, expected: bool) -> None:
    assert DescriptionEnricher.needs_page_recount(value) is expected


def test_recount_targets_only_unconfirmed_page_counts(tmp_path: Path) -> None:
    input_path = tmp_path / "result.xlsx"
    _write_pages_excel(
        input_path,
        [
            ("a.example", "×", "54以上", "ページ数が多いため", ""),
            ("b.example", "×", "100以上", "ページ数が多いため", ""),
            ("c.example", "要確認", 8, "", ""),
            ("moved.example", "×", "3以上", "すでにリニューアル済のため", ""),
        ],
    )
    enricher = _RecountStubEnricher({})

    counts = enricher.count_targets(input_path)
    enricher.enrich_file(input_path, tmp_path / "out.xlsx", fill_purpose=False, recount_pages=True, force_all=True)

    assert counts.pages_rows == 1
    assert enricher.called == ["a.example"]


def test_recount_updates_pending_judgement(tmp_path: Path) -> None:
    """時間切れで判定保留(要確認)だった行は、ページ数が確定したら判定をやり直す。"""
    input_path = tmp_path / "result.xlsx"
    output_path = tmp_path / "out.xlsx"
    _write_pages_excel(input_path, [("a.example", "要確認", "8以上", "", RenewalEvaluator.PENDING_TIMEOUT_REASON)])
    recount = PageRecount(total_pages=23, confirmed=True, evaluation_result="×", rejection_reason="ページ数が多いため", remarks="")
    enricher = _RecountStubEnricher({"a.example": recount})

    result = enricher.enrich_file(input_path, output_path, fill_purpose=False, recount_pages=True)

    assert (result.pages_confirmed, result.judgement_updated, result.pages_unconfirmed_domains) == (1, 1, [])
    ws = load_workbook(str(output_path)).active
    assert isinstance(ws, Worksheet)
    assert ws["I2"].value == 23
    assert ws["I2"].fill.fgColor.rgb.endswith(DescriptionEnricher.SUMMARIZED_FILL_COLOR)
    assert ws["C2"].value == "×"
    assert ws["M2"].value == "ページ数が多いため"
    assert ws["N2"].value in (None, "")


def test_recount_adds_page_reason_to_rejected_row(tmp_path: Path) -> None:
    """既に「×」の行は、ページ数がしきい値を超えたと新たに分かった場合だけ理由を追記する。"""
    input_path = tmp_path / "result.xlsx"
    output_path = tmp_path / "out.xlsx"
    _write_pages_excel(input_path, [("a.example", "×", "8以上", "ログイン・マイページ機能があるため", "")])
    recount = PageRecount(total_pages="100以上", confirmed=True, evaluation_result="×", rejection_reason="", remarks="")
    enricher = _RecountStubEnricher({"a.example": recount})

    enricher.enrich_file(input_path, output_path, fill_purpose=False, recount_pages=True)

    ws = load_workbook(str(output_path)).active
    assert isinstance(ws, Worksheet)
    assert ws["I2"].value == "100以上"
    assert ws["M2"].value == "ページ数が多いため\nログイン・マイページ機能があるため"


def test_recount_keeps_value_when_not_improved(tmp_path: Path) -> None:
    """再計測できなかった・件数が増えなかった場合は元の値のまま(ピンク)。件数が増えた場合は未確定でも更新する。"""
    input_path = tmp_path / "result.xlsx"
    output_path = tmp_path / "out.xlsx"
    _write_pages_excel(
        input_path,
        [
            ("fail.example", "×", "54以上", "ページ数が多いため", ""),
            ("same.example", "×", "54以上", "ページ数が多いため", ""),
            ("more.example", "×", "54以上", "ページ数が多いため", ""),
        ],
    )

    def unconfirmed(total_pages: str) -> PageRecount:
        return PageRecount(total_pages=total_pages, confirmed=False, evaluation_result="×", rejection_reason="ページ数が多いため", remarks="")

    enricher = _RecountStubEnricher(
        {
            "fail.example": None,
            "same.example": unconfirmed("40以上"),
            "more.example": unconfirmed("80以上"),
        }
    )

    result = enricher.enrich_file(input_path, output_path, fill_purpose=False, recount_pages=True)

    assert sorted(result.pages_unconfirmed_domains) == ["fail.example", "more.example", "same.example"]
    assert result.pages_confirmed == 0
    ws = load_workbook(str(output_path)).active
    assert isinstance(ws, Worksheet)
    assert [ws[f"I{r}"].value for r in (2, 3, 4)] == ["54以上", "54以上", "80以上"]
    assert all(ws[f"I{r}"].fill.fgColor.rgb.endswith(DescriptionEnricher.FAILED_FILL_COLOR) for r in (2, 3, 4))


class _RenderStubEnricher(DescriptionEnricher):
    """静的HTMLにはナビが無く、ブラウザで描画するとナビが現れるサイトを模す。"""

    def __init__(self, summarizer: SiteSummarizer, static_html: str, rendered_html: str) -> None:
        super().__init__(summarizer=summarizer)
        self.static_html = static_html
        self.rendered_html = rendered_html
        self.render_count = 0

    def fetch_site_pages(self, domain: str) -> tuple[str, list[str]]:
        return self.static_html, []

    def render_top_page(self, domain: str) -> str:
        self.render_count += 1
        return self.rendered_html


def test_structure_retries_with_rendered_page_when_no_menu_in_static_html() -> None:
    """ヘッダーをJavaScriptで後から読み込むサイトは、描画したトップページで判定し直す。"""
    summarizer = _StubSummarizer(nav_index=0)
    enricher = _RenderStubEnricher(summarizer, "<header></header><main><p>本文</p></main>", NAV_HTML)
    target = EnrichmentTarget(row=2, domain="a.example", current_description="", needs_purpose=False, needs_structure=True)

    assert enricher.enrich_row(target).structure == "ホーム\n事業内容\n会社概要\nお問い合わせ"
    assert enricher.render_count == 1


def test_structure_does_not_render_when_ai_rejected_candidates() -> None:
    """候補があってAIが「該当なし」と判定した場合は、描画し直さない。"""
    summarizer = _StubSummarizer(nav_index=SiteSummarizer.NO_GLOBAL_NAV)
    enricher = _RenderStubEnricher(summarizer, NAV_HTML, NAV_HTML)
    target = EnrichmentTarget(row=2, domain="a.example", current_description="", needs_purpose=False, needs_structure=True)

    assert enricher.enrich_row(target).structure == ""
    assert enricher.render_count == 0


def test_fetch_top_page_follows_same_site_meta_refresh() -> None:
    """トップページが同一サイト内の実際のトップへmeta refreshで移動するだけなら辿る。別ドメインへは辿らない。"""
    import httpx

    from web_analyzer.core.crawler import WebCrawler

    pages = {
        "https://a.example": '<meta http-equiv="refresh" content="0;URL=https://www.a.example/main/">',
        "https://www.a.example/main": NAV_HTML,
        "https://moved.example": '<meta http-equiv="refresh" content="0;URL=https://other.example/">',
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url).rstrip("/")
        return httpx.Response(200, text=pages[url], headers={"content-type": "text/html; charset=utf-8"}) if url in pages else httpx.Response(404)

    enricher = DescriptionEnricher(summarizer=SiteSummarizer(api_key="dummy"))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        html, url = enricher._fetch_top_page(client, WebCrawler(), "a.example")
        assert (url, "事業内容" in html) == ("https://www.a.example/main/", True)
        assert enricher._fetch_top_page(client, WebCrawler(), "moved.example")[1] == "https://moved.example"


class _RateLimitedSummarizer(_StubSummarizer):
    """Geminiの利用上限に達した状態を模す(要約・ナビ判定とも失敗し、上限が原因と記録される)。"""

    def select_global_nav(self, domain: str, candidates: list[list[str]]) -> int | None:
        SiteSummarizer._thread_state.rate_limited = True
        return None

    def summarize(self, domain: str, top_html: str, profile_htmls: list[str] | None = None, current_description: str = "") -> str:
        SiteSummarizer._thread_state.rate_limited = True
        return ""


def test_enrich_file_reports_rows_skipped_by_rate_limit(tmp_path: Path) -> None:
    """利用上限でAIを呼べなかった行は、処理を止めずに件数・ドメインとして報告する。"""
    input_path = tmp_path / "result.xlsx"
    _write_result_excel(input_path, [("a.example", "A株式会社"), ("b.example", "B株式会社")])

    enricher = _StructureStubEnricher(_RateLimitedSummarizer(nav_index=None), NAV_HTML)
    result = enricher.enrich_file(input_path, tmp_path / "out.xlsx", fill_purpose=True, fill_structure=True)

    assert sorted(result.rate_limited_domains) == ["a.example", "b.example"]
    assert result.summarized == 0


def test_enrich_file_does_not_report_rate_limit_when_ai_works(tmp_path: Path) -> None:
    input_path = tmp_path / "result.xlsx"
    _write_result_excel(input_path, [("a.example", "A株式会社")])

    enricher = _StructureStubEnricher(_StubSummarizer(nav_index=0), NAV_HTML)
    result = enricher.enrich_file(input_path, tmp_path / "out.xlsx", fill_purpose=True, fill_structure=True)

    assert result.rate_limited_domains == []


@pytest.mark.parametrize(
    ("value", "pages", "flagged", "expected"),
    [
        # 一時期の調査結果では、フォームが見つからなければ空欄にしていた
        ("", 12, False, True),
        (None, "100以上", False, True),
        # 「問い合わせフォームなし」は、取り損ねの疑い(グレー)・補完の失敗(ピンク)の行だけ対象
        ("問い合わせフォームなし", 12, True, True),
        ("問い合わせフォームなし", 12, False, False),
        ("お名前\nメールアドレス", 12, True, False),
        ("mailto", 12, False, False),
        # 巡回できなかった行は対象外
        ("", None, False, False),
        ("", 0, False, False),
    ],
)
def test_needs_contact(value: object, pages: object, flagged: bool, expected: bool) -> None:
    assert DescriptionEnricher.needs_contact(value, pages, flagged) is expected


class _ContactStubEnricher(DescriptionEnricher):
    """サイトを取得せず、ドメインごとに決め打ちの問合せ項目を返す。"""

    def __init__(self, contacts: dict[str, str]) -> None:
        super().__init__(summarizer=SiteSummarizer(api_key="dummy"))
        self.contacts = contacts
        self.called: list[str] = []

    def find_contact_fields(self, domain: str) -> str:
        self.called.append(domain)
        return self.contacts.get(domain, "")


def test_enrich_file_fills_flagged_contact_fields(tmp_path: Path) -> None:
    from openpyxl.styles import PatternFill

    from web_analyzer.core.excel_service import ExcelService

    input_path = tmp_path / "result.xlsx"
    output_path = tmp_path / "out.xlsx"
    domains = ["a.example", "b.example", "c.example", "d.example", "e.example"]
    _write_result_excel(input_path, [(d, LONG_DESCRIPTION) for d in domains])
    wb = load_workbook(str(input_path))
    sheet = wb.active
    assert isinstance(sheet, Worksheet)
    gray = PatternFill(start_color=ExcelService.UNKNOWN_FILL_COLOR, end_color=ExcelService.UNKNOWN_FILL_COLOR, fill_type="solid")
    sheet["L3"] = "問い合わせフォームなし"
    sheet["L3"].fill = gray
    sheet["L4"] = "お名前\nメールアドレス"
    sheet["L5"] = "問い合わせフォームなし"  # 取り損ねの疑いが無い(色なし)
    sheet["L6"] = "問い合わせフォームなし"
    sheet["L6"].fill = gray
    wb.save(str(input_path))
    enricher = _ContactStubEnricher({"a.example": "お名前\nご用件", "b.example": "問い合わせフォームなし"})

    counts = enricher.count_targets(input_path)
    result = enricher.enrich_file(input_path, output_path, fill_purpose=False, fill_contact=True, force_all=True)

    assert counts.contact_rows == 3
    assert sorted(enricher.called) == ["a.example", "b.example", "e.example"]
    assert (result.contact_target_rows, result.contact_filled, result.contact_failed_domains) == (3, 2, ["e.example"])
    ws = load_workbook(str(output_path)).active
    assert isinstance(ws, Worksheet)
    assert [ws[f"L{r}"].value for r in range(2, 7)] == ["お名前\nご用件", "問い合わせフォームなし", "お名前\nメールアドレス", "問い合わせフォームなし", "問い合わせフォームなし"]
    assert ws["L3"].fill.fgColor.rgb.endswith(DescriptionEnricher.SUMMARIZED_FILL_COLOR)
    assert ws["L6"].fill.fgColor.rgb.endswith(DescriptionEnricher.FAILED_FILL_COLOR)

    # 補完で確認済み(黄色)の行は、出力ファイルにもう一度かけても対象にしない。補完できなかった(ピンク)行は対象にする
    enricher.called.clear()
    enricher.enrich_file(output_path, tmp_path / "out2.xlsx", fill_purpose=False, fill_contact=True)
    assert enricher.called == ["e.example"]


def test_find_contact_links_and_form_links() -> None:
    from bs4 import BeautifulSoup

    enricher = DescriptionEnricher(summarizer=SiteSummarizer(api_key="dummy"))
    top = """
    <a href="/company/">会社概要</a><a href="/inquiry/">INQUIRY</a>
    <a href="https://forms.example.net/abc">お問い合わせ</a><a href="mailto:info@a.example">メール</a>
    """
    assert enricher._find_contact_links(top, "https://a.example/") == ["https://a.example/inquiry/", "https://forms.example.net/abc"]

    contact = BeautifulSoup(
        """
        <p>お電話またはメールフォームからお問い合わせください。</p>
        <a href="/contact/form/">メールフォーム</a><a href="/cgi-bin/mail2.html">技術相談</a><a href="/access/">アクセス</a>
        <iframe src="https://docs.google.com/forms/d/e/xxx/viewform"></iframe><iframe src="https://www.google.com/maps/embed?pb=1"></iframe>
        """,
        "lxml",
    )
    assert enricher._find_form_links(contact, "https://a.example/contact/") == [
        "https://docs.google.com/forms/d/e/xxx/viewform",
        "https://a.example/contact/form/",
        "https://a.example/cgi-bin/mail2.html",
    ]


def test_contact_page_text_marks_form_controls() -> None:
    from bs4 import BeautifulSoup

    enricher = DescriptionEnricher(summarizer=SiteSummarizer(api_key="dummy"))
    soup = BeautifulSoup(
        """
        <header><form><input type="search" name="s"></form></header>
        <main><h1>お問い合わせ</h1>
        <form><input type="hidden" name="token"><div>お名前</div><input type="text" name="n">
        <div>種別</div><select name="k"><option>見積</option></select>
        <label><input type="checkbox" name="agree">同意する</label>
        <div>内容</div><textarea name="b"></textarea><input type="submit" value="送信"></form></main>
        """,
        "lxml",
    )
    assert enricher._contact_page_text(soup) == "お名前 [入力欄] 種別 [選択欄] [選択肢] 同意する 内容 [複数行入力欄]"


class _ContactAISummarizer(SiteSummarizer):
    def __init__(self, answer: list[str] | None) -> None:
        super().__init__(api_key="dummy")
        self.answer = answer
        self.pages: list[str] = []

    def extract_contact_fields(self, domain: str, pages: list[tuple[str, str]]) -> list[str] | None:
        self.pages = [url for url, _ in pages]
        return self.answer


CONTACT_SITE = {
    "https://a.example": '<a href="/contact/">お問い合わせ</a>',
    # お問い合わせページには電話番号の案内と、フォーム本体のページへのリンクだけがある
    "https://a.example/contact": '<p>TEL 03-1234-5678</p><a href="/contact/form.html">メールフォームはこちら</a><a href="mailto:info@a.example">info@a.example</a>',
    "https://a.example/contact/form.html": """
        <form><table>
        <tr><th>お名前</th><td><input type="text" name="n"></td></tr>
        <tr><th>メールアドレス</th><td><input type="email" name="m"></td></tr>
        <tr><th>お問い合わせ内容</th><td><textarea name="b"></textarea></td></tr>
        </table></form>""",
    "https://b.example": '<a href="/contact/">お問い合わせ</a>',
    "https://b.example/contact": '<p>TEL 03-1234-5678</p><a href="mailto:info@b.example">info@b.example</a>',
}


@pytest.fixture
def contact_site(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from web_analyzer.core import description_enricher

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url).rstrip("/")
        if url in CONTACT_SITE:
            return httpx.Response(200, text=CONTACT_SITE[url], headers={"content-type": "text/html; charset=utf-8"})
        return httpx.Response(404)

    real_client = httpx.Client
    monkeypatch.setattr(description_enricher.httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), follow_redirects=True))
    # ブラウザでの描画は行わない
    monkeypatch.setattr("web_analyzer.core.crawler.WebCrawler._fetch_rendered_html", lambda self, url: ("", "", None, ""))


@pytest.mark.usefixtures("contact_site")
def test_find_contact_fields_follows_link_to_form_page() -> None:
    """お問い合わせページの先にあるフォーム本体のページまで辿り、ルールで項目を取る(AIは使わない)。"""
    summarizer = _ContactAISummarizer(answer=None)
    enricher = DescriptionEnricher(summarizer=summarizer)

    assert enricher.find_contact_fields("a.example") == "お名前\nメールアドレス\nお問い合わせ内容"
    assert summarizer.pages == []


@pytest.mark.usefixtures("contact_site")
@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        # AIがフォームは無いと判定した場合は、mailtoリンクの有無で出し分ける
        ([], "mailto"),
        (["お名前", "ご用件"], "お名前\nご用件"),
        # 1項目だけ(同意欄等しか読み取れなかった)・判定できなかった場合は空欄のまま
        (["プライバシーポリシーに同意する"], ""),
        (None, ""),
    ],
)
def test_find_contact_fields_asks_ai_when_rules_find_nothing(answer: list[str] | None, expected: str) -> None:
    summarizer = _ContactAISummarizer(answer=answer)
    enricher = DescriptionEnricher(summarizer=summarizer)

    assert enricher.find_contact_fields("b.example") == expected
    assert summarizer.pages == ["https://b.example/contact/"]


IFRAME_TOP_HTML = """
<body><table><tr><td><iframe src="parts/menu.htm"></iframe></td></tr></table>
<table><tr><td><a href="/x">病院A</a></td><td><a href="/y">病院B</a></td><td><a href="/z">病院C</a></td></tr></table></body>
"""
IFRAME_MENU_HTML = """<body><table><tr>
    <td><a href="https://a.example/" target="_top"><b>トップ(紹介･概要)</b></a></td>
    <td><a href="../tomo/index.htm" target="_top"><b>友の会･基金</b></a></td>
    <td><a href="../koukai/index.htm" target="_top"><b>情報公開</b></a></td>
    <td><a href="../saiyou/index.htm" target="_top"><b>採用情報</b></a></td>
</tr></table></body>"""
IFRAME_MENUS = ["トップ(紹介･概要)", "友の会･基金", "情報公開", "採用情報"]


def test_fetch_iframe_nav_menus(monkeypatch: pytest.MonkeyPatch) -> None:
    """ナビをiframeで読み込むサイトは、iframeのページからナビを取る(調査本体と同じ処理)。"""
    import httpx

    from web_analyzer.core import description_enricher

    pages = {"https://a.example/": IFRAME_TOP_HTML, "https://a.example/parts/menu.htm": IFRAME_MENU_HTML}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        url = url if url in pages else url.rstrip("/") + "/"
        return httpx.Response(200, text=pages[url], headers={"content-type": "text/html; charset=utf-8"}) if url in pages else httpx.Response(404)

    real_client = httpx.Client
    monkeypatch.setattr(description_enricher.httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), follow_redirects=True))
    enricher = DescriptionEnricher(summarizer=SiteSummarizer(api_key="dummy"))

    assert enricher.fetch_iframe_nav_menus("a.example", IFRAME_TOP_HTML) == IFRAME_MENUS
    # ナビらしいiframeが無いページは取り直さない
    assert enricher.fetch_iframe_nav_menus("a.example", NAV_HTML) == []


class _IframeStubEnricher(_StructureStubEnricher):
    def fetch_iframe_nav_menus(self, domain: str, top_html: str) -> list[str]:
        return IFRAME_MENUS


@pytest.mark.parametrize(("nav_index", "expected"), [(0, IFRAME_MENUS), (None, IFRAME_MENUS)])
def test_structure_prefers_iframe_nav(nav_index: int | None, expected: list[str]) -> None:
    """iframeのナビを先頭の候補(ルールで取れた結果)にする。AIを呼べなかった場合もそれを使う。"""
    summarizer = _StubSummarizer(nav_index=nav_index)
    enricher = _IframeStubEnricher(summarizer, IFRAME_TOP_HTML)
    target = EnrichmentTarget(row=2, domain="a.example", current_description="", needs_purpose=False, needs_structure=True)

    assert enricher.enrich_row(target).structure == "\n".join(expected)
    assert summarizer.nav_calls[0][0] == IFRAME_MENUS
