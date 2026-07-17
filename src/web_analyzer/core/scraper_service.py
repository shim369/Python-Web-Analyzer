import logging
import threading

from web_analyzer.core.crawler import WebCrawler
from web_analyzer.core.evaluator import RenewalEvaluator
from web_analyzer.core.ssl_checker import SslChecker
from web_analyzer.models import ScrapingJob, SiteAssessment

# ログの設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(threadName)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


class SiteScraperService:
    """バックグラウンドでサイト調査を実行し、進捗率やステータスを管理するサービス。"""

    def __init__(self) -> None:
        self.ssl_checker = SslChecker()
        # 💡 [修正] crawlerの共通インスタンス保持を廃止し、スレッドごとに生成するようにします

        # ジョブや解析データをメモリ上で保持するキャッシュ
        self._jobs_cache: dict[str, ScrapingJob] = {}
        self._results_cache: dict[str, list[SiteAssessment]] = {}

        # 排他制御用ロック
        self._lock = threading.Lock()

    def get_job_progress(self, job_id: str) -> tuple[ScrapingJob | None, list[SiteAssessment], int, int]:
        """指定されたジョブの現在の進捗状況（進捗率、全件数、完了件数）を取得する。"""
        with self._lock:
            job = self._jobs_cache.get(job_id)
            assessments = self._results_cache.get(job_id, [])

            if not job or not assessments:
                return None, [], 0, 0

            # 処理完了件数をカウント（判定結果が空欄以外になっているものをカウント）
            completed_count = sum(1 for item in assessments if item.evaluation_result != "")
            total_count = len(assessments)

            return job, assessments, total_count, completed_count

    def start_background_job(self, job: ScrapingJob, assessments: list[SiteAssessment]) -> None:
        """非同期スレッドを立ち上げて、バックグラウンドでのスクレイピングタスクを開始する。"""
        with self._lock:
            self._jobs_cache[job.id] = job
            self._results_cache[job.id] = assessments

        logger.info(f"ジョブを開始します: JOB_ID={job.id}, 対象件数={len(assessments)}件")
        thread = threading.Thread(
            target=self._run_scraping_worker,
            args=(job.id,),
            name=f"Worker-{job.id}",
            daemon=True,
        )
        thread.start()

    def _run_scraping_worker(self, job_id: str) -> None:
        """バックグラウンドで1件ずつ逐次スクレイピング処理を行う実体メソッド。"""
        with self._lock:
            job = self._jobs_cache.get(job_id)
            assessments = self._results_cache.get(job_id)

        if not job or not assessments:
            logger.error(f"ジョブの起動に失敗しました: JOB_ID={job_id} がキャッシュに存在しません。")
            return

        self._update_job_status(job_id, "processing")
        evaluator = RenewalEvaluator(
            threshold_1=job.threshold_1,
            threshold_2=job.threshold_2,
            threshold_3=job.threshold_3,
        )

        try:
            for item in assessments:
                logger.info(f"[{job_id}] 解析開始: {item.domain_name}")

                # 初期値を安全に設定
                has_ssl_val: str = ""
                is_always_ssl_val: str = ""
                total_pages, max_depth, contact_fields, site_structure = (
                    0,
                    0,
                    "",
                    "取得失敗（接続エラーまたはタイムアウト）",
                )
                site_purpose = "コーポレートサイト"
                site_remarks = "サイトに接続できませんでした。サーバーが停止しているか、アクセスが拒否されています。"
                cms_name = "なし"

                # 1. SSL判定の実行 (修正: ◯/× -> あり/なし)
                try:
                    has_ssl, is_always_ssl = self.ssl_checker.check_ssl_status(item.domain_name)

                    if has_ssl is None or is_always_ssl is None:
                        has_ssl_val = ""
                        is_always_ssl_val = ""
                    else:
                        has_ssl_val = "あり" if has_ssl else "なし"
                        is_always_ssl_val = "◯" if is_always_ssl else "×"

                except Exception as e:
                    logger.warning(f"[{item.domain_name}] SSLチェック中にエラーが発生しました: {e}")
                    has_ssl_val = ""
                    is_always_ssl_val = ""

                # 2. クローラー巡回
                crawler = WebCrawler()
                try:
                    (
                        total_pages,
                        max_depth,
                        contact_fields,
                        site_structure,
                        site_purpose,
                        site_remarks,
                        cms_name,
                    ) = crawler.crawl_and_analyze(item.domain_name)
                except Exception as e:
                    logger.warning(f"[{item.domain_name}] クロール中に予期せぬエラーが発生しました: {e}")

                # 4. リニューアル評価判定 & 不可判定
                site_structure_lower = site_structure.lower()
                has_login = "login" in site_structure_lower or "signin" in site_structure_lower

                if total_pages == 0:
                    eval_result = "要確認"
                    rejection_reason = "接続不可またはアクセス拒否のため、判定を保留しました。"
                elif 1 <= total_pages <= 2:
                    eval_result = "要確認"
                    rejection_reason = f"クロールできたページ数が極端に少ないため判定を保留しました (取得数: {total_pages}ページ)。"
                else:
                    eval_result = evaluator.evaluate_rank(cms_name, total_pages)
                    if not contact_fields or contact_fields == "なし" or contact_fields.strip() == "":
                        rejection_reason = "問い合わせ入力項目が検出できませんでした（導線またはフォームの不足）。"
                    else:
                        rejection_reason = evaluator.compile_rejection_reason(total_pages, has_login)

                # スレッドセーフに結果を書き込み
                with self._lock:
                    item.has_ssl = has_ssl_val
                    item.is_always_ssl = is_always_ssl_val
                    item.total_pages = total_pages
                    item.max_depth = max_depth

                    if total_pages == 0:
                        item.contact_fields = "なし"
                    else:
                        item.contact_fields = contact_fields or "なし"

                    item.site_structure = site_structure
                    item.cms_name = cms_name
                    item.description = site_purpose
                    item.remarks = site_remarks
                    item.evaluation_result = eval_result
                    item.rejection_reason = rejection_reason

                logger.info(f"[{job_id}] 解析完了: {item.domain_name} -> 判定: {eval_result}")

            # すべての処理が正常終了
            logger.info(f"ジョブが正常終了しました: JOB_ID={job_id}")
            self._update_job_status(job_id, "completed")

        except Exception as e:
            logger.exception(f"[Fatal] ジョブ {job_id} の実行中に予期せぬ致命的なエラーが発生しました: {e}")
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
