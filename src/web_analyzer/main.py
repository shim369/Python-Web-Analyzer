import logging
import os
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

from web_analyzer.core.excel_service import ExcelService
from web_analyzer.core.job_repository import JobRepository
from web_analyzer.core.scraper_service import SiteScraperService
from web_analyzer.models import ScrapingJob
from web_analyzer.utils.logger import setup_logger

# プロジェクトルートからの相対パスを指定
ICON_PATH = os.path.join("docs", "images", "cursor_icon.ico")

# 1. ページ設定を最初に行う
# favicon引数にアイコンファイルのパスを渡します
st.set_page_config(
    page_title="My AI Web Analyzer",  # ブラウザのタブに表示されるタイトル
    page_icon=ICON_PATH,  # ここでアイコンを指定
    layout="wide",
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


st.set_page_config(
    page_title="Web Site Analyzer",
    layout="wide",
    initial_sidebar_state="expanded",
)


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
        * トップイメージ等に動画
        * フローティング機能
        * アコーディオン
        * タブ切り替え
        * モーダルウィンドウ
        * 画像ギャラリー
        * アニメーション
        """
    )

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
                '<div class="metric-card" style="margin-bottom:1rem;">'
                '<p style="margin:0;color:#666;font-size:0.9rem;">総ドメイン数</p>'
                f'<h2 style="margin:5px 0;color:#1F4E78;font-weight:700;">{total} 件</h2>'
                "</div>",
                unsafe_allow_html=True,
            )
        with col2:
            st.markdown(
                '<div class="metric-card" style="margin-bottom:1rem;">'
                '<p style="margin:0;color:#666;font-size:0.9rem;">解析完了</p>'
                f'<h2 style="margin:5px 0;color:#2e7d32;font-weight:700;">{completed} 件</h2>'
                "</div>",
                unsafe_allow_html=True,
            )
        with col3:
            st.markdown(
                '<div class="metric-card" style="margin-bottom:1rem;">'
                '<p style="margin:0;color:#666;font-size:0.9rem;">現在の進捗率</p>'
                f'<h2 style="margin:5px 0;color:#333;font-weight:700;">{percent} %</h2>'
                "</div>",
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
