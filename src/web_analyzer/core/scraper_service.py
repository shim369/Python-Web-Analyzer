import logging
import threading

from web_analyzer.core.crawler import WebCrawler
from web_analyzer.core.evaluator import RenewalEvaluator
from web_analyzer.core.ssl_checker import SslChecker
from web_analyzer.models import ScrapingJob, SiteAssessment

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
        evaluator = RenewalEvaluator()

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
                html_src = ""
                has_attachment_raw: bool = False
                has_login_raw: bool = False
                has_basic_auth_raw: bool = False
                has_multilang_raw: bool = False
                blocked_reason_raw: str = ""
                redirect_target_url_raw: str = ""

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
                crawler = WebCrawler(render_js=True)
                try:
                    (
                        total_pages_fetched,
                        max_depth,
                        contact_fields,
                        site_structure,
                        site_purpose,
                        html_src,
                        cms_name,
                        has_attachment_raw,
                        has_login_raw,
                        has_basic_auth_raw,
                        has_multilang_raw,
                        blocked_reason_raw,
                        redirect_target_url_raw,
                    ) = crawler.crawl_and_analyze(item.domain_name)
                except Exception as e:
                    logger.warning(f"[{item.domain_name}] クロール中に予期せぬエラーが発生しました: {e}")

                has_attachment = bool(has_attachment_raw)
                has_login = bool(has_login_raw)
                has_basic_auth = bool(has_basic_auth_raw)
                has_multilang = bool(has_multilang_raw)
                blocked_reason = str(blocked_reason_raw or "")
                redirect_target_url = str(redirect_target_url_raw or "")

                # 文字列判定と数値へのクリーンアップ処理
                if total_pages_fetched == "100ページ以上":
                    total_pages_int = 100
                    total_pages_display: int | str = "100以上"
                else:
                    total_pages_int = int(total_pages_fetched)
                    total_pages_display = total_pages_int

                remarks_value = ""

                if redirect_target_url:
                    # meta refresh / JSタイマー等により、数秒後に別ドメインへ自動リダイレクトされる
                    # 「移転案内ページ」だった場合。既にリニューアル・移転済みとみなし、
                    # 通常の判定ロジックは通さず固定の結果を採用する。
                    eval_result, rejection_reason = "×", "すでにリニューアル済のため"
                    remarks_value = f"移転先：{redirect_target_url}"
                elif blocked_reason:
                    # Fortinet等のネットワーク機器によるSSL証明書エラー/ブロックページを取得した場合、
                    # 他の項目は実サイトの内容を反映していないため、通常の判定ロジックを通さず
                    # 「要確認」+ 具体的な理由をそのままM列に出力する。
                    eval_result, rejection_reason = "要確認", blocked_reason
                else:
                    eval_result, rejection_reason = evaluator.decide(
                        total_pages=total_pages_int,
                        max_depth=max_depth,
                        has_login=has_login,
                        has_attachment=has_attachment,
                        has_basic_auth=has_basic_auth,
                        has_multilang=has_multilang,
                        html_src=html_src,
                        page_threshold=job.page_threshold,
                    )

                # スレッドセーフに結果を書き込み
                with self._lock:
                    item.has_ssl = has_ssl_val
                    item.is_always_ssl = is_always_ssl_val

                    item.total_pages = total_pages_display  # type: ignore
                    item.max_depth = max_depth  # type: ignore

                    if total_pages_int == 0:
                        item.contact_fields = ""
                    else:
                        item.contact_fields = contact_fields or ""

                    item.site_structure = site_structure
                    item.cms_name = cms_name
                    item.description = site_purpose
                    item.remarks = remarks_value
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
                # 【修正ポイント】旧 threshold_1~3 を一掃し、page_threshold に準拠させる
                updated_job = ScrapingJob(
                    id=current_job.id,
                    operator_name=current_job.operator_name,
                    page_threshold=current_job.page_threshold,
                    status=status,
                    created_at=current_job.created_at,
                )
                self._jobs_cache[job_id] = updated_job
