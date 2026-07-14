import uuid
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment
from openpyxl.worksheet.worksheet import Worksheet

from web_analyzer.models import ScrapingJob, SiteAssessment


class ExcelService:
    """Excelファイルのパースおよび生成を担当するサービス。"""

    @staticmethod
    def import_excel(
        file_path: Path,
        operator_name: str,
        threshold_1: int = 10,
        threshold_2: int = 15,
        threshold_3: int = 20,
    ) -> tuple[ScrapingJob, list[SiteAssessment]]:
        """Excelからドメイン名(B列)を抽出し、パラメータを紐付けてモデルを生成する。"""
        job_id = str(uuid.uuid4())
        job = ScrapingJob(
            id=job_id,
            operator_name=operator_name,
            threshold_1=threshold_1,
            threshold_2=threshold_2,
            threshold_3=threshold_3,
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
            # 新仕様に基づき、B列（column=2）からドメインを取得
            cell_value = ws.cell(row=row, column=2).value

            # 万が一古いフォーマット（A列にしか値がない）のファイルが来ても動くようセーフティ配置
            if cell_value is None:
                cell_value = ws.cell(row=row, column=1).value

            if cell_value is None:
                continue

            domain = str(cell_value).strip()
            if not domain or domain == "サイト名":  # ヘッダーの重複混入防止
                continue

            assessment = SiteAssessment(
                id=str(uuid.uuid4()),
                job_id=job_id,
                date_str=current_date_str,  # A列用
                domain_name=domain,  # B列用
                operator_name=operator_name,  # O列用
            )
            assessments.append(assessment)

        return job, assessments

    @staticmethod
    def export_excel(assessments: list[SiteAssessment], output_path: Path) -> None:
        """指定された解析結果リストを新仕様(A〜O列)のフォーマットでExcel出力する。"""
        wb = Workbook()
        ws = wb.active
        assert isinstance(ws, Worksheet)
        ws.title = "調査結果"

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

        # データのマッピング書き込み
        for item in assessments:
            row_data = [
                item.date_str,  # A
                item.domain_name,  # B
                item.evaluation_result,  # C
                item.has_ssl,  # D
                item.is_always_ssl,  # E
                item.max_depth if item.max_depth is not None else "",  # F
                item.svcmd,  # G
                item.site_structure,  # H
                item.total_pages if item.total_pages is not None else "",  # I
                item.cms_name,  # J
                item.description,  # K
                item.contact_fields,  # L
                item.rejection_reason,  # M
                item.remarks,  # N
                item.operator_name,  # O
            ]
            ws.append(row_data)

            # 【追加】直前に追加したデータ行を取得し、全セルを上揃えにする
            current_row = ws.max_row
            for col_idx in range(1, len(row_data) + 1):
                cell = ws.cell(row=current_row, column=col_idx)
                cell.alignment = Alignment(
                    vertical="top",  # 縦位置を上揃えに設定
                    wrap_text=True,  # セル内での折り返し（自動改行）を有効化
                )

        wb.save(str(output_path))
