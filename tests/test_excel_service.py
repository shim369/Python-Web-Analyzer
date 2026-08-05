from pathlib import Path

from web_analyzer.core.excel_service import ExcelService
from web_analyzer.models import SiteAssessment


def test_excel_export_and_import(tmp_path: Path) -> None:
    test_file = tmp_path / "test_io.xlsx"

    # job_id を追加してモデルの定義に完全準拠させる
    mock_data = [
        SiteAssessment(
            id="1",
            job_id="job_test_123",  # ここを追加！
            domain_name="example.com",
            operator_name="山田 太郎",
            has_ssl="◯",
            is_always_ssl="◯",
            total_pages=5,
            max_depth=2,
            contact_fields="名前,メールアドレス",
            site_structure="Top -> Contact",
            cms_name="",
            evaluation_result="◎",
            rejection_reason="",
        )
    ]

    # 1. エクスポートを実行
    ExcelService.export_excel(mock_data, test_file)
    assert test_file.exists()

    # 2. インポートを実行して検証
    _, imported_data = ExcelService.import_excel(test_file, operator_name="山田 太郎")

    assert len(imported_data) == 1
    assert imported_data[0].domain_name == "example.com"
