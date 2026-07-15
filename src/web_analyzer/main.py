import time
from datetime import datetime
from pathlib import Path

import streamlit as st

from web_analyzer.core.excel_service import ExcelService
from web_analyzer.core.scraper_service import SiteScraperService
from web_analyzer.models import ScrapingJob

# 1. ページ設定（必ず最初に実行）
st.set_page_config(
    page_title="Web Site Analyzer",
    layout="wide",
    initial_sidebar_state="expanded",
)

# -----------------------------------------------------------------------------
# Google アナリティクス (GA4) トラッキングコードの埋め込み
# -----------------------------------------------------------------------------
# Ruffの行長150文字制限に引っかからないよう、URL文字列を分割して結合しています
ga_url = (
    "https://www.googletagmanager.com/gtag/js"
    "?id=G-2WN3P34LZQ"
)

ga_code = f"""
<script async src="{ga_url}"></script>
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){{dataLayer.push(arguments);}}
  gtag('js', new Date());

  gtag('config', 'G-2WN3P34LZQ');
</script>
"""

# HTMLをバックグラウンドに埋め込み（画面上には何も表示されません）
st.html(ga_code)

# チープな要素を排除し、信頼感のあるコーポレートブルーを基調としたフラットUI
st.markdown(
    """
    <style>
        /* 全体フォントの統一 */
        html, body, [class*="css"] {
            font-family: 'Yu Gothic', 'Hiragino Kaku Gothic ProN', sans-serif;
        }
        /* メインタイトルの装飾（落ち着いたネイビーのアンダーライン） */
        .main-title {
            color: #1F4E78;
            font-size: 2.0rem;
            font-weight: 700;
            margin-bottom: 0.5rem;
            border-bottom: 2px solid #1F4E78;
            padding-bottom: 0.5rem;
        }
        /* サブタイトルの装飾 */
        .sub-title {
            color: #666666;
            font-size: 0.95rem;
            margin-bottom: 2rem;
        }
        /* 起動ボタンのスタイリング */
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
        /* シンプルなカード風コンテナ */
        .metric-card {
            background-color: #ffffff;
            border: 1px solid #e2e8f0;
            border-radius: 6px;
            padding: 1.2rem;
            text-align: center;
            box-shadow: 0 1px 3px rgba(0,0,0,0.02);
        }
    </style>
    """,
    unsafe_allow_html=True,
)

# -----------------------------------------------------------------------------
# 2. 初期化とサービス生成
# -----------------------------------------------------------------------------
if "scraper_service" not in st.session_state:
    st.session_state.scraper_service = SiteScraperService()
if "current_job_id" not in st.session_state:
    st.session_state.current_job_id = None

scraper_service: SiteScraperService = st.session_state.scraper_service

# -----------------------------------------------------------------------------
# 3. サイドバーの設定（ビジネスライクな設定パネル）
# -----------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### 解析設定")
    st.write("調査パラメータおよび担当者名を設定してください。")

    operator_name = st.text_input("担当者名", value="担当者名", placeholder="氏名を入力")

    st.markdown("---")
    st.markdown("#### 判定しきい値（ページ数）")
    st.caption("各評価（◎, ◯, △）を分ける最大ページ数です。")

    threshold_1 = st.number_input("評価 ◎ の最大ページ数", min_value=1, value=10)
    threshold_2 = st.number_input("評価 ◯ の最大ページ数", min_value=1, value=15)
    threshold_3 = st.number_input("評価 △ の最大ページ数", min_value=1, value=20)

# -----------------------------------------------------------------------------
# 4. メインコンテンツエリア
# -----------------------------------------------------------------------------
st.markdown('<div class="main-title">Web Site Analyzer</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="sub-title">指定されたExcelリストから、複数ドメインのSSL対応状況、サイト構成、総ページ数を自動調査・評価します。</div>',
    unsafe_allow_html=True,
)

# ファイルアップローダー（150文字制限を確実にクリアできるよう、改行で接続）
uploaded_file = st.file_uploader(
    "インポート用Excelファイル（B列にドメイン名が配置されたシート）を選択、またはドラッグ＆ドロップしてください",
    type=["xlsx"],
)

if uploaded_file and operator_name:
    temp_dir = Path("temp")
    temp_dir.mkdir(exist_ok=True)
    input_path = temp_dir / uploaded_file.name

    with open(input_path, "wb") as f:
        f.write(uploaded_file.getbuffer())

    try:
        # mypy の代入における型不整合を防ぐため、受ける変数の型を明示
        job: ScrapingJob
        job, assessments = ExcelService.import_excel(
            file_path=input_path,
            operator_name=operator_name,
            threshold_1=threshold_1,
            threshold_2=threshold_2,
            threshold_3=threshold_3,
        )

        st.success(f"ファイルを正常に読み込みました。 (対象ドメイン数: {len(assessments)}件)")

        if st.button("調査を開始する", use_container_width=True):
            scraper_service.start_background_job(job, assessments)
            st.session_state.current_job_id = job.id

    except Exception as e:
        st.error(f"ファイルのインポート処理中にエラーが発生しました: {e}")

# -----------------------------------------------------------------------------
# 5. リアルタイム進行状況モニタリング
# -----------------------------------------------------------------------------
job_id = st.session_state.current_job_id

if job_id:
    st.markdown("---")
    st.markdown("### リアルタイム実行状況")

    metrics_placeholder = st.empty()
    progress_bar = st.progress(0)
    status_text = st.empty()
    download_placeholder = st.empty()

    while True:
        # mypy での型割り当て不整合を避けるため、受け取る job_progress の型を考慮
        job_progress_info = scraper_service.get_job_progress(job_id)
        current_job_opt = job_progress_info[0]
        assessments = job_progress_info[1]
        total = job_progress_info[2]
        completed = job_progress_info[3]

        if not current_job_opt:
            break

        percent = int((completed / total) * 100) if total > 0 else 0
        progress_bar.progress(percent)

        with metrics_placeholder.container():
            col1, col2, col3 = st.columns(3)
            # 各メトリクスカード内の長いHTML1行を複数行に綺麗に分割
            with col1:
                st.markdown(
                    '<div class="metric-card">'
                    '<p style="margin:0;color:#666;font-size:0.9rem;">'
                    "総ドメイン数"
                    "</p>"
                    f'<h2 style="margin:5px 0;color:#1F4E78;font-weight:700;">'
                    f"{total} 件"
                    "</h2>"
                    "</div>",
                    unsafe_allow_html=True,
                )
            with col2:
                st.markdown(
                    '<div class="metric-card">'
                    '<p style="margin:0;color:#666;font-size:0.9rem;">'
                    "解析完了"
                    "</p>"
                    f'<h2 style="margin:5px 0;color:#2e7d32;font-weight:700;">'
                    f"{completed} 件"
                    "</h2>"
                    "</div>",
                    unsafe_allow_html=True,
                )
            with col3:
                st.markdown(
                    '<div class="metric-card">'
                    '<p style="margin:0;color:#666;font-size:0.9rem;">'
                    "現在の進捗率"
                    "</p>"
                    f'<h2 style="margin:5px 0;color:#333;font-weight:700;">'
                    f"{percent} %"
                    "</h2>"
                    "</div>",
                    unsafe_allow_html=True,
                )

        status_text.write(f"処理実行中... ({completed}/{total} 件完了)")

        if current_job_opt.status == "completed":
            status_text.success("すべてのドメインの解析が完了しました。")

            output_filename = f"result_{datetime.now().strftime('%Y%m%d%H%M%S')}.xlsx"
            output_path = Path("temp") / output_filename
            ExcelService.export_excel(assessments, output_path)

            with open(output_path, "rb") as file:
                download_placeholder.download_button(
                    label="調査結果Excelをダウンロード",
                    data=file,
                    file_name=output_filename,
                    mime=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                    use_container_width=True,
                )
            break

        elif current_job_opt.status == "failed":
            status_text.error("予期せぬエラーが発生したため、処理が中断されました。")
            break

        time.sleep(1)
