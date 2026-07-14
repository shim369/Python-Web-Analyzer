import threading

from web_analyzer.core.crawler import WebCrawler
from web_analyzer.core.evaluator import RenewalEvaluator
from web_analyzer.core.ssl_checker import SslChecker
from web_analyzer.models import ScrapingJob, SiteAssessment


class SiteScraperService:
    """バックグラウンドでサイト調査を実行し、進捗率やステータスを管理するサービス。"""

    def __init__(self) -> None:
        self.ssl_checker = SslChecker()
        self.crawler = WebCrawler()

        # ジョブや解析データをメモリ上で保持するキャッシュ（Pythonモダン型ヒントに修正）
        self._jobs_cache: dict[str, ScrapingJob] = {}
        self._results_cache: dict[str, list[SiteAssessment]] = {}

        # 排他制御用ロック
        self._lock = threading.Lock()

    def get_job_progress(self, job_id: str) -> tuple[ScrapingJob | None, list[SiteAssessment], int, int]:
        """指定されたジョブの現在の進捗状況（進捗率、全件数、完了件数）を取得する。

        Returns:
            tuple: (ScrapingJob, SiteAssessmentリスト, 全件数, 完了件数)
        """
        with self._lock:
            job = self._jobs_cache.get(job_id)
            assessments = self._results_cache.get(job_id, [])

            if not job or not assessments:
                return None, [], 0, 0

            # 処理完了件数をカウント
            completed_count = sum(1 for item in assessments if item.has_ssl != "" or item.total_pages is not None)
            total_count = len(assessments)

            return job, assessments, total_count, completed_count

    def start_background_job(self, job: ScrapingJob, assessments: list[SiteAssessment]) -> None:
        """非同期スレッドを立ち上げて、バックグラウンドでのスクレイピングタスクを開始する。"""
        with self._lock:
            # メモリキャッシュへの初回登録
            self._jobs_cache[job.id] = job
            self._results_cache[job.id] = assessments

        # ワーカースレッドの生成と開始
        thread = threading.Thread(target=self._run_scraping_worker, args=(job.id,), daemon=True)
        thread.start()

    def _run_scraping_worker(self, job_id: str) -> None:
        """バックグラウンドで1件ずつ逐次スクレイピング処理を行う実体メソッド。"""
        with self._lock:
            job = self._jobs_cache.get(job_id)
            assessments = self._results_cache.get(job_id)

        if not job or not assessments:
            return

        # ジョブステータスを処理中に更新
        self._update_job_status(job_id, "processing")

        # 評価判定器を初期化
        evaluator = RenewalEvaluator(threshold_1=job.threshold_1, threshold_2=job.threshold_2, threshold_3=job.threshold_3)

        try:
            for item in assessments:
                # 1. SSL判定
                has_ssl, is_always_ssl = self.ssl_checker.check_ssl_status(item.domain_name)

                # 2. クローラー巡回
                total_pages, max_depth, contact_fields, site_structure = self.crawler.crawl_and_analyze(item.domain_name)

                # 3. リニューアル可否の動的評価
                has_login = "login" in site_structure.lower() or "signin" in site_structure.lower()
                cms_name = "WordPress" if "wp-content" in site_structure else ""

                eval_result = evaluator.evaluate_rank(cms_name, total_pages)
                rejection_reason = evaluator.compile_rejection_reason(total_pages, has_login)

                # スレッドセーフに各 assessment のプロパティを更新
                with self._lock:
                    item.has_ssl = "◯" if has_ssl else "×"
                    item.is_always_ssl = "◯" if is_always_ssl else "×"
                    item.total_pages = total_pages
                    item.max_depth = max_depth
                    item.contact_fields = contact_fields
                    item.site_structure = site_structure
                    item.cms_name = cms_name
                    item.evaluation_result = eval_result
                    item.rejection_reason = rejection_reason

            # すべての処理が正常終了
            self._update_job_status(job_id, "completed")

        except Exception as e:
            print(f"[Fatal] ジョブ {job_id} の実行中に致命的なエラーが発生しました: {e}")
            self._update_job_status(job_id, "failed")

    def _update_job_status(self, job_id: str, status: str) -> None:
        """スレッドセーフにジョブのステータスを書き換える内部ユーティリティ。"""
        with self._lock:
            current_job = self._jobs_cache.get(job_id)
            if current_job:
                updated_job = ScrapingJob(
                    id=current_job.id,
                    operator_name=current_job.operator_name,
                    threshold_1=current_job.threshold_1,
                    threshold_2=current_job.threshold_2,
                    threshold_3=current_job.threshold_3,
                    status=status,
                    created_at=current_job.created_at,
                )
                self._jobs_cache[job_id] = updated_job
