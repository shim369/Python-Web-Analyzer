import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from web_analyzer.core.crawler import WebCrawler
from web_analyzer.core.evaluator import RenewalEvaluator
from web_analyzer.core.job_repository import JobRepository
from web_analyzer.core.ssl_checker import SslChecker
from web_analyzer.models import ScrapingJob, SiteAssessment

logger = logging.getLogger(__name__)


class SiteScraperService:
    """バックグラウンドでサイト調査を実行し、進捗率やステータスを管理するサービス。

    v2: ThreadPoolExecutorによるドメイン単位の並列化に対応。
    従来は1ドメインずつ逐次処理していたため、対象件数が多いと総処理時間が
    線形に増加していたが、複数ドメインを同時に処理することでスループットを改善する。
    """

    # 同時に処理するドメイン数の上限。
    # render_js=True の場合、各ワーカーが個別にPlaywright(Chromiumヘッドレス)を
    # 起動するため、上げすぎるとメモリ・CPU負荷が急増して逆に遅くなる/落ちる恐れがある。
    # 5並列程度は一般的なマシンでも安定して動作する経験則値。
    DEFAULT_MAX_WORKERS = 5

    def __init__(
        self,
        max_workers: int = DEFAULT_MAX_WORKERS,
        repository: JobRepository | None = None,
    ) -> None:
        self.ssl_checker = SslChecker()
        self._jobs_cache: dict[str, ScrapingJob] = {}
        self._results_cache: dict[str, list[SiteAssessment]] = {}
        self._lock = threading.Lock()
        self.max_workers = max_workers
        # 永続化層。渡されなければ自前で1つ生成する(テスト等での差し替えを想定)。
        self.repository = repository or JobRepository()

    def get_job_progress(self, job_id: str) -> tuple[ScrapingJob | None, list[SiteAssessment], int, int]:
        """指定されたジョブの現在の進捗状況（進捗率、全件数、完了件数）を取得する。

        メモリキャッシュに無い場合(プロセス再起動直後や、別セッションから
        初めて参照された場合など)は、DBから復元してキャッシュに載せてから返す。
        """
        with self._lock:
            job = self._jobs_cache.get(job_id)
            assessments = self._results_cache.get(job_id, [])

        if job is None:
            job = self.repository.get_job(job_id)
            if job is not None:
                assessments = self.repository.get_assessments(job_id)
                with self._lock:
                    self._jobs_cache[job_id] = job
                    self._results_cache[job_id] = assessments

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

        # DBにも初期状態を書き込んでおく。ここで落ちてもUI側は「processing」の
        # ままDBに残るだけなので、次回起動時のreconcile_interrupted_jobsで拾える。
        self.repository.save_job(job)
        self.repository.save_assessments(assessments)

        logger.info(f"ジョブを開始します: JOB_ID={job.id}, 対象件数={len(assessments)}件, 並列数={self.max_workers}")
        thread = threading.Thread(
            target=self._run_scraping_worker,
            args=(job.id,),
            name=f"Worker-{job.id}",
            daemon=True,
        )
        thread.start()

    def _run_scraping_worker(self, job_id: str) -> None:
        """バックグラウンドでジョブ全体を統括する実体メソッド。

        ジョブ内の各ドメイン(assessment)の処理は ThreadPoolExecutor で
        self.max_workers 件まで同時実行される。
        """
        with self._lock:
            job = self._jobs_cache.get(job_id)
            assessments = self._results_cache.get(job_id)

        if not job or not assessments:
            logger.error(f"ジョブの起動に失敗しました: JOB_ID={job_id} がキャッシュに存在しません。")
            return

        self._update_job_status(job_id, "processing")

        try:
            with ThreadPoolExecutor(
                max_workers=self.max_workers,
                thread_name_prefix=f"Worker-{job_id}",
            ) as executor:
                future_to_item = {executor.submit(self._process_single_assessment, job_id, item, job.page_threshold): item for item in assessments}

                for future in as_completed(future_to_item):
                    item = future_to_item[future]
                    try:
                        future.result()
                    except Exception as e:
                        # 1ドメインの処理失敗でジョブ全体を巻き添えにしないよう、
                        # ここで握りつぶして「要確認」として結果に残し、他のドメインの
                        # 処理・ジョブ全体の完了は継続させる。
                        logger.exception(f"[{job_id}] {item.domain_name} の処理中に予期せぬエラーが発生しました: {e}")
                        with self._lock:
                            item.evaluation_result = "要確認"
                            item.rejection_reason = f"処理中に予期せぬエラーが発生しました: {e}"

            logger.info(f"ジョブが正常終了しました: JOB_ID={job_id}")
            self._update_job_status(job_id, "completed")

        except Exception as e:
            # ThreadPoolExecutor自体の起動失敗など、個別ドメインの範囲外で起きた
            # 致命的なエラーのみここに到達する。
            logger.exception(f"[Fatal] ジョブ {job_id} の実行中に予期せぬ致命的なエラーが発生しました: {e}")
            self._update_job_status(job_id, "failed")

    def _process_single_assessment(self, job_id: str, item: SiteAssessment, page_threshold: int) -> None:
        """1ドメイン分の解析を行い、結果をitemに書き込む。

        並列実行されるワーカースレッドから呼び出される想定のため、
        RenewalEvaluator・WebCrawlerはいずれもこのメソッド内でローカルに
        生成し、スレッド間で状態を共有しない。SslChecker(self.ssl_checker)は
        呼び出しごとに新規Sessionを生成する作りのため、インスタンスを
        共有しても問題ない。
        """
        evaluator = RenewalEvaluator()
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
        # クロールが内部リンクを発見し尽くして自然に終了したか(True)、
        # タイムアウト等で途中終了したか(False)。例外発生時は実際には不明なため、
        # 保守的にFalse(＝クロール失敗とみなし「要確認」判定を維持)扱いにしておく。
        queue_exhausted_raw: bool = False

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
        # render_js=True 時、トップページのJSフレームワーク検知でPlaywrightレンダリングが
        # 走ると1回あたり15秒前後かかることがあるため、デフォルトのtimeout=30.0のままだと
        # 2ページ目以降を巡回する前に全体タイムアウトへ到達してしまう。
        # ここで明示的に余裕を持たせる。
        #
        # timeout=90.0だと、ニュース記事やお知らせ等の個別ページを大量に持つ
        # サイト(daitoh-ec.co.jp, deli-j.j-tr.jp等)で、90秒以内に辿り着けた分しか
        # カウントされず、実際のページ数を大きく下回ってしまっていた。
        # クローラー側には「visited件数が100件に達したら即座に打ち切る」という
        # 上限が既に実装されており、この上限がある限りページ数自体の暴走(無限に
        # 増え続けること)は起きない。そのため全体タイムアウトは「100ページに
        # 到達するまで十分な時間」を目安に300秒へ引き上げ、記事系ページが
        # 多いサイトでも実態に近い件数(上限に達した場合は「100ページ以上」表示)
        # まで巡回できるようにする。
        crawler = WebCrawler(render_js=True, timeout=300.0, page_timeout=8.0)
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
                queue_exhausted_raw,
            ) = crawler.crawl_and_analyze(item.domain_name)
        except Exception as e:
            logger.warning(f"[{item.domain_name}] クロール中に予期せぬエラーが発生しました: {e}")

        has_attachment = bool(has_attachment_raw)
        has_login = bool(has_login_raw)
        has_basic_auth = bool(has_basic_auth_raw)
        has_multilang = bool(has_multilang_raw)
        blocked_reason = str(blocked_reason_raw or "")
        redirect_target_url = str(redirect_target_url_raw or "")
        queue_exhausted = bool(queue_exhausted_raw)

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
                page_threshold=page_threshold,
                queue_exhausted=queue_exhausted,
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

        # 1件処理し終えるたびにDBへ反映する。ジョブ全体が終わる前に
        # プロセスが落ちても、ここまで完了した分の結果はディスクに残る。
        self.repository.update_assessment(item)

        logger.info(f"[{job_id}] 解析完了: {item.domain_name} -> 判定: {eval_result}")

    def _update_job_status(self, job_id: str, status: str) -> None:
        """スレッドセーフにジョブのステータスを書き換える内部ユーティリティ。"""
        with self._lock:
            current_job = self._jobs_cache.get(job_id)
            if current_job:
                updated_job = ScrapingJob(
                    id=current_job.id,
                    operator_name=current_job.operator_name,
                    page_threshold=current_job.page_threshold,
                    status=status,
                    created_at=current_job.created_at,
                )
                self._jobs_cache[job_id] = updated_job

        # メモリキャッシュと同様に、DB側のステータスも更新する。
        self.repository.update_job_status(job_id, status)
