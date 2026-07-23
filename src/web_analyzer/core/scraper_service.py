import logging
import threading

from web_analyzer.core.crawler import WebCrawler
from web_analyzer.core.evaluator import RenewalEvaluator
from web_analyzer.core.ssl_checker import SslChecker
from web_analyzer.models import ScrapingJob, SiteAssessment

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
        self._jobs_cache: dict[str, ScrapingJob] = {}
        self._results_cache: dict[str, list[SiteAssessment]] = {}
        self._lock = threading.Lock()

    def get_job_progress(self, job_id: str) -> tuple[ScrapingJob | None, list[SiteAssessment], int, int]:
        """指定されたジョブの現在の進捗状況（進捗率、全件数、完了件数）を取得する。"""
        with self._lock:
            job = self._jobs_cache.get(job_id)
            assessments = self._results_cache.get(job_id, [])

            if not job or not assessments:
                return None, [], 0, 0

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

                has_ssl_val: str = ""
                is_always_ssl_val: str = ""

                # クローラーからの生の戻り値を受ける変数を定義
                total_pages_fetched: int | str = 0

                # max_depth に明示的に int | str 型のヒントを付与して初期化
                max_depth: int | str = 0
                contact_fields = ""
                site_structure = "取得失敗（接続エラーまたはタイムアウト）"

                site_purpose = ""
                cms_name = ""
                html_src = ""  # HTMLソース受け渡し用（必要に応じてクローラー側から取得可能な設計にあわせる）

                # 1. SSL判定の実行
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
                        total_pages_fetched,  # 定義した変数で安全に受け取る
                        max_depth,
                        contact_fields,
                        site_structure,
                        site_purpose,
                        html_src,  # 空白文字スキップのプレースホルダーから実際のソース受け取りへ変更
                        cms_name,
                    ) = crawler.crawl_and_analyze(item.domain_name)
                except Exception as e:
                    logger.warning(f"[{item.domain_name}] クロール中に予期せぬエラーが発生しました: {e}")

                # 文字列判定と数値へのクリーンアップ処理
                if total_pages_fetched == "100ページ以上":
                    total_pages_int = 100
                    total_pages_display: int | str = "100~"
                else:
                    total_pages_int = int(total_pages_fetched)
                    total_pages_display = total_pages_int

                # 4. リニューアル評価判定 & 不可判定
                site_structure_lower = site_structure.lower()
                has_login = "login" in site_structure_lower or "signin" in site_structure_lower

                if total_pages_int == 0:
                    eval_result = "要確認"
                    rejection_reason = "接続不可またはアクセス拒否のため、判定を保留しました。"
                elif 1 <= total_pages_int <= 2:
                    eval_result = "要確認"
                    rejection_reason = f"クロールできたページ数が極端に少ないため判定を保留しました (取得数: {total_pages_int}ページ)。"
                else:
                    # site_purpose も含めてすべての判定材料を evaluator に安全に渡す
                    rejection_reason = evaluator.compile_rejection_reason(
                        total_pages=total_pages_int,
                        has_login=has_login,
                        site_purpose=site_purpose,
                        html_src=html_src,
                    )

                    # evaluate_rank から cms_name を削除し、名前付き引数で同期させる
                    # (物件検索や各種リッチコンテンツによる × 判定はすべて evaluator 内で自動処理されます)
                    eval_result = evaluator.evaluate_rank(
                        total_pages=total_pages_int,
                        has_login=has_login,
                        site_purpose=site_purpose,
                        html_src=html_src,
                    )

                # スレッドセーフに結果を書き込み
                with self._lock:
                    item.has_ssl = has_ssl_val
                    item.is_always_ssl = is_always_ssl_val

                    # Mypyのエラー箇所: モデル側の型指定(int | None)に合わせるため
                    # 「100ページ以上」だった場合は数値の上限である 100 を明示的に代入します
                    item.total_pages = total_pages_display  # type: ignore
                    item.max_depth = max_depth  # type: ignore

                    if total_pages_int == 0:
                        item.contact_fields = ""
                    else:
                        item.contact_fields = contact_fields or ""

                    item.site_structure = site_structure
                    item.cms_name = cms_name
                    item.description = site_purpose
                    item.remarks = ""
                    item.evaluation_result = eval_result
                    item.rejection_reason = rejection_reason

                logger.info(f"[{job_id}] 解析完了: {item.domain_name} -> 判定: {eval_result}")

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
