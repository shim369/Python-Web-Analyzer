"""ジョブ(ScrapingJob)と調査結果(SiteAssessment)をSQLiteに永続化するリポジトリ。

設計方針:
- アプリが Ctrl+C や強制終了で死んでも、次回起動時に矛盾なく状態を
  復元できるようにする。書き込みはすべてこのクラス経由で行い、
  SQLiteのトランザクションのアトミック性に守ってもらう。
- WALモードにすることで、書き込み中に強制終了されてもファイル自体が
  壊れることはない(コミットされていない変更は破棄されるだけ)。
- 複数のワーカースレッドから同時に呼ばれるため、書き込みは自前の
  threading.Lock で直列化する。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from web_analyzer.models import ScrapingJob, SiteAssessment

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("data") / "jobs.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    operator_name TEXT NOT NULL,
    page_threshold INTEGER NOT NULL DEFAULT 10,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assessments (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    date_str TEXT DEFAULT '',
    domain_name TEXT DEFAULT '',
    evaluation_result TEXT DEFAULT '',
    has_ssl TEXT DEFAULT '',
    is_always_ssl TEXT DEFAULT '',
    max_depth TEXT DEFAULT '',
    svcmd TEXT DEFAULT '',
    site_structure TEXT DEFAULT '',
    total_pages TEXT DEFAULT '',
    cms_name TEXT DEFAULT '',
    description TEXT DEFAULT '',
    contact_fields TEXT DEFAULT '',
    rejection_reason TEXT DEFAULT '',
    remarks TEXT DEFAULT '',
    operator_name TEXT DEFAULT '',
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);

CREATE INDEX IF NOT EXISTS idx_assessments_job_id ON assessments(job_id);
"""


def _to_str_or_empty(value: int | str | None) -> str:
    return "" if value is None else str(value)


def _to_int_or_none(value: str) -> int | str | None:
    """DBに文字列で保存しているmax_depth/total_pagesを元の型に戻す。

    "要確認" や "100以上" のような数値以外の文字列もそのまま保持する。
    """
    if value == "" or value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return value


class JobRepository:
    """ジョブと調査結果の永続化を担当するリポジトリ。"""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

        # 複数スレッドから使い回すため check_same_thread=False にし、
        # 書き込みはこのクラス内の Lock で直列化する。
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------------
    # ジョブ
    # ------------------------------------------------------------------

    def save_job(self, job: ScrapingJob) -> None:
        """ジョブを新規保存(または上書き保存)する。"""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO jobs (id, operator_name, page_threshold, status, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    operator_name=excluded.operator_name,
                    page_threshold=excluded.page_threshold,
                    status=excluded.status,
                    created_at=excluded.created_at
                """,
                (
                    job.id,
                    job.operator_name,
                    job.page_threshold,
                    job.status,
                    job.created_at.isoformat(),
                ),
            )
            self._conn.commit()

    def update_job_status(self, job_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status = ? WHERE id = ?",
                (status, job_id),
            )
            self._conn.commit()

    def get_job(self, job_id: str) -> ScrapingJob | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, operator_name, page_threshold, status, created_at FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()

        if row is None:
            return None

        return ScrapingJob(
            id=row[0],
            operator_name=row[1],
            page_threshold=row[2],
            status=row[3],
            created_at=datetime.fromisoformat(row[4]),
        )

    # ------------------------------------------------------------------
    # 調査結果(SiteAssessment)
    # ------------------------------------------------------------------

    def save_assessments(self, assessments: list[SiteAssessment]) -> None:
        """ジョブ開始時に、初期状態(未処理)の全行をまとめて保存する。"""
        with self._lock:
            self._conn.executemany(
                """
                INSERT INTO assessments (
                    id, job_id, date_str, domain_name, evaluation_result,
                    has_ssl, is_always_ssl, max_depth, svcmd, site_structure,
                    total_pages, cms_name, description, contact_fields,
                    rejection_reason, remarks, operator_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                [self._to_row(a) for a in assessments],
            )
            self._conn.commit()

    def update_assessment(self, item: SiteAssessment) -> None:
        """1ドメイン分の解析完了時に、その行だけを更新する。

        ワーカースレッドが1件処理し終えるたびに呼ぶ想定。これにより、
        ジョブが全件終わる前にプロセスが落ちても、そこまで完了した分は
        ディスクに残る。
        """
        with self._lock:
            self._conn.execute(
                """
                UPDATE assessments SET
                    evaluation_result = ?,
                    has_ssl = ?,
                    is_always_ssl = ?,
                    max_depth = ?,
                    site_structure = ?,
                    total_pages = ?,
                    cms_name = ?,
                    description = ?,
                    contact_fields = ?,
                    rejection_reason = ?,
                    remarks = ?
                WHERE id = ?
                """,
                (
                    item.evaluation_result,
                    item.has_ssl,
                    item.is_always_ssl,
                    _to_str_or_empty(item.max_depth),
                    item.site_structure,
                    _to_str_or_empty(item.total_pages),
                    item.cms_name,
                    item.description,
                    item.contact_fields,
                    item.rejection_reason,
                    item.remarks,
                    item.id,
                ),
            )
            self._conn.commit()

    def get_assessments(self, job_id: str) -> list[SiteAssessment]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, job_id, date_str, domain_name, evaluation_result,
                       has_ssl, is_always_ssl, max_depth, svcmd, site_structure,
                       total_pages, cms_name, description, contact_fields,
                       rejection_reason, remarks, operator_name
                FROM assessments WHERE job_id = ?
                """,
                (job_id,),
            ).fetchall()

        return [self._from_row(r) for r in rows]

    @staticmethod
    def _to_row(a: SiteAssessment) -> tuple[str, ...]:
        return (
            a.id,
            a.job_id,
            a.date_str,
            a.domain_name,
            a.evaluation_result,
            a.has_ssl,
            a.is_always_ssl,
            _to_str_or_empty(a.max_depth),
            a.svcmd,
            a.site_structure,
            _to_str_or_empty(a.total_pages),
            a.cms_name,
            a.description,
            a.contact_fields,
            a.rejection_reason,
            a.remarks,
            a.operator_name,
        )

    @staticmethod
    def _from_row(r: tuple[str, ...]) -> SiteAssessment:
        return SiteAssessment(
            id=r[0],
            job_id=r[1],
            date_str=r[2],
            domain_name=r[3],
            evaluation_result=r[4],
            has_ssl=r[5],
            is_always_ssl=r[6],
            max_depth=_to_int_or_none(r[7]),  # type: ignore[arg-type]
            svcmd=r[8],
            site_structure=r[9],
            total_pages=_to_int_or_none(r[10]),  # type: ignore[arg-type]
            cms_name=r[11],
            description=r[12],
            contact_fields=r[13],
            rejection_reason=r[14],
            remarks=r[15],
            operator_name=r[16],
        )

    # ------------------------------------------------------------------
    # 起動時の整合性チェック
    # ------------------------------------------------------------------

    def reconcile_interrupted_jobs(self) -> list[str]:
        """アプリ起動時に一度だけ呼び出す。

        前回 'processing' のまま残っている(=Ctrl+C等で異常終了した)
        ジョブを検出し、'interrupted' 状態に修正する。これをしないと、
        UIが「処理実行中...」の表示のまま永久に終わらないゾンビ状態になる。

        戻り値: 修正したjob_idのリスト
        """
        with self._lock:
            rows = self._conn.execute("SELECT id FROM jobs WHERE status = 'processing'").fetchall()
            job_ids = [r[0] for r in rows]

            if job_ids:
                self._conn.executemany(
                    "UPDATE jobs SET status = 'interrupted' WHERE id = ?",
                    [(jid,) for jid in job_ids],
                )
                self._conn.commit()

        if job_ids:
            logger.warning(f"起動時に中断されたジョブを検知し、状態を修正しました: {job_ids}")

        return job_ids
