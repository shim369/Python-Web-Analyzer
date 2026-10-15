import logging
import os
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

from web_analyzer.core.description_enricher import DescriptionEnricher
from web_analyzer.core.excel_service import ExcelService
from web_analyzer.core.job_repository import JobRepository
from web_analyzer.core.scraper_service import SiteScraperService
from web_analyzer.core.site_summarizer import SiteSummarizer
from web_analyzer.models import ScrapingJob
from web_analyzer.utils.logger import setup_logger

# プロジェクトルートからの相対パスを指定
ICON_PATH = os.path.join("docs", "images", "cursor_icon.ico")

# 1. ページ設定を最初に行う
# favicon引数にアイコンファイルのパスを渡します
st.set_page_config(
    page_title="Web Site Analyzer",  # ブラウザのタブに表示されるタイトル
    page_icon=ICON_PATH,  # ここでアイコンを指定
    layout="wide",
    initial_sidebar_state="expanded",
)

logger = logging.getLogger(__name__)


@st.cache_resource(show_spinner=False)
def get_scraper_service() -> SiteScraperService:
    """SiteScraperServiceをプロセス全体で1つだけ生成し、全セッションで共有する。

    以前は st.session_state に格納していたため、ブラウザが完全にリロード
    されて新しいセッションが作られるたびに、ジョブの進捗・結果が入った
    インメモリキャッシュごと失われていた。st.cache_resource はサーバー
    プロセスが生きている限り同じインスタンスを使い回すため、リロードされても
    (サーバー自体は落ちていない限り)ジョブの状態を保持できる。

    また、ここでアプリ起動時に一度だけ、前回 Ctrl+C 等で中断されたまま
    'processing' で残っているジョブを検出し、'interrupted' 状態に修正する。
    """
    repository = JobRepository()
    interrupted_job_ids = repository.reconcile_interrupted_jobs()
    if interrupted_job_ids:
        logger.warning(f"前回異常終了した中断ジョブを検知しました: {interrupted_job_ids}")
    return SiteScraperService(repository=repository)


def resolve_gemini_api_key() -> str:
    """GeminiのAPIキーを .streamlit/secrets.toml → 環境変数 の順に探す。

    Windowsの setx で設定した環境変数は、設定より前から開いていたターミナル
    (VS Codeの内蔵ターミナルはVS Code自体を再起動するまで)から起動したアプリには
    反映されない。起動方法に左右されずに設定できるよう、secrets.toml も見る。
    """
    try:
        key = str(st.secrets.get("GEMINI_API_KEY", ""))
    except FileNotFoundError:  # secrets.toml が無い場合(StreamlitSecretNotFoundError)
        key = ""
    return (key or os.environ.get("GEMINI_API_KEY", "")).strip()


@st.cache_resource(show_spinner=False)
def get_description_enricher(api_key: str) -> DescriptionEnricher:
    """K列補完の処理とジョブ状態を、調査サービスと同様にプロセス全体で共有する。

    APIキーを引数に取ることで、キーを後から設定・変更した場合は
    キャッシュ済みの(キー無しの)インスタンスではなく新しいものが作られる。
    """
    return DescriptionEnricher(summarizer=SiteSummarizer(api_key=api_key))


MODE_SCRAPE = "サイト調査"
MODE_ENRICH = "用途(K列)の補完"


def render_enrichment_page() -> None:
    """完成した調査結果Excelに対して、K列「用途」をGeminiの要約で補完する画面。"""
    enricher = get_description_enricher(resolve_gemini_api_key())

    st.markdown('<div class="main-title">用途(K列)の補完</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="sub-title">調査結果Excelのうち、K列「用途」が社名だけなど内容の分かりにくい行について、サイトを取り直してGeminiで100文字前後のサイト概要に要約し、K列を書き換えます。</div>',
        unsafe_allow_html=True,
    )

    if not enricher.summarizer.enabled:
        st.error("GeminiのAPIキーが見つかりません。.streamlit/secrets.toml に GEMINI_API_KEY を記載するか、環境変数 GEMINI_API_KEY を設定してください（環境変数の場合は、設定後に開き直したターミナルからアプリを起動する必要があります）。")
        return

    if "enrich_job_id" not in st.session_state:
        st.session_state.enrich_job_id = st.query_params.get("enrich_job_id")

    enrich_job_id = st.session_state.enrich_job_id
    job = enricher.get_job(enrich_job_id) if enrich_job_id else None

    def clear_enrich_job() -> None:
        st.session_state.enrich_job_id = None
        if "enrich_job_id" in st.query_params:
            del st.query_params["enrich_job_id"]
        st.rerun()

    if job is None:
        uploaded = st.file_uploader(
            "調査結果Excel（このツールで出力したファイル）を選択、またはドラッグ＆ドロップしてください",
            type=["xlsx"],
            key=f"enrich_uploader_{st.session_state.get('enrich_uploader_key', 0)}",
        )
        force_all = st.checkbox(
            "全行を要約し直す（文体をそろえたい場合）",
            value=False,
            help=f"オフの場合は、K列が空欄・{DescriptionEnricher.MIN_DESCRIPTION_CHARS}文字未満・社名やキーワードを区切り記号で並べただけの行のみ要約します。",
        )
        if uploaded and st.button("K列の補完を開始する", use_container_width=True, type="primary"):
            # 同名ファイルを同時・連続で処理しても衝突しないよう、実行ごとに専用フォルダを切る
            work_dir = Path("temp") / f"enrich_{datetime.now().strftime('%Y%m%d%H%M%S_%f')}"
            work_dir.mkdir(parents=True, exist_ok=True)
            input_path = work_dir / "input.xlsx"
            input_path.write_bytes(uploaded.getbuffer())
            # 補完済みファイルに再度かけた場合に「_用途補完_用途補完」と重ならないようにする
            base_name = Path(uploaded.name).stem.removesuffix("_用途補完")
            output_path = work_dir / f"{base_name}_用途補完.xlsx"

            new_job = enricher.start_background_job(input_path, output_path, force_all=force_all)
            st.session_state.enrich_job_id = new_job.id
            st.query_params["enrich_job_id"] = new_job.id
            st.session_state.enrich_uploader_key = st.session_state.get("enrich_uploader_key", 0) + 1
            st.rerun()
        return

    st.markdown("---")
    percent = int(job.done / job.total * 100) if job.total else 0
    st.progress(percent if job.status == "processing" else 100)

    if job.status == "processing":
        st.info(f"要約中... ({job.done}/{job.total} 件完了)")
        time.sleep(1.5)
        st.rerun()

    elif job.status == "completed" and job.result and job.result.output_path:
        result = job.result
        result_path: Path = job.result.output_path
        st.success(f"K列の補完が完了しました。（全 {result.total_rows}件中、対象 {result.target_rows}件 / 要約 {result.summarized}件）")
        if result.failed_domains:
            st.warning(f"{len(result.failed_domains)}件は要約できなかったため、元の値のままです（サイトに接続できない、またはGeminiの利用上限に達した可能性があります）。出力したファイルにもう一度かけると、残りの行だけが対象になります。")
            with st.expander("要約できなかったサイト"):
                st.write("\n".join(f"- {d}" for d in result.failed_domains))

        d_col1, d_col2 = st.columns([2, 1])
        with d_col1:
            st.download_button(
                label="補完済みExcelをダウンロード",
                data=result_path.read_bytes(),
                file_name=result_path.name,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
                type="primary",
            )
        with d_col2:
            if st.button("結果表示をクリア", use_container_width=True, key="clear_btn_enrich", type="secondary"):
                clear_enrich_job()

    else:
        st.error(f"K列の補完中にエラーが発生しました: {job.error}")
        if st.button("画面をクリアしてやり直す", key="clear_btn_enrich_failed", type="secondary"):
            clear_enrich_job()


# UIスタイリング
st.markdown(
    """
    <style>
        html, body, [class*="css"] {
            font-family: 'Yu Gothic', 'Hiragino Kaku Gothic ProN', sans-serif;
        }
        .main-title {
            color: #1d78c9;
            font-size: 2.0rem;
            font-weight: 700;
            margin-bottom: 0.5rem;
            border-bottom: 2px solid #1F4E78;
            padding-bottom: 0.5rem;
        }
        .sub-title {
            font-size: 0.95rem;
            margin-bottom: 2rem;
        }
        .stButton > button {
            background-color: #1F4E78 !important;
            color: white !important;
            border-radius: 4px !important;
            border: none !important;
            padding: 0.6rem 2rem !important;
            font-weight: bold !important;
            transition: all 0.2s ease;
        }
        .stButton > button:hover {
            background-color: #2c6aa3 !important;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }
        .metric-card {
            background-color: #ffffff;
            border: 1px solid #e2e8f0;
            border-radius: 6px;
            padding: 1.2rem;
            text-align: center;
            box-shadow: 0 1px 3px rgba(0,0,0,0.02);
        }
        /* メインボタン (濃い青)：調査開始・ダウンロード用 */
        .stButton > button[kind="primary"],
        div[data-testid="stDownloadButton"] > button {
            background-color: #1F4E78 !important;
            color: white !important;
            border-radius: 4px !important;
            border: none !important;
            padding: 0.6rem 2rem !important;
            font-weight: bold !important;
            transition: all 0.2s ease;
        }
        .stButton > button[kind="primary"]:hover,
        div[data-testid="stDownloadButton"] > button:hover {
            background-color: #2c6aa3 !important;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }

        /* サブボタン (白地＋青枠)：クリア・リセット用 */
        .stButton > button[kind="secondary"] {
            background-color: #ffffff !important;
            color: #1F4E78 !important;
            border-radius: 4px !important;
            border: 1px solid #1F4E78 !important;
            padding: 0.6rem 2rem !important;
            font-weight: bold !important;
            transition: all 0.2s ease;
        }
        .stButton > button[kind="secondary"]:hover {
            background-color: #f0f4f8 !important;
            border-color: #2c6aa3 !important;
            color: #2c6aa3 !important;
        }
    </style>
    """,
    unsafe_allow_html=True,
)

# 2. 初期化とサービス生成
if "logger_initialized" not in st.session_state:
    setup_logger()
    st.session_state.logger_initialized = True

# プロセス全体で共有される永続化対応のサービス(セッションをまたいで生存する)
scraper_service: SiteScraperService = get_scraper_service()

if "current_job_id" not in st.session_state:
    # ブラウザが完全にリロードされて新しいセッションになった場合でも、
    # URLのクエリパラメータに job_id が残っていればそこから復元する。
    st.session_state.current_job_id = st.query_params.get("job_id")

# 3. サイドバーの設定
with st.sidebar:
    mode = st.radio("機能", [MODE_SCRAPE, MODE_ENRICH], key="app_mode")
    st.markdown("---")

    st.markdown("### 解析設定")
    st.write("担当者名を設定してください。")

    operator_name = st.text_input("担当者名", value="", placeholder="担当者名を入力")

    st.markdown("---")
    st.markdown("#### 判定基準（自動一括判定）")
    st.markdown(
        """
        以下のいずれかに該当する場合は **×** と判定されます。

        **基本構成**
        * 11ページ以上のサイト
        * サイト構成が3階層以上
        * ログイン / マイページ
        * ベーシック認証
        * 多言語対応

        **問い合わせフォーム**
        * 画像認証（Captcha）
        * フォームに添付機能

        **専用機能**
        * カレンダー
        * サイト内検索・絞り込み検索
        * チャットボット導入

        **UI上の演出**
        * 動画（videoタグ / YouTube・Vimeo埋め込み）
        * フローティング機能
        * アコーディオン
        * タブ切り替え
        * モーダルウィンドウ
        * 画像ギャラリー
        * アニメーション
        """
    )

# K列補完はサイト調査とは独立した画面として表示する(調査ジョブはその間もバックグラウンドで継続する)
if mode == MODE_ENRICH:
    render_enrichment_page()
    st.stop()

# 4. メインコンテンツエリア
st.markdown('<div class="main-title">Web Site Analyzer</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="sub-title">指定されたExcelリストから、複数ドメインのSSL対応状況、サイト構成、総ページ数、リッチコンテンツ等を自動調査・評価します。</div>',
    unsafe_allow_html=True,
)

uploaded_file = st.file_uploader(
    "インポート用Excelファイル（B列にドメイン名が配置されたシート）を選択、またはドラッグ＆ドロップしてください",
    type=["xlsx"],
    key=f"file_uploader_{st.session_state.get('uploader_key', 0)}",
)

# アップローダーの値はウィジェットが生きている間ずっと保持されるため、
# 何もガードしないと「処理中(processing)」のポーリングによる st.rerun() の
# たびに、このブロックが毎回丸ごと再実行されてしまい、
#   - 一時ファイルへの書き込み
#   - Excelの再パース
# が1.5秒おきに繰り返されて temp/ に import_*.xlsx が積み上がる原因になっていた。
# 「同じファイルを既に読み込み済みか」を (ファイル名, サイズ) の組で覚えておき、
# 本当に新しいファイルが選択されたときだけ読み込み処理を行うようにする。
current_file_fingerprint = (uploaded_file.name, uploaded_file.size) if uploaded_file else None

if uploaded_file and current_file_fingerprint != st.session_state.get("imported_file_fingerprint"):
    temp_dir = Path("temp")
    temp_dir.mkdir(exist_ok=True)

    # タイムスタンプをつけてファイル名の競合を防ぐ
    unique_filename = f"import_{datetime.now().strftime('%Y%m%d%H%M%S_%f')}.xlsx"
    input_path = temp_dir / unique_filename

    with open(input_path, "wb") as f:
        f.write(uploaded_file.getbuffer())

    try:
        job, assessments = ExcelService.import_excel(file_path=input_path, operator_name=operator_name if operator_name.strip() else "未指定")
        # パース結果をセッションに保持しておき、ボタン押下前の再実行(例:担当者名の
        # 入力による再描画)ではファイルの再読み込みが起きないようにする。
        st.session_state.imported_file_fingerprint = current_file_fingerprint
        st.session_state.pending_import = (job, assessments)
    except Exception as e:
        st.session_state.imported_file_fingerprint = current_file_fingerprint
        st.session_state.pending_import = None
        st.error(f"ファイルのインポート処理中にエラーが発生しました: {e}")

if st.session_state.get("pending_import"):
    job, assessments = st.session_state.pending_import

    st.success(f"ファイルを正常に読み込みました。 (対象ドメイン数: {len(assessments)}件)")

    if st.button("調査を開始する", use_container_width=True):
        if not operator_name.strip():
            st.error("調査を開始するには、サイドバーから「担当者名」を入力してください。")
        else:
            updated_job = ScrapingJob(
                id=job.id,
                operator_name=operator_name.strip(),
                status=job.status,
                created_at=job.created_at,
            )

            scraper_service.start_background_job(updated_job, assessments)
            st.session_state.current_job_id = updated_job.id
            # リロードされてもジョブを追跡し続けられるよう、URLにも保持しておく
            st.query_params["job_id"] = updated_job.id

            # アップローダーを空の状態に戻す(keyを変えて別ウィジェット扱いにする)。
            # これにより、ポーリングでrerunが起き続けても uploaded_file は None に
            # なるため、このブロック自体が二度と実行されなくなる。
            st.session_state.uploader_key = st.session_state.get("uploader_key", 0) + 1
            st.session_state.imported_file_fingerprint = None
            st.session_state.pending_import = None

            st.rerun()

# 5. リアルタイム進行状況モニタリング
job_id = st.session_state.current_job_id

if job_id:
    st.markdown("---")
    st.markdown("### リアルタイム実行状況")

    job_progress_info = scraper_service.get_job_progress(job_id)
    current_job_opt = job_progress_info[0]
    assessments = job_progress_info[1]
    total = job_progress_info[2]
    completed = job_progress_info[3]

    if current_job_opt:
        percent = int((completed / total) * 100) if total > 0 else 0
        st.progress(percent)

        col1, col2, col3 = st.columns(3)
        with col1:
            st.markdown(
                f'<div class="metric-card" style="margin-bottom:1rem;"><p style="margin:0;color:#666;font-size:0.9rem;">総ドメイン数</p><h2 style="margin:5px 0;color:#1F4E78;font-weight:700;">{total} 件</h2></div>',
                unsafe_allow_html=True,
            )
        with col2:
            st.markdown(
                f'<div class="metric-card" style="margin-bottom:1rem;"><p style="margin:0;color:#666;font-size:0.9rem;">解析完了</p><h2 style="margin:5px 0;color:#2e7d32;font-weight:700;">{completed} 件</h2></div>',
                unsafe_allow_html=True,
            )
        with col3:
            st.markdown(
                f'<div class="metric-card" style="margin-bottom:1rem;"><p style="margin:0;color:#666;font-size:0.9rem;">現在の進捗率</p><h2 style="margin:5px 0;color:#333;font-weight:700;">{percent} %</h2></div>',
                unsafe_allow_html=True,
            )

        # リセット処理を共通化するためのヘルパー関数/ボタン処理
        def clear_current_job() -> None:
            st.session_state.current_job_id = None
            if "job_id" in st.query_params:
                del st.query_params["job_id"]
            st.rerun()

        if current_job_opt.status == "processing":
            st.info(f"処理実行中... ({completed}/{total} 件完了)")
            time.sleep(1.5)
            st.rerun()

        elif current_job_opt.status == "completed":
            st.success("すべてのドメインの解析が完了しました。")

            output_path_key = f"output_path_{job_id}"

            if output_path_key not in st.session_state:
                temp_dir = Path("temp")
                temp_dir.mkdir(exist_ok=True)
                output_filename = f"result_{datetime.now().strftime('%Y%m%d%H%M%S')}.xlsx"
                output_path = temp_dir / output_filename
                ExcelService.export_excel(assessments, output_path)
                st.session_state[output_path_key] = output_path
            else:
                output_path = st.session_state[output_path_key]

            # 2列で並べ、ダウンロード(Primary:濃い青)とクリア(Secondary:白地)で差別化
            d_col1, d_col2 = st.columns([2, 1])
            with d_col1:
                with open(output_path, "rb") as file:
                    st.download_button(
                        label="調査結果Excelをダウンロード",
                        data=file,
                        file_name=output_path.name,
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True,
                        type="primary",
                    )
            with d_col2:
                if st.button(
                    "結果表示をクリア",
                    use_container_width=True,
                    key="clear_btn_completed",
                    type="secondary",
                ):
                    clear_current_job()

        elif current_job_opt.status == "failed":
            st.error("予期せぬエラーが発生したため、処理が中断されました。")
            if st.button("画面をクリアしてやり直す", key="clear_btn_failed", type="secondary"):
                clear_current_job()

        elif current_job_opt.status == "interrupted":
            st.warning("前回の処理はアプリの再起動（Ctrl+C等による停止を含む）により中断されました。お手数ですが、再度Excelをアップロードして最初からやり直してください。")
            if st.button("画面をクリア", key="clear_btn_interrupted", type="secondary"):
                clear_current_job()
