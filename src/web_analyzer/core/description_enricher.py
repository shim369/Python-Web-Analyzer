import logging
import re
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag
from openpyxl import load_workbook
from openpyxl.styles import PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from web_analyzer.core.crawler import WebCrawler
from web_analyzer.core.evaluator import RenewalEvaluator
from web_analyzer.core.excel_service import ExcelService, _estimate_wrapped_line_count
from web_analyzer.core.site_summarizer import SiteSummarizer

logger = logging.getLogger(__name__)

HTML_PARSER = "lxml"


@dataclass
class EnrichmentTarget:
    row: int
    domain: str
    current_description: str
    needs_purpose: bool = True
    needs_structure: bool = False
    current_structure: str = ""
    needs_pages: bool = False
    current_pages: str = ""
    needs_contact: bool = False


@dataclass
class TargetCounts:
    """補完対象の件数(画面での事前表示用)。"""

    total_rows: int = 0
    # M列が「すでにリニューアル済」の行を除いた件数(=全行やり直す場合の対象件数)
    eligible_rows: int = 0
    purpose_rows: int = 0
    structure_rows: int = 0
    # ページ数が「〇〇以上」(100未満)で確定していない行数(全行やり直す場合も同じ)
    pages_rows: int = 0
    # 問合せ項目が空欄(未補完の「問い合わせフォームなし」を含む)の行数(全行やり直す場合も同じ)
    contact_rows: int = 0


@dataclass
class EnrichmentResult:
    total_rows: int = 0
    target_rows: int = 0
    summarized: int = 0
    failed_domains: list[str] = field(default_factory=list)
    structure_target_rows: int = 0
    structure_filled: int = 0
    structure_failed_domains: list[str] = field(default_factory=list)
    pages_target_rows: int = 0
    # ページ数が確定した件数(件数が増えたが、まだ「〇〇以上」の行は含まない)
    pages_confirmed: int = 0
    # 再計測してもページ数が確定しなかったサイト(件数が増えた行を含む)
    pages_unconfirmed_domains: list[str] = field(default_factory=list)
    # 再計測の結果、調査結果(C列)の判定を書き換えた件数
    judgement_updated: int = 0
    contact_target_rows: int = 0
    contact_filled: int = 0
    contact_failed_domains: list[str] = field(default_factory=list)
    # Geminiの利用上限に達していたため、用途・構成をAIで判定できなかったサイト
    rate_limited_domains: list[str] = field(default_factory=list)
    output_path: Path | None = None


@dataclass
class EnrichmentJob:
    id: str
    status: str = "processing"  # processing, completed, failed
    total: int = 0
    done: int = 0
    result: EnrichmentResult | None = None
    error: str = ""


@dataclass
class PageRecount:
    """ページ数の再計測結果と、それに基づいてやり直した判定。"""

    total_pages: int | str
    # ページ数が確定したか(巡回し尽くした、または100ページ以上と分かった)
    confirmed: bool
    evaluation_result: str
    rejection_reason: str
    remarks: str


@dataclass
class RowResult:
    summary: str = ""
    structure: str = ""
    pages: PageRecount | None = None
    contact: str = ""
    # Geminiの利用上限に達していたため、AIでの判定を飛ばしたか
    rate_limited: bool = False


@dataclass
class _Columns:
    domain: int
    purpose: int
    rejection: int
    structure: int
    result: int
    pages: int
    remarks: int
    contact: int


class DescriptionEnricher:
    """完成した調査結果Excelに対して、K列「用途」とH列「構成」をAI(Gemini)で補完する処理。

    - K列「用途」: 社名だけ等で内容の分かりにくい行を、サイト概要の要約で書き換える
    - H列「構成」: 空欄・3項目未満・電話番号等が混ざった行を、グローバルナビの
      メニュー項目で埋める。AIにはページ内のメニューのリスト(候補)から番号で
      選ばせるだけにし、項目名はサイト上の文字をそのまま使う

    スクレイピング本体とは切り離して実行するため、
      - 調査処理の速度やGemini側の制限(無料枠のレート上限等)の影響を受けない
      - 過去に出力したExcelにも後からかけられる
      - 1日の上限で途中までしか補完できなかった場合も、同じファイルに再度かければ
        残りの行だけが対象になる(補完済みの行は判定条件に引っかからないため)
    という運用ができる。

    Excelにはページの中身が残っていないため、対象行のサイトだけトップページと
    会社概要・事業内容らしいページ(最大2ページ)を取り直して使う。用途と構成の
    両方が対象の行でも、取り直しは1回で済ませる。
    """

    # これ未満の文字数は「社名・サイト名だけ」とみなして要約対象にする
    MIN_DESCRIPTION_CHARS = 40

    # 句点が無く、これらの区切り記号を含むものは「山田工業株式会社｜精密金属加工｜愛知県」の
    # ような名前・キーワードの羅列とみなして要約対象にする
    NAME_LIST_SEPARATOR_RE = re.compile(r"[|｜／/]|\s[-–—]\s|[-–—]{2,}")

    # 構成列がこれ未満の項目数なら、グローバルナビを取り損ねたとみなして補完対象にする
    MIN_STRUCTURE_ITEMS = 3
    # 構成列にこれらが含まれる場合は、ナビ以外(連絡先・外部リンク集・コピーライト等)を
    # 拾ったとみなして補完対象にする
    SUSPICIOUS_STRUCTURE_RE = re.compile(r"\d{2,4}-\d{2,4}-\d{3,4}|@|↗|copyright|©|all rights reserved", re.I)
    # 構成の候補にできる最大項目数。これを超える候補は切り詰めずに使わない(調査本体と同じ値)
    MAX_STRUCTURE_ITEMS = WebCrawler.MAX_STRUCTURE_ITEMS
    # SNSへのリンクだけの項目(構成の候補からは取り除く)
    SNS_ITEM_RE = WebCrawler.SNS_ITEM_RE

    # 会社概要・事業内容が書かれていそうなページへのリンクの目印(リンクテキスト/URL)
    PROFILE_LINK_TEXT_RE = re.compile(r"会社概要|企業情報|会社案内|会社情報|事業内容|事業紹介|業務内容|私たちについて|当社について|about", re.I)
    PROFILE_LINK_HREF_RE = re.compile(r"about|company|corporate|profile|gaiyou|gaiyo|outline|business|jigyou|jigyo", re.I)
    MAX_PROFILE_PAGES = 2

    DOMAIN_HEADER = "サイト名"
    PURPOSE_HEADER = "用途"
    REJECTION_HEADER = "不可の理由"
    STRUCTURE_HEADER = "構成"
    RESULT_HEADER = "調査結果"
    PAGES_HEADER = "ページ数"
    REMARKS_HEADER = "備考"
    CONTACT_HEADER = "問合せ項目"
    DEFAULT_DOMAIN_COLUMN = 2  # B列
    DEFAULT_STRUCTURE_COLUMN = 8  # H列
    DEFAULT_PURPOSE_COLUMN = 11  # K列
    DEFAULT_REJECTION_COLUMN = 13  # M列
    DEFAULT_RESULT_COLUMN = 3  # C列
    DEFAULT_PAGES_COLUMN = 9  # I列
    DEFAULT_REMARKS_COLUMN = 14  # N列
    DEFAULT_CONTACT_COLUMN = 12  # L列
    # M列(不可の理由)がこれを含む行は、移転案内ページ等で既にリニューアル済みのサイトで
    # 用途・構成を書く必要が無いため、補完せず空欄のままにする
    # (「全行をやり直す」場合も対象外。scraper_service.pyの判定文言と合わせる)
    ALREADY_RENEWED_REASON = "すでにリニューアル済"

    # サイトの取り直しは並列で行い、Geminiへの送信間隔(SiteSummarizer側で制御)の
    # 待ち時間とページ取得の時間を重ねる。Playwrightでのレンダリングも走りうるため
    # 調査本体と同じく5並列程度に抑える。
    MAX_WORKERS = 5
    PAGE_TIMEOUT = 10.0

    # ページ数の再計測では、調査本体(本巡回150秒 + ページ数カウント専用の追加巡回120秒、
    # 1リクエスト8秒)より時間予算を延ばす。対象は時間切れで「〇〇以上」になった行だけなので、
    # 1件あたり最大10分程度かかっても全体への影響は小さい。
    RECOUNT_TIMEOUT = 300.0
    RECOUNT_EXTRA_TIMEOUT = 300.0
    RECOUNT_PAGE_TIMEOUT = 15.0
    # ページ数がこの値以上なら「100以上」で確定扱い(crawler.pyの上限と同じ)
    PAGE_COUNT_CAP = 100
    # 調査結果(C列)の判定に使うページ数のしきい値(RenewalEvaluator.evaluate()の既定値と同じ)
    PAGE_THRESHOLD = 10
    PAGE_COUNT_REASON = "ページ数が多いため"

    # 補完の結果をセル背景で見分けられるようにする。
    # 失敗した行は、出力ファイルに再度かけて成功すれば黄色に塗り替わる。
    SUMMARIZED_FILL_COLOR = "FFF2CC"  # 淡い黄色: 補完で書き換えた
    FAILED_FILL_COLOR = "FFE5EC"  # 淡いピンク: 補完できず元の値のまま

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

    @classmethod
    def needs_structure(cls, text: str) -> bool:
        """H列の文字列が、グローバルナビを取り損ねたものかどうか。"""
        items = [line for line in (text or "").splitlines() if line.strip()]
        if len(items) < cls.MIN_STRUCTURE_ITEMS:
            return True
        return any(cls.SUSPICIOUS_STRUCTURE_RE.search(item) for item in items)

    @classmethod
    def needs_page_recount(cls, value: object) -> bool:
        """I列の値が、時間切れで確定しなかったページ数(「〇〇以上」で100未満)かどうか。"""
        if value is None:
            return False
        count, is_lower_bound = RenewalEvaluator.parse_lower_bound(str(value).strip())
        return is_lower_bound and 0 < count < cls.PAGE_COUNT_CAP

    @classmethod
    def needs_contact(cls, value: object, pages_value: object, flagged: bool) -> bool:
        """L列が、お問い合わせフォームを取り損ねた可能性のあるものかどうか。

        調査本体では、「問い合わせフォームなし」に取り損ねの疑いがある場合だけセルに
        背景色(グレー)を付けるため、その行(flagged)を対象にする。補完できずピンクに
        なった行も、もう一度かけたときに対象にする。色の無い「問い合わせフォームなし」は、
        お問い合わせページが電話番号の案内だけ等で妥当なことが多いため対象外。
        空欄の行(一時期の調査結果ではフォームが見つからなければ空欄にしていた)も対象にする。
        巡回できなかった(ページ数が空欄の)行は対象外。
        """
        if pages_value is None or str(pages_value).strip() in ("", "0"):
            return False
        text = str(value).strip() if value is not None else ""
        return not text or (text == WebCrawler.NO_CONTACT_FORM_TEXT and flagged)

    def find_targets(
        self,
        ws: Worksheet,
        force_all: bool = False,
        fill_purpose: bool = True,
        fill_structure: bool = False,
        recount_pages: bool = False,
        fill_contact: bool = False,
    ) -> tuple[int, list[EnrichmentTarget]]:
        """(データ行数, 補完対象行のリスト)を返す。

        ページ数の再計測は、全行やり直す(force_all)場合も「〇〇以上」の行だけを対象にする
        (確定済みのページ数を測り直す意味は無く、1件あたりの時間もかかるため)。
        問合せ項目も同様に、空欄・要確認の行だけを対象にする(取れている項目は、調査本体と
        同じルールで取り直しても変わらないため)。
        """
        cols = self._locate_columns(ws)
        total_rows = 0
        targets: list[EnrichmentTarget] = []

        for row in range(2, ws.max_row + 1):
            domain_value = ws.cell(row=row, column=cols.domain).value
            domain = str(domain_value).strip() if domain_value is not None else ""
            if not domain:
                continue
            total_rows += 1

            rejection_value = ws.cell(row=row, column=cols.rejection).value
            if rejection_value is not None and self.ALREADY_RENEWED_REASON in str(rejection_value):
                continue

            purpose_value = ws.cell(row=row, column=cols.purpose).value
            current = str(purpose_value).strip() if purpose_value is not None else ""
            structure_value = ws.cell(row=row, column=cols.structure).value
            current_structure = str(structure_value).strip() if structure_value is not None else ""

            pages_value = ws.cell(row=row, column=cols.pages).value
            current_pages = str(pages_value).strip() if pages_value is not None else ""

            needs_purpose = fill_purpose and (force_all or self.needs_summary(current))
            needs_structure = fill_structure and (force_all or self.needs_structure(current_structure))
            needs_pages = recount_pages and self.needs_page_recount(current_pages)
            contact_cell = ws.cell(row=row, column=cols.contact)
            contact_fill = str(contact_cell.fill.fgColor.rgb) if contact_cell.fill.fill_type else ""
            contact_flagged = contact_fill.endswith((ExcelService.UNKNOWN_FILL_COLOR, self.FAILED_FILL_COLOR))
            needs_contact = fill_contact and self.needs_contact(contact_cell.value, pages_value, contact_flagged)
            if needs_purpose or needs_structure or needs_pages or needs_contact:
                targets.append(
                    EnrichmentTarget(
                        row=row,
                        domain=domain,
                        current_description=current,
                        needs_purpose=needs_purpose,
                        needs_structure=needs_structure,
                        current_structure=current_structure,
                        needs_pages=needs_pages,
                        current_pages=current_pages,
                        needs_contact=needs_contact,
                    )
                )

        return total_rows, targets

    def count_targets(self, source: Path | BinaryIO) -> TargetCounts:
        """用途・構成・ページ数それぞれの補完対象件数(全行やり直さない場合)を数える。画面での事前表示用。"""
        wb = load_workbook(source)
        ws = wb.active
        assert isinstance(ws, Worksheet)
        total_rows, eligible = self.find_targets(ws, force_all=True)
        _, targets = self.find_targets(ws, fill_purpose=True, fill_structure=True, recount_pages=True, fill_contact=True)
        return TargetCounts(
            total_rows=total_rows,
            eligible_rows=len(eligible),
            purpose_rows=sum(t.needs_purpose for t in targets),
            structure_rows=sum(t.needs_structure for t in targets),
            pages_rows=sum(t.needs_pages for t in targets),
            contact_rows=sum(t.needs_contact for t in targets),
        )

    def _locate_columns(self, ws: Worksheet) -> _Columns:
        """ヘッダー行から各列を探す。見つからなければB列/K列/M列/H列とみなす。"""
        cols = _Columns(
            domain=self.DEFAULT_DOMAIN_COLUMN,
            purpose=self.DEFAULT_PURPOSE_COLUMN,
            rejection=self.DEFAULT_REJECTION_COLUMN,
            structure=self.DEFAULT_STRUCTURE_COLUMN,
            result=self.DEFAULT_RESULT_COLUMN,
            pages=self.DEFAULT_PAGES_COLUMN,
            remarks=self.DEFAULT_REMARKS_COLUMN,
            contact=self.DEFAULT_CONTACT_COLUMN,
        )
        for cell in ws[1]:
            header = str(cell.value).strip() if cell.value is not None else ""
            if cell.column is None:
                continue
            if header == self.DOMAIN_HEADER:
                cols.domain = cell.column
            elif header == self.PURPOSE_HEADER:
                cols.purpose = cell.column
            elif header == self.REJECTION_HEADER:
                cols.rejection = cell.column
            elif header == self.STRUCTURE_HEADER:
                cols.structure = cell.column
            elif header == self.RESULT_HEADER:
                cols.result = cell.column
            elif header == self.PAGES_HEADER:
                cols.pages = cell.column
            elif header == self.REMARKS_HEADER:
                cols.remarks = cell.column
            elif header == self.CONTACT_HEADER:
                cols.contact = cell.column
        return cols

    # ------------------------------------------------------------------
    # Excel一括処理
    # ------------------------------------------------------------------

    def enrich_file(
        self,
        input_path: Path,
        output_path: Path,
        force_all: bool = False,
        progress_callback: Callable[[int, int], None] | None = None,
        fill_purpose: bool = True,
        fill_structure: bool = False,
        recount_pages: bool = False,
        fill_contact: bool = False,
    ) -> EnrichmentResult:
        """input_pathのExcelのK列(用途)・H列(構成)の補完と、I列(ページ数)の再計測を行い、output_pathに保存する。

        元のExcelの書式(列幅・罫線・フォント等)はそのまま残し、書き換えた
        セルの値と、その行の高さだけを更新する。
        """
        wb = load_workbook(str(input_path))
        ws = wb.active
        assert isinstance(ws, Worksheet)

        total_rows, targets = self.find_targets(
            ws,
            force_all=force_all,
            fill_purpose=fill_purpose,
            fill_structure=fill_structure,
            recount_pages=recount_pages,
            fill_contact=fill_contact,
        )
        cols = self._locate_columns(ws)
        result = EnrichmentResult(
            total_rows=total_rows,
            target_rows=sum(t.needs_purpose for t in targets),
            structure_target_rows=sum(t.needs_structure for t in targets),
            pages_target_rows=sum(t.needs_pages for t in targets),
            contact_target_rows=sum(t.needs_contact for t in targets),
        )
        logger.info(
            f"AI補完を開始します: 用途 {result.target_rows}件 / 構成 {result.structure_target_rows}件 / "
            f"ページ数 {result.pages_target_rows}件 / 問合せ項目 {result.contact_target_rows}件 / 全 {total_rows}件"
        )

        if progress_callback:
            progress_callback(0, len(targets))

        done = 0
        with ThreadPoolExecutor(max_workers=self.MAX_WORKERS, thread_name_prefix="Enricher") as executor:
            future_to_target = {executor.submit(self.enrich_row, t): t for t in targets}
            for future in as_completed(future_to_target):
                target = future_to_target[future]
                try:
                    row_result = future.result()
                except Exception as e:
                    logger.warning(f"[{target.domain}] AI補完中に予期せぬエラーが発生しました: {e}")
                    row_result = RowResult()

                if target.needs_purpose:
                    if self._write_cell(ws, target.row, cols.purpose, row_result.summary):
                        result.summarized += 1
                    else:
                        result.failed_domains.append(target.domain)
                if target.needs_structure:
                    if self._write_cell(ws, target.row, cols.structure, row_result.structure):
                        result.structure_filled += 1
                    else:
                        result.structure_failed_domains.append(target.domain)
                if target.needs_contact:
                    if self._write_cell(ws, target.row, cols.contact, row_result.contact):
                        result.contact_filled += 1
                    else:
                        result.contact_failed_domains.append(target.domain)
                if target.needs_pages:
                    self._write_page_recount(ws, cols, target, row_result.pages, result)
                if row_result.rate_limited:
                    result.rate_limited_domains.append(target.domain)

                done += 1
                if progress_callback:
                    progress_callback(done, len(targets))

        wb.save(str(output_path))
        result.output_path = output_path
        logger.info(
            f"AI補完が完了しました: 用途 {result.summarized}件(失敗 {len(result.failed_domains)}件) / "
            f"構成 {result.structure_filled}件(失敗 {len(result.structure_failed_domains)}件) / "
            f"問合せ項目 {result.contact_filled}件(失敗 {len(result.contact_failed_domains)}件) / "
            f"ページ数 確定 {result.pages_confirmed}件(未確定 {len(result.pages_unconfirmed_domains)}件) / "
            f"判定の更新 {result.judgement_updated}件"
        )
        return result

    def _write_page_recount(
        self,
        ws: Worksheet,
        cols: _Columns,
        target: EnrichmentTarget,
        recount: PageRecount | None,
        result: EnrichmentResult,
    ) -> None:
        """ページ数の再計測結果をI列に書き込み、必要なら調査結果(C/M/N列)も書き換える。

        - ページ数が確定した(巡回し尽くした・100以上と分かった)場合はI列を黄色に、
          確定しなかった場合はピンクにする。確定しなくても件数が増えた場合は値を更新する。
        - 時間切れで「要確認」(判定保留)だった行は、再計測した巡回結果で判定をやり直す。
        - 既に「×」の行は、ページ数がしきい値を超えたことが新たに分かった場合だけ、
          不可の理由に「ページ数が多いため」を追記する(他の理由は変えない)。
        """
        old_count, _ = RenewalEvaluator.parse_lower_bound(target.current_pages)
        new_count = RenewalEvaluator.parse_lower_bound(recount.total_pages)[0] if recount else 0
        if recount is None or (new_count <= old_count and not recount.confirmed):
            self._write_cell(ws, target.row, cols.pages, "")
            result.pages_unconfirmed_domains.append(target.domain)
            return

        pages_cell = ws.cell(row=target.row, column=cols.pages)
        pages_cell.value = recount.total_pages
        fill_color = self.SUMMARIZED_FILL_COLOR if recount.confirmed else self.FAILED_FILL_COLOR
        pages_cell.fill = PatternFill(start_color=fill_color, end_color=fill_color, fill_type="solid")
        if recount.confirmed:
            result.pages_confirmed += 1
        else:
            result.pages_unconfirmed_domains.append(target.domain)

        current_result = str(ws.cell(row=target.row, column=cols.result).value or "").strip()
        current_rejection = str(ws.cell(row=target.row, column=cols.rejection).value or "").strip()
        current_remarks = str(ws.cell(row=target.row, column=cols.remarks).value or "").strip()

        if current_result == "要確認" and RenewalEvaluator.PENDING_TIMEOUT_REASON in current_remarks:
            updated = (recount.evaluation_result, recount.rejection_reason, recount.remarks)
            if updated == (current_result, current_rejection, current_remarks):
                return
            for column, value in zip((cols.result, cols.rejection, cols.remarks), updated, strict=True):
                ws.cell(row=target.row, column=column).value = value or None
                if value:
                    self._write_cell(ws, target.row, column, value)
            result.judgement_updated += 1
        elif current_result == "×" and new_count > self.PAGE_THRESHOLD and self.PAGE_COUNT_REASON not in current_rejection:
            self._write_cell(ws, target.row, cols.rejection, "\n".join(filter(None, [self.PAGE_COUNT_REASON, current_rejection])))
            result.judgement_updated += 1

    def _write_cell(self, ws: Worksheet, row: int, column: int, value: str) -> bool:
        """補完結果をセルに書き込み、背景色で結果を示す。書き込めた場合True。"""
        cell = ws.cell(row=row, column=column)
        if value:
            cell.value = value
            cell.fill = PatternFill(start_color=self.SUMMARIZED_FILL_COLOR, end_color=self.SUMMARIZED_FILL_COLOR, fill_type="solid")
            self._fit_row_height(ws, row, column, value)
            return True
        cell.fill = PatternFill(start_color=self.FAILED_FILL_COLOR, end_color=self.FAILED_FILL_COLOR, fill_type="solid")
        return False

    @staticmethod
    def _fit_row_height(ws: Worksheet, row: int, column: int, text: str) -> None:
        """補完した文字列が折り返しで見切れないよう、必要なら行の高さを広げる(縮めはしない)。"""
        width = ws.column_dimensions[get_column_letter(column)].width or 60
        needed = _estimate_wrapped_line_count(text, width) * 15 + 5
        current = ws.row_dimensions[row].height or 20
        ws.row_dimensions[row].height = max(current, needed)

    # ------------------------------------------------------------------
    # バックグラウンド実行(Streamlit画面からの呼び出し用)
    # ------------------------------------------------------------------

    def start_background_job(
        self,
        input_path: Path,
        output_path: Path,
        force_all: bool = False,
        fill_purpose: bool = True,
        fill_structure: bool = False,
        recount_pages: bool = False,
        fill_contact: bool = False,
    ) -> EnrichmentJob:
        job = EnrichmentJob(id=str(uuid.uuid4()))
        with self._lock:
            self._jobs[job.id] = job

        def progress(done: int, total: int) -> None:
            with self._lock:
                job.done = done
                job.total = total

        def run() -> None:
            try:
                result = self.enrich_file(
                    input_path,
                    output_path,
                    force_all=force_all,
                    progress_callback=progress,
                    fill_purpose=fill_purpose,
                    fill_structure=fill_structure,
                    recount_pages=recount_pages,
                    fill_contact=fill_contact,
                )
                with self._lock:
                    job.result = result
                    job.status = "completed"
            except Exception as e:
                logger.exception(f"AI補完ジョブ {job.id} が失敗しました: {e}")
                with self._lock:
                    job.error = str(e)
                    job.status = "failed"

        threading.Thread(target=run, name=f"Enricher-{job.id}", daemon=True).start()
        return job

    def get_job(self, job_id: str) -> EnrichmentJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    # ------------------------------------------------------------------
    # 1サイト分の取り直し + 要約・構成の判定
    # ------------------------------------------------------------------

    def enrich_row(self, target: EnrichmentTarget) -> RowResult:
        """1行分の補完結果を返す。対象でない列・補完できなかった列は空文字(ページ数はNone)。"""
        result = RowResult()
        self.summarizer.pop_rate_limited()
        if target.needs_structure:
            top_html, profile_htmls = self.fetch_site_pages(target.domain)
            if top_html:
                if target.needs_purpose:
                    result.summary = self.summarizer.summarize(target.domain, top_html, profile_htmls, target.current_description)
                iframe_menus = self.fetch_iframe_nav_menus(target.domain, top_html)
                result.structure = self.select_structure(target.domain, top_html, iframe_menus)
                if not result.structure and not self._structure_candidates(top_html, iframe_menus)[1]:
                    # ヘッダーをJavaScriptで後から読み込むサイト(<header>が空で、jQueryの
                    # .load("header.html")等で差し込む)は、静的HTMLにナビが1つも無い。
                    # JSフレームワークの検知には掛からないため、候補が1つも無い場合に限り
                    # ブラウザで描画したトップページで判定し直す。
                    rendered_html = self.render_top_page(target.domain)
                    if rendered_html and rendered_html != top_html:
                        result.structure = self.select_structure(target.domain, rendered_html)
            else:
                logger.warning(f"[{target.domain}] トップページを取得できなかったため、AI補完をスキップしました")
        elif target.needs_purpose:
            result.summary = self.summarize_domain(target.domain, target.current_description)
        if target.needs_contact:
            result.contact = self.find_contact_fields(target.domain)
        result.rate_limited = self.summarizer.pop_rate_limited()

        if target.needs_pages:
            result.pages = self.recount_pages(target.domain)
        return result

    def recount_pages(self, domain: str) -> PageRecount | None:
        """時間予算を延ばしてサイトを巡回し直し、ページ数と、それに基づく判定をやり直す。

        数え方・判定は調査本体(scraper_service.py)と同じ処理を使い、本巡回・
        ページ数カウント専用の追加巡回の時間予算だけを延ばす。巡回できなかった
        (ブロック・移転案内等を含む)場合はNone。
        """
        crawler = WebCrawler(render_js=True, timeout=self.RECOUNT_TIMEOUT, page_timeout=self.RECOUNT_PAGE_TIMEOUT)
        crawler.COUNT_ONLY_EXTRA_TIMEOUT = self.RECOUNT_EXTRA_TIMEOUT
        try:
            (
                total_pages,
                max_depth,
                _contact_fields,
                _site_structure,
                _site_purpose,
                html_src,
                _cms_name,
                has_attachment,
                has_login,
                has_basic_auth,
                has_multilang,
                blocked_reason,
                redirect_target_url,
                queue_exhausted,
                _subsystem_note,
            ) = crawler.crawl_and_analyze(domain)
        except Exception as e:
            logger.warning(f"[{domain}] ページ数の再計測中にエラーが発生しました: {e}")
            return None

        evaluator = RenewalEvaluator()
        total_pages_int, _ = evaluator.parse_lower_bound(total_pages)
        if blocked_reason or redirect_target_url or total_pages_int == 0:
            return None

        eval_result, rejection_reason = evaluator.decide(
            total_pages=total_pages_int,
            max_depth=max_depth,
            has_login=bool(has_login),
            has_attachment=bool(has_attachment),
            has_basic_auth=bool(has_basic_auth),
            has_multilang=bool(has_multilang),
            html_src=html_src,
            queue_exhausted=bool(queue_exhausted),
        )
        remarks = ""
        # scraper_service.pyと同様、判定保留・目視確認の注記は不可の理由ではなく備考に出す
        if rejection_reason in (evaluator.PENDING_TIMEOUT_REASON, evaluator.NO_NG_REASON_NOTE):
            remarks, rejection_reason = rejection_reason, ""

        return PageRecount(
            total_pages=f"{total_pages_int}以上" if isinstance(total_pages, str) else total_pages_int,
            confirmed=not isinstance(total_pages, str) or total_pages_int >= 100,
            evaluation_result=eval_result,
            rejection_reason=rejection_reason,
            remarks=remarks,
        )

    def select_structure(self, domain: str, top_html: str, iframe_menus: list[str] | None = None) -> str:
        """トップページのHTMLから、構成列に入れるグローバルナビの項目(改行区切り)を決める。

        調査本体と同じルールで取れた結果と、ページ内のメニューのリストを候補にし、
        (ナビをiframeで読み込むサイトでは、iframeのページから取れたナビ(iframe_menus)を
        調査本体と同じく優先し、ルールで取れた結果として先頭の候補にする)
        AIに番号で選ばせる(候補が1つでも、それがグローバルナビかどうかをAIに判定させる)。
        AIの呼び出しに失敗した場合は、ルールで取れた結果を使う。各候補からは電話番号・
        SNS等のナビ以外の項目を取り除き、残りが3項目未満の候補は使わない。
        決められなかった場合は空文字。
        """
        rule_menus, candidates = self._structure_candidates(top_html, iframe_menus)

        chosen: list[str] = []
        if candidates:
            index = self.summarizer.select_global_nav(domain, candidates)
            if index is None:
                # API呼び出しの失敗(上限到達等)。ルールで取れた結果があればそれを使う
                chosen = rule_menus if rule_menus in candidates else []
            elif index != SiteSummarizer.NO_GLOBAL_NAV:
                chosen = candidates[index]

        if not chosen:
            logger.info(f"[{domain}] 構成(グローバルナビ)を判定できませんでした(候補 {len(candidates)}件)")
        return "\n".join(chosen)

    def _structure_candidates(self, top_html: str, iframe_menus: list[str] | None = None) -> tuple[list[str], list[list[str]]]:
        """(ルールで取れたナビ項目, AIに選ばせる候補のリスト)を返す。"""
        crawler = WebCrawler()
        page_menus = self._clean_structure_items(crawler._extract_global_nav_menus(BeautifulSoup(top_html, HTML_PARSER), crawler._detect_cms(top_html)))
        frame_menus = self._clean_structure_items(iframe_menus or [])
        rule_menus = frame_menus if self._is_structure_size(frame_menus) else page_menus

        candidates: list[list[str]] = []
        for menus in (rule_menus, page_menus):
            if self._is_structure_size(menus) and menus not in candidates:
                candidates.append(menus)
        for menus in crawler.collect_nav_candidates(BeautifulSoup(top_html, HTML_PARSER)):
            cleaned = self._clean_structure_items(menus)
            if self._is_structure_size(cleaned) and cleaned not in candidates:
                candidates.append(cleaned)
        return rule_menus, candidates

    @classmethod
    def _is_structure_size(cls, menus: list[str]) -> bool:
        """構成の候補にできる項目数か。上限を超える候補は途中で切ると中途半端になるため、
        切り詰めずに候補から外す(調査本体で上限を超えた場合に空欄にするのと同じ考え方)。"""
        return cls.MIN_STRUCTURE_ITEMS <= len(menus) <= cls.MAX_STRUCTURE_ITEMS

    @classmethod
    def _clean_structure_items(cls, menus: list[str]) -> list[str]:
        """メニュー項目から、電話番号・メールアドレス・外部リンク・SNS等のナビ以外の項目を除く。"""
        return [m for m in menus if not cls.SUSPICIOUS_STRUCTURE_RE.search(m) and not cls.SNS_ITEM_RE.match(m)]

    def summarize_domain(self, domain: str, current_description: str = "") -> str:
        top_html, profile_htmls = self.fetch_site_pages(domain)
        if not top_html:
            logger.warning(f"[{domain}] トップページを取得できなかったため、K列補完をスキップしました")
            return ""
        return self.summarizer.summarize(domain, top_html, profile_htmls, current_description)

    def fetch_site_pages(self, domain: str) -> tuple[str, list[str]]:
        """(トップページのHTML, 会社概要・事業内容らしいページのHTMLのリスト)を返す。"""
        crawler = WebCrawler(render_js=True, page_timeout=self.PAGE_TIMEOUT)

        with httpx.Client(headers=crawler.headers, timeout=self.PAGE_TIMEOUT, follow_redirects=True, verify=False) as client:
            top_html, top_url = self._fetch_top_page(client, crawler, domain)
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

    def fetch_iframe_nav_menus(self, domain: str, top_html: str) -> list[str]:
        """トップページがナビをiframeで読み込む作りの場合に、iframeのページからナビの項目を取る(調査本体と同じ処理)。

        ナビらしいiframeが無いページでは、ページを取り直さずに空リストを返す。
        """
        crawler = WebCrawler(page_timeout=self.PAGE_TIMEOUT)
        soup = BeautifulSoup(top_html, HTML_PARSER)
        if not crawler._has_nav_iframe(soup):
            return []
        with httpx.Client(headers=crawler.headers, timeout=self.PAGE_TIMEOUT, follow_redirects=True, verify=False) as client:
            _, top_url = self._fetch_top_page(client, crawler, domain)
            if not top_url:
                return []
            return crawler._extract_iframe_nav_menus(soup, top_url, client, crawler._detect_cms(top_html))

    def render_top_page(self, domain: str) -> str:
        """トップページをブラウザ(Playwright)で描画したHTMLを返す。取得できなければ空文字。"""
        crawler = WebCrawler(render_js=True, page_timeout=self.PAGE_TIMEOUT)
        with httpx.Client(headers=crawler.headers, timeout=self.PAGE_TIMEOUT, follow_redirects=True, verify=False) as client:
            _, top_url = self._fetch_top_page(client, crawler, domain)
        if not top_url:
            return ""
        return crawler._fetch_rendered_html(top_url)[0]

    # 問合せ項目の補完で取得するページ数(お問い合わせページ・その先のフォームページ)と、
    # そのうち入力欄が無いためブラウザで描画し直す最大ページ数
    MAX_CONTACT_PAGES = 5
    MAX_CONTACT_RENDERS = 2
    MAX_CONTACT_LINKS_FROM_TOP = 3
    # AIに渡すお問い合わせページ本文の1ページあたりの最大文字数
    MAX_CONTACT_TEXT_CHARS = 3000
    # お問い合わせページの先にある、フォーム本体のページへのリンク・埋め込み
    # (「メールフォーム」「お問い合わせフォームへ」、/cgi-bin/form/、mail2.html、Googleフォーム等)
    FORM_LINK_RE = re.compile(r"フォーム|form|入力|mail", re.I)
    CONTACT_HREF_RE = re.compile(r"contact|inquiry|toiawase|otoiawase", re.I)

    def find_contact_fields(self, domain: str) -> str:
        """お問い合わせページを取り直し、L列(問合せ項目)に入れる文字列を返す。決められなければ空文字。

        調査本体でフォームが見つからなかったサイトには、フォームが別ページ・外部サービス
        (Googleフォーム等)・JavaScript描画のものが混ざる。トップページのお問い合わせ
        リンクから、その先のフォームページ・埋め込みまで辿り、入力欄が無ければブラウザで
        描画し直して、調査本体と同じルールで項目を取る。それでも取れなければ、ページ本文を
        AIに読ませる。AIがフォームは無いと判定した場合は「mailto」(mailtoリンクがある場合)
        または「問い合わせフォームなし」を返す。
        """
        crawler = WebCrawler(render_js=True, page_timeout=self.PAGE_TIMEOUT)
        with httpx.Client(headers=crawler.headers, timeout=self.PAGE_TIMEOUT, follow_redirects=True, verify=False) as client:
            top_html, top_url = self._fetch_top_page(client, crawler, domain)
            if not top_html:
                logger.warning(f"[{domain}] トップページを取得できなかったため、問合せ項目の補完をスキップしました")
                return ""
            # ヘッダーをJavaScriptで後から差し込むサイトは、静的HTMLにお問い合わせへのリンクが無い
            if crawler._detect_js_framework(top_html) or crawler._has_empty_header(top_html):
                rendered_html, rendered_url, _, _ = crawler._fetch_rendered_html(top_url)
                if rendered_html:
                    top_html, top_url = rendered_html, rendered_url or top_url

            queue = self._find_contact_links(top_html, top_url)[: self.MAX_CONTACT_LINKS_FROM_TOP]
            if not queue:
                logger.info(f"[{domain}] お問い合わせページへのリンクが見つかりませんでした")
                return ""
            site_host = urlparse(top_url).netloc.lower().removeprefix("www.")
            has_mailto = False
            visited: set[str] = set()
            pages: list[tuple[str, str]] = []
            renders = 0
            while queue and len(visited) < self.MAX_CONTACT_PAGES:
                url = queue.pop(0)
                if url in visited:
                    continue
                visited.add(url)
                try:
                    response = client.get(url)
                except httpx.HTTPError:
                    continue
                if response.status_code >= 400:
                    continue
                html, page_url = crawler._decode_response(response), str(response.url)

                fields = self._rule_contact_fields(crawler, client, html, page_url)
                if not fields and renders < self.MAX_CONTACT_RENDERS and not crawler._has_form_like_controls(BeautifulSoup(html, HTML_PARSER)):
                    renders += 1
                    rendered_html = crawler._fetch_rendered_html(page_url)[0]
                    if rendered_html:
                        html = rendered_html
                        fields = self._rule_contact_fields(crawler, client, html, page_url)
                if fields:
                    return fields

                soup = BeautifulSoup(html, HTML_PARSER)
                has_mailto = has_mailto or soup.find("a", href=crawler.MAILTO_HREF_PATTERN) is not None
                pages.append((page_url, self._contact_page_text(soup)))
                # 外部のフォーム作成サービスのページ内のリンク(Googleフォームの規約・サービス紹介等)は辿らない
                if urlparse(page_url).netloc.lower().removeprefix("www.") == site_host:
                    queue.extend(u for u in self._find_form_links(soup, page_url) if u not in visited)

        texts = [(url, text) for url, text in pages if text]
        items = self.summarizer.extract_contact_fields(domain, texts)
        # 項目名が入力欄の外に書かれていない(画像・CSSで表示している)フォームでは、同意欄等の
        # 一部の項目しか読み取れない(実際に確認済み)。1項目だけのものは取り損ねとみなす
        if items is None or len(items) == 1:
            logger.info(f"[{domain}] 問合せ項目を判定できませんでした(取得したページ {len(pages)}件)")
            return ""
        if items:
            return "\n".join(items)
        return "mailto" if has_mailto else crawler.NO_CONTACT_FORM_TEXT

    @staticmethod
    def _rule_contact_fields(crawler: WebCrawler, client: httpx.Client, html: str, page_url: str) -> str:
        """調査本体と同じルールでフォームの項目を取る。お問い合わせフォームらしくなければ空文字。"""
        fields, _ = crawler._extract_form_fields(html, page_url, client, depth=0)
        if crawler._looks_like_genuine_contact_form(BeautifulSoup(html, HTML_PARSER), fields, is_contact_page=True):
            return fields
        return ""

    def _find_contact_links(self, html: str, page_url: str) -> list[str]:
        """トップページから、お問い合わせページらしいURLを探す(リンクテキストで一致したものを優先)。

        お問い合わせフォームは別ドメイン(フォーム作成サービス等)に置かれていることもあるため、
        リンクテキストが明確にお問い合わせを指している場合は別ドメインも辿る。
        """
        soup = BeautifulSoup(html, HTML_PARSER)
        base_host = urlparse(page_url).netloc.lower().removeprefix("www.")
        by_text: list[str] = []
        by_href: list[str] = []
        for a in soup.find_all("a", href=True):
            if not isinstance(a, Tag):
                continue
            url = self._link_url(a, page_url)
            if not url:
                continue
            same_site = urlparse(url).netloc.lower().removeprefix("www.") == base_host
            if WebCrawler.CONTACT_LINK_TEXT_RE.search(a.get_text(" ", strip=True)):
                by_text.append(url)
            elif same_site and self.CONTACT_HREF_RE.search(urlparse(url).path):
                by_href.append(url)
        return list(dict.fromkeys(by_text + by_href))

    def _find_form_links(self, soup: BeautifulSoup, page_url: str) -> list[str]:
        """お問い合わせページから、その先にあるフォーム本体のページ・埋め込み(iframe)のURLを探す。"""
        urls: list[str] = []
        for frame in soup.find_all("iframe", src=True):
            src = urljoin(page_url, str(frame.get("src", "")))
            if urlparse(src).scheme in ("http", "https") and (self.FORM_LINK_RE.search(src) or self.CONTACT_HREF_RE.search(src)):
                urls.append(src.split("#")[0])
        for a in soup.find_all("a", href=True):
            if not isinstance(a, Tag):
                continue
            url = self._link_url(a, page_url)
            if url and self.FORM_LINK_RE.search(a.get_text(" ", strip=True) + " " + urlparse(url).path + urlparse(url).netloc):
                urls.append(url)
        return list(dict.fromkeys(urls))

    @staticmethod
    def _link_url(a: Tag, page_url: str) -> str:
        """リンク先の絶対URL(#以降を除く)。ページ内リンク・mailto・画像等のファイルは空文字。"""
        href = str(a.get("href", "")).strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            return ""
        url = urljoin(page_url, href).split("#")[0]
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or url.rstrip("/") == page_url.split("#")[0].rstrip("/"):
            return ""
        if re.search(r"\.(pdf|jpe?g|png|gif|zip|docx?|xlsx?)$", parsed.path, re.I):
            return ""
        return url

    def _contact_page_text(self, soup: BeautifulSoup) -> str:
        """AIに渡すお問い合わせページの本文。入力欄は[入力欄]等の目印に置き換える。"""
        for tag in soup(["script", "style", "noscript", "svg", "nav", "header", "footer"]):
            tag.decompose()
        for tag in soup.find_all(["input", "textarea", "select"]):
            if not isinstance(tag, Tag):
                continue
            itype = str(tag.get("type", "text")).lower()
            if tag.name == "input" and itype in ("hidden", "submit", "button", "image", "reset"):
                tag.decompose()
                continue
            if tag.name == "textarea":
                marker = "[複数行入力欄]"
            elif tag.name == "select":
                marker = "[選択欄]"
            elif itype in ("checkbox", "radio"):
                marker = "[選択肢]"
            else:
                marker = "[入力欄]"
            tag.replace_with(f" {marker} ")
        forms = soup.find_all("form")
        root = soup.find("main") or soup.body or soup
        texts = [form.get_text(" ", strip=True) for form in forms] if any("[" in f.get_text() for f in forms) else [root.get_text(" ", strip=True)]
        text = re.sub(r"\s+", " ", " ".join(texts)).strip()
        return text[: self.MAX_CONTACT_TEXT_CHARS]

    # トップページが同一サイト内の別ページへmeta refresh等で移動するだけの場合に、辿る回数の上限
    MAX_TOP_PAGE_HOPS = 2

    def _fetch_top_page(self, client: httpx.Client, crawler: WebCrawler, domain: str) -> tuple[str, str]:
        """(トップページのHTML, そのURL)を返す。取得できなければ("", "")。

        トップページが「自動的に移動します」のように、同一サイト内の実際の
        トップページへmeta refresh・JavaScriptで移動するだけの薄いページの場合は、
        移動先のページを辿る(静的取得ではmeta refreshは実行されないため)。
        別ドメインへの移動(移転案内)は辿らない。
        """
        if domain.startswith(("http://", "https://")):
            candidates = [domain]
        else:
            candidates = [f"https://{domain}", f"http://{domain}"]
            if not domain.lower().startswith("www."):
                candidates += [f"https://www.{domain}", f"http://www.{domain}"]

        top_html, top_url = "", ""
        for url in candidates:
            try:
                response = client.get(url)
            except httpx.HTTPError:
                continue
            if response.status_code < 400:
                top_html, top_url = crawler._decode_response(response), str(response.url)
                break

        for _ in range(self.MAX_TOP_PAGE_HOPS):
            if not top_html:
                break
            target = crawler._extract_delayed_redirect_target(top_html, top_url)
            if not target or target.split("#")[0] == top_url:
                break
            if urlparse(target).netloc.lower().removeprefix("www.") != urlparse(top_url).netloc.lower().removeprefix("www."):
                break
            try:
                response = client.get(target)
            except httpx.HTTPError:
                break
            if response.status_code >= 400:
                break
            top_html, top_url = crawler._decode_response(response), str(response.url)
        return top_html, top_url

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
