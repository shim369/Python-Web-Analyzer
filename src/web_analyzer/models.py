from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ScrapingJob:
    """一括処理ジョブを管理するドメインモデル。"""

    id: str
    operator_name: str
    threshold_1: int = 10
    threshold_2: int = 15
    threshold_3: int = 20
    status: str = "pending"  # pending, processing, completed, failed
    created_at: datetime = datetime.now()


@dataclass
class SiteAssessment:
    """Excelの1行分の解析・調査結果を保持するモデル。

    データの更新が発生するため、このモデルは可変（mutable）とします。
    """

    id: str
    job_id: str
    date_str: str = ""  # A列 (例: "7/14")
    domain_name: str = ""  # B列
    evaluation_result: str = ""  # C列
    has_ssl: str = ""  # D列
    is_always_ssl: str = ""  # E列
    max_depth: int | None = None  # F列
    svcmd: str = ""  # G列 (常に空欄)
    site_structure: str = ""  # H列
    total_pages: int | None = None  # I列
    cms_name: str = ""  # J列
    description: str = ""  # K列
    contact_fields: str = ""  # L列
    rejection_reason: str = ""  # M列
    remarks: str = ""  # N列
    operator_name: str = ""  # O列
