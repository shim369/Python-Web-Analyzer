import logging
import re
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag
from openpyxl import load_workbook
from openpyxl.styles import PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from web_analyzer.core.crawler import WebCrawler
from web_analyzer.core.excel_service import _estimate_wrapped_line_count
from web_analyzer.core.site_summarizer import SiteSummarizer

logger = logging.getLogger(__name__)

HTML_PARSER = "lxml"


@dataclass
class EnrichmentTarget:
    row: int
    domain: str
    current_description: str


@dataclass
class EnrichmentResult:
    total_rows: int = 0
    target_rows: int = 0
    summarized: int = 0
    failed_domains: list[str] = field(default_factory=list)
    output_path: Path | None = None


@dataclass
class EnrichmentJob:
    id: str
    status: str = "processing"  # processing, completed, failed
    total: int = 0
    done: int = 0
    result: EnrichmentResult | None = None
    error: str = ""


class DescriptionEnricher:
    """完成した調査結果Excelに対して、K列「用途」が社名だけ等で内容の分かりにくい行を
    Geminiの要約(サイト概要)で書き換える「K列補完」処理。

    スクレイピング本体とは切り離して実行するため、
      - 調査処理の速度やGemini側の制限(無料枠のレート上限等)の影響を受けない
      - 過去に出力したExcelにも後からかけられる
      - 1日の上限で途中までしか要約できなかった場合も、同じファイルに再度かければ
        残りの行だけが対象になる(要約済みの行は判定条件に引っかからないため)
    という運用ができる。

    Excelにはページの中身が残っていないため、対象行のサイトだけトップページと
    会社概要・事業内容らしいページ(最大2ページ)を取り直して要約に使う。
    """

    # これ未満の文字数は「社名・サイト名だけ」とみなして要約対象にする
    MIN_DESCRIPTION_CHARS = 40

    # 句点が無く、これらの区切り記号を含むものは「山田工業株式会社｜精密金属加工｜愛知県」の
    # ような名前・キーワードの羅列とみなして要約対象にする
    NAME_LIST_SEPARATOR_RE = re.compile(r"[|｜／/]|\s[-–—]\s|[-–—]{2,}")

    # 会社概要・事業内容が書かれていそうなページへのリンクの目印(リンクテキスト/URL)
    PROFILE_LINK_TEXT_RE = re.compile(r"会社概要|企業情報|会社案内|会社情報|事業内容|事業紹介|業務内容|私たちについて|当社について|about", re.I)
    PROFILE_LINK_HREF_RE = re.compile(r"about|company|corporate|profile|gaiyou|gaiyo|outline|business|jigyou|jigyo", re.I)
    MAX_PROFILE_PAGES = 2

    DOMAIN_HEADER = "サイト名"
    PURPOSE_HEADER = "用途"
    DEFAULT_DOMAIN_COLUMN = 2  # B列
    DEFAULT_PURPOSE_COLUMN = 11  # K列

    # サイトの取り直しは並列で行い、Geminiへの送信間隔(SiteSummarizer側で制御)の
    # 待ち時間とページ取得の時間を重ねる。Playwrightでのレンダリングも走りうるため
    # 調査本体と同じく5並列程度に抑える。
    MAX_WORKERS = 5
    PAGE_TIMEOUT = 10.0

    # 補完の結果をK列のセル背景で見分けられるようにする。
    # 失敗した行は、出力ファイルに再度かけて成功すれば黄色に塗り替わる。
    SUMMARIZED_FILL_COLOR = "FFF2CC"  # 淡い黄色: 要約で書き換えた
    FAILED_FILL_COLOR = "FFE5EC"  # 淡いピンク: 要約できず元の値のまま

    def __init__(self, summarizer: SiteSummarizer | None = None) -> None:
        self.summarizer = summarizer or SiteSummarizer()
        self._jobs: dict[str, EnrichmentJob] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 判定
    # ------------------------------------------------------------------

    @classmethod
    def needs_summary(cls, text: str) -> bool:
        """K列の文字列が、要約し直すべき「内容の分かりにくい」ものかどうか。"""
        stripped = (text or "").strip()
        if len(stripped) < cls.MIN_DESCRIPTION_CHARS:
            return True
        return "。" not in stripped and bool(cls.NAME_LIST_SEPARATOR_RE.search(stripped))

    def find_targets(self, ws: Worksheet, force_all: bool = False) -> tuple[int, list[EnrichmentTarget]]:
        """(データ行数, 要約対象行のリスト)を返す。"""
        domain_col, purpose_col = self._locate_columns(ws)
        total_rows = 0
        targets: list[EnrichmentTarget] = []

        for row in range(2, ws.max_row + 1):
            domain_value = ws.cell(row=row, column=domain_col).value
            domain = str(domain_value).strip() if domain_value is not None else ""
            if not domain:
                continue
            total_rows += 1

            purpose_value = ws.cell(row=row, column=purpose_col).value
            current = str(purpose_value).strip() if purpose_value is not None else ""
            if force_all or self.needs_summary(current):
                targets.append(EnrichmentTarget(row=row, domain=domain, current_description=current))

        return total_rows, targets

    def _locate_columns(self, ws: Worksheet) -> tuple[int, int]:
        """ヘッダー行から「サイト名」「用途」の列を探す。見つからなければB列/K列とみなす。"""
        domain_col = self.DEFAULT_DOMAIN_COLUMN
        purpose_col = self.DEFAULT_PURPOSE_COLUMN
        for cell in ws[1]:
            header = str(cell.value).strip() if cell.value is not None else ""
            if header == self.DOMAIN_HEADER and cell.column is not None:
                domain_col = cell.column
            elif header == self.PURPOSE_HEADER and cell.column is not None:
                purpose_col = cell.column
        return domain_col, purpose_col

    # ------------------------------------------------------------------
    # Excel一括処理
    # ------------------------------------------------------------------

    def enrich_file(
        self,
        input_path: Path,
        output_path: Path,
        force_all: bool = False,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> EnrichmentResult:
        """input_pathのExcelのK列を補完し、output_pathに保存する。

        元のExcelの書式(列幅・罫線・フォント等)はそのまま残し、書き換えた
        セルの値と、その行の高さだけを更新する。
        """
        wb = load_workbook(str(input_path))
        ws = wb.active
        assert isinstance(ws, Worksheet)

        total_rows, targets = self.find_targets(ws, force_all=force_all)
        _, purpose_col = self._locate_columns(ws)
        result = EnrichmentResult(total_rows=total_rows, target_rows=len(targets))
        logger.info(f"K列補完を開始します: 対象 {len(targets)}件 / 全 {total_rows}件")

        if progress_callback:
            progress_callback(0, len(targets))

        done = 0
        with ThreadPoolExecutor(max_workers=self.MAX_WORKERS, thread_name_prefix="Enricher") as executor:
            future_to_target = {executor.submit(self.summarize_domain, t.domain, t.current_description): t for t in targets}
            for future in as_completed(future_to_target):
                target = future_to_target[future]
                try:
                    summary = future.result()
                except Exception as e:
                    logger.warning(f"[{target.domain}] K列補完中に予期せぬエラーが発生しました: {e}")
                    summary = ""

                cell = ws.cell(row=target.row, column=purpose_col)
                if summary:
                    cell.value = summary
                    cell.fill = PatternFill(start_color=self.SUMMARIZED_FILL_COLOR, end_color=self.SUMMARIZED_FILL_COLOR, fill_type="solid")
                    self._fit_row_height(ws, target.row, purpose_col, summary)
                    result.summarized += 1
                else:
                    cell.fill = PatternFill(start_color=self.FAILED_FILL_COLOR, end_color=self.FAILED_FILL_COLOR, fill_type="solid")
                    result.failed_domains.append(target.domain)

                done += 1
                if progress_callback:
                    progress_callback(done, len(targets))

        wb.save(str(output_path))
        result.output_path = output_path
        logger.info(f"K列補完が完了しました: 要約 {result.summarized}件 / 失敗 {len(result.failed_domains)}件")
        return result

    @staticmethod
    def _fit_row_height(ws: Worksheet, row: int, column: int, text: str) -> None:
        """要約文が折り返しで見切れないよう、必要なら行の高さを広げる(縮めはしない)。"""
        width = ws.column_dimensions[get_column_letter(column)].width or 60
        needed = _estimate_wrapped_line_count(text, width) * 15 + 5
        current = ws.row_dimensions[row].height or 20
        ws.row_dimensions[row].height = max(current, needed)

    # ------------------------------------------------------------------
    # バックグラウンド実行(Streamlit画面からの呼び出し用)
    # ------------------------------------------------------------------

    def start_background_job(self, input_path: Path, output_path: Path, force_all: bool = False) -> EnrichmentJob:
        job = EnrichmentJob(id=str(uuid.uuid4()))
        with self._lock:
            self._jobs[job.id] = job

        def progress(done: int, total: int) -> None:
            with self._lock:
                job.done = done
                job.total = total

        def run() -> None:
            try:
                result = self.enrich_file(input_path, output_path, force_all=force_all, progress_callback=progress)
                with self._lock:
                    job.result = result
                    job.status = "completed"
            except Exception as e:
                logger.exception(f"K列補完ジョブ {job.id} が失敗しました: {e}")
                with self._lock:
                    job.error = str(e)
                    job.status = "failed"

        threading.Thread(target=run, name=f"Enricher-{job.id}", daemon=True).start()
        return job

    def get_job(self, job_id: str) -> EnrichmentJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    # ------------------------------------------------------------------
    # 1サイト分の取り直し + 要約
    # ------------------------------------------------------------------

    def summarize_domain(self, domain: str, current_description: str = "") -> str:
        top_html, profile_htmls = self.fetch_site_pages(domain)
        if not top_html:
            logger.warning(f"[{domain}] トップページを取得できなかったため、K列補完をスキップしました")
            return ""
        return self.summarizer.summarize(domain, top_html, profile_htmls, current_description)

    def fetch_site_pages(self, domain: str) -> tuple[str, list[str]]:
        """(トップページのHTML, 会社概要・事業内容らしいページのHTMLのリスト)を返す。"""
        crawler = WebCrawler(render_js=True, page_timeout=self.PAGE_TIMEOUT)

        if domain.startswith(("http://", "https://")):
            candidates = [domain]
        else:
            candidates = [f"https://{domain}", f"http://{domain}"]
            if not domain.lower().startswith("www."):
                candidates += [f"https://www.{domain}", f"http://www.{domain}"]

        with httpx.Client(headers=crawler.headers, timeout=self.PAGE_TIMEOUT, follow_redirects=True, verify=False) as client:
            top_html, top_url = "", ""
            for url in candidates:
                try:
                    response = client.get(url)
                except httpx.HTTPError:
                    continue
                if response.status_code < 400:
                    top_html, top_url = crawler._decode_response(response), str(response.url)
                    break

            if not top_html:
                return "", []

            # React/Vue等で静的HTMLに本文が無いサイトは、調査本体と同様にPlaywrightで描画し直す
            needs_render = bool(crawler._detect_js_framework(top_html)) or crawler._looks_like_empty_js_shell(top_html)
            if needs_render:
                rendered_html, rendered_url, _, _ = crawler._fetch_rendered_html(top_url)
                if rendered_html:
                    top_html, top_url = rendered_html, rendered_url or top_url

            profile_htmls: list[str] = []
            for url in self._find_profile_links(top_html, top_url):
                html = ""
                if needs_render:
                    html = crawler._fetch_rendered_html(url)[0]
                else:
                    try:
                        response = client.get(url)
                        if response.status_code < 400:
                            html = crawler._decode_response(response)
                    except httpx.HTTPError:
                        pass
                if html:
                    profile_htmls.append(html)

        return top_html, profile_htmls

    def _find_profile_links(self, html: str, page_url: str) -> list[str]:
        """トップページから、会社概要・事業内容らしい同一サイト内のページURLを探す。"""
        soup = BeautifulSoup(html, HTML_PARSER)
        base_host = urlparse(page_url).netloc.lower().removeprefix("www.")
        top_path = urlparse(page_url).path.rstrip("/")

        # リンクテキストで一致したものを優先し、URLだけで一致したものは後回しにする
        by_text: list[str] = []
        by_href: list[str] = []
        for a in soup.find_all("a", href=True):
            if not isinstance(a, Tag):
                continue
            href = str(a.get("href", "")).strip()
            if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue

            url = urljoin(page_url, href).split("#")[0]
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                continue
            same_site = parsed.netloc.lower().removeprefix("www.") == base_host
            if (same_site and parsed.path.rstrip("/") == top_path) or re.search(r"\.(pdf|jpe?g|png|gif|zip|docx?|xlsx?)$", parsed.path, re.I):
                continue

            # 大手企業では「企業情報」だけ別ドメインのコーポレートサイトに置いている
            # ことがある(例: 製品サイト kikkoman.co.jp → 企業情報 kikkoman.com)。
            # リンクテキストが明確に会社情報を指している場合に限り、別ドメインも辿る。
            # URLだけで一致したものは誤検知もありうるため、同一サイト内に限る。
            if self.PROFILE_LINK_TEXT_RE.search(a.get_text(strip=True)):
                by_text.append(url)
            elif same_site and self.PROFILE_LINK_HREF_RE.search(parsed.path):
                by_href.append(url)

        return list(dict.fromkeys(by_text + by_href))[: self.MAX_PROFILE_PAGES]
