import re
import uuid
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from web_analyzer.models import ScrapingJob, SiteAssessment
from web_analyzer.utils.decorators import log_action


def _estimate_wrapped_line_count(text: str, column_width: float) -> int:
    """openpyxlの列幅(文字数相当)から、折り返し後のおおよその行数を見積もる。

    Excel実機のフォントレンダリングと厳密には一致しない簡易近似だが、
    「列幅に上限を設けたことで長文が縦方向に見切れる」のを防ぐため、
    行の高さを決める目安として使う。
    """
    if not text:
        return 1

    # 列幅からセルの左右余白分を差し引いた、1行あたりのおおよその文字数
    chars_per_line = max(int(column_width) - 2, 1)

    total_lines = 0
    for line in str(text).split("\n"):
        # 全角文字を2文字として簡易計算(幅の自動調整ロジックと揃える)
        char_len = sum(2 if ord(c) > 127 else 1 for c in line)
        total_lines += max(1, -(-char_len // chars_per_line))  # 切り上げ除算

    return total_lines


class ExcelService:
    """Excelファイルのパースおよび生成を担当するサービス"""

    # 中央揃えにする列番号(日付, 調査結果, SSL, 階層, ページ数, 担当)。
    # それ以外は左揃え+折り返し(wrap_text)にする。
    _CENTER_ALIGN_COLUMNS = {1, 3, 4, 5, 6, 9, 15}

    # 折り返し対象列のうち、特に長文が入りうる列の幅に上限を設ける。
    # 上限を超えた分は、後段の行高さ自動調整で縦方向に折り返して吸収する。
    _WRAP_TEXT_COLUMNS = {2, 7, 8, 10, 11, 12, 13, 14}
    _MAX_WRAP_COLUMN_WIDTH = 60

    @staticmethod
    def import_excel(
        file_path: Path,
        operator_name: str,
        page_threshold: int = 10,
    ) -> tuple[ScrapingJob, list[SiteAssessment]]:
        """Excelからドメイン名(B列)を抽出し、パラメータを紐付けてモデルを生成する。"""
        job_id = str(uuid.uuid4())
        job = ScrapingJob(
            id=job_id,
            operator_name=operator_name,
            page_threshold=page_threshold,
            status="processing",
        )

        assessments: list[SiteAssessment] = []
        wb = load_workbook(str(file_path), data_only=True)
        ws = wb.active

        assert isinstance(ws, Worksheet)

        # アプリ実行時の日付 (例: "7/14") を取得
        current_date_str = datetime.now().strftime("%m/%d").lstrip("0").replace("/0", "/")

        # 2行目から開始（1行目はヘッダーを想定。インポート元のB列にサイト名がある前提）
        for row in range(2, ws.max_row + 1):
            cell_value = ws.cell(row=row, column=2).value

            if cell_value is None:
                cell_value = ws.cell(row=row, column=1).value

            if cell_value is None:
                continue

            domain = str(cell_value).strip()
            if not domain or domain == "サイト名":
                continue

            assessment = SiteAssessment(
                id=str(uuid.uuid4()),
                job_id=job_id,
                date_str=current_date_str,
                domain_name=domain,
                operator_name=operator_name,
            )
            assessments.append(assessment)

        return job, assessments

    @staticmethod
    @log_action("Excel出力")
    def export_excel(assessments: list[SiteAssessment], output_path: Path) -> None:
        """指定された解析結果リストをスタイリッシュなデザインでExcel出力する。"""
        wb = Workbook()
        ws = wb.active
        assert isinstance(ws, Worksheet)
        ws.title = "調査結果"

        # グリッド線（目盛線）を常に表示する設定
        ws.views.sheetView[0].showGridLines = True

        # スタイル定義
        font_family = "Yu Gothic"

        # フォント設定
        header_font = Font(name=font_family, size=11, bold=True, color="FFFFFF")
        data_font = Font(name=font_family, size=10)

        # 背景色設定（シックなネイビー）
        header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")

        # 罫線設定（極細のライトグレー）
        thin_border_side = Side(style="thin", color="D9D9D9")
        thin_border = Border(left=thin_border_side, right=thin_border_side, top=thin_border_side, bottom=thin_border_side)

        # 新仕様に基づいたヘッダー（A〜O列）
        headers = [
            "日付",
            "サイト名",
            "調査結果",
            "SSLあり",
            "SSL常時",
            "階層数",
            "svcmd",
            "構成",
            "ページ数",
            "使用CMS",
            "用途",
            "問合せ項目",
            "不可の理由",
            "備考",
            "担当",
        ]
        ws.append(headers)

        # ヘッダー行のデザイン適用
        for col_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=1, column=col_idx)
            cell.font = header_font
            cell.fill = header_fill
            cell.border = thin_border
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=False)

        # ヘッダーの行高を少し広げてゆとりを持たせる
        ws.row_dimensions[1].height = 26

        # データのマッピング書き込み
        for item in assessments:
            row_data = [
                item.date_str,
                item.domain_name,
                item.evaluation_result,
                item.has_ssl,
                item.is_always_ssl,
                item.max_depth if item.max_depth is not None else "",
                item.svcmd,
                item.site_structure,
                item.total_pages if item.total_pages is not None else "",
                item.cms_name,
                item.description,
                item.contact_fields,
                item.rejection_reason,
                item.remarks,
                item.operator_name,
            ]

            # Excelで保存できない制御文字（\x00〜\x1Fの改行等を除く文字）を消去する正規表現
            illegal_chars = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f]")

            # openpyxl標準のclean_stringを使って安全に除去する
            cleaned_row_data = [illegal_chars.sub("", str(val)) if isinstance(val, str) else val for val in row_data]

            ws.append(cleaned_row_data)

            # 追加したデータ行にデザインを適用（上揃え + 罫線 + フォント）
            # 行の高さは、この後の自動調整パスで内容量に応じて設定するため、
            # ここでは仮の値は入れない。
            current_row = ws.max_row

            for col_idx in range(1, len(row_data) + 1):
                cell = ws.cell(row=current_row, column=col_idx)
                cell.font = data_font
                cell.border = thin_border

                # 中央揃えにする列と、左揃え（上揃え）にする列を出し分ける
                if col_idx in ExcelService._CENTER_ALIGN_COLUMNS:
                    cell.alignment = Alignment(horizontal="center", vertical="top")
                else:
                    cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)

        # --- 【自動調整】各列の幅をコンテンツの最大長に合わせて調整する ---
        # (ただし長文が入りうる列は _MAX_WRAP_COLUMN_WIDTH で頭打ちにする)
        column_widths: dict[int, float] = {}

        for col in ws.columns:
            max_len = 0
            # col[0].column が None の場合は処理をスキップ（型安全性の確保）
            col_num = col[0].column
            if col_num is None:
                continue

            col_letter = get_column_letter(col_num)

            for cell in col:
                # セル値の文字数を簡易カウント（Noneはスキップ、改行がある場合は一番長い行を基準に）
                if cell.value is not None:
                    lines = str(cell.value).split("\n")
                    for line in lines:
                        # 全角文字を2文字として簡易計算
                        val_len = sum(2 if ord(c) > 127 else 1 for c in line)
                        if val_len > max_len:
                            max_len = val_len

            # 少し余白（パディング）を足して、最低幅も保証する
            width: float = float(max(max_len + 4, 10))

            if col_num in ExcelService._WRAP_TEXT_COLUMNS:
                # 1件でも極端に長い文章が入ると、その列だけ異常に幅広くなって
                # シート全体が横長になってしまうため、上限を設ける。
                # 上限を超えた分は縦方向の折り返しで表現する。
                width = min(width, ExcelService._MAX_WRAP_COLUMN_WIDTH)

            ws.column_dimensions[col_letter].width = width
            column_widths[col_num] = width

        # --- 【自動調整】折り返し列(wrap_text)の内容量に応じて、各データ行の高さを調整する ---
        # 列幅に上限を設けたことで折り返し行数が増えている可能性があるため、
        # 実際に使われた列幅をもとに折り返し後の行数を見積もり、行の高さに反映する。
        for row_cells in ws.iter_rows(min_row=2, max_row=ws.max_row):
            max_lines_in_row = 1

            for cell in row_cells:
                if cell.column in ExcelService._WRAP_TEXT_COLUMNS and cell.value is not None:
                    width = column_widths.get(cell.column, ExcelService._MAX_WRAP_COLUMN_WIDTH)
                    max_lines_in_row = max(
                        max_lines_in_row,
                        _estimate_wrapped_line_count(str(cell.value), width),
                    )

            # 1行(pt)あたり15pt + 余白5pt を目安に、最低でも20ptは確保する。
            row_index = row_cells[0].row
            assert row_index is not None

            ws.row_dimensions[row_index].height = max(
                20,
                max_lines_in_row * 15 + 5,
            )

        wb.save(str(output_path))
