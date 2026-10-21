import sqlite3
from pathlib import Path

from web_analyzer.core.job_repository import JobRepository
from web_analyzer.models import ScrapingJob, SiteAssessment


def test_adds_contact_uncertain_column_to_existing_db(tmp_path: Path) -> None:
    """列を追加する前に作ったDBでも、起動時に列を足して読み書きできる。"""
    db_path = tmp_path / "jobs.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE jobs (id TEXT PRIMARY KEY, operator_name TEXT NOT NULL, page_threshold INTEGER NOT NULL DEFAULT 10,
                           status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL);
        CREATE TABLE assessments (
            id TEXT PRIMARY KEY, job_id TEXT NOT NULL, date_str TEXT DEFAULT '', domain_name TEXT DEFAULT '',
            evaluation_result TEXT DEFAULT '', has_ssl TEXT DEFAULT '', is_always_ssl TEXT DEFAULT '', max_depth TEXT DEFAULT '',
            svcmd TEXT DEFAULT '', site_structure TEXT DEFAULT '', total_pages TEXT DEFAULT '', cms_name TEXT DEFAULT '',
            description TEXT DEFAULT '', contact_fields TEXT DEFAULT '', rejection_reason TEXT DEFAULT '', remarks TEXT DEFAULT '',
            operator_name TEXT DEFAULT '', FOREIGN KEY (job_id) REFERENCES jobs(id));
        """
    )
    conn.close()

    repo = JobRepository(db_path)
    job = ScrapingJob(id="job1", operator_name="担当者")
    repo.save_job(job)
    item = SiteAssessment(id="a1", job_id="job1", domain_name="a.example")
    repo.save_assessments([item])
    assert repo.get_assessments("job1")[0].contact_uncertain is False

    item.contact_fields = "問い合わせフォームなし"
    item.contact_uncertain = True
    repo.update_assessment(item)
    loaded = repo.get_assessments("job1")[0]
    assert (loaded.contact_fields, loaded.contact_uncertain) == ("問い合わせフォームなし", True)
