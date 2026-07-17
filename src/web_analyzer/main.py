import time
from datetime import datetime
from pathlib import Path

import streamlit as st

from web_analyzer.core.excel_service import ExcelService
from web_analyzer.core.scraper_service import SiteScraperService
from web_analyzer.models import ScrapingJob

st.set_page_config(
    page_title="Web Site Analyzer",
    layout="wide",
    initial_sidebar_state="expanded",
)

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
            color: #1d78c9;
            font-size: 2.0rem;
            font-weight: 700;
            margin-bottom: 0.5rem;
            border-bottom: 2px solid #1F4E78;
            padding-bottom: 0.5rem;
        }
        /* サブタイトルの装飾 */
        .sub-title {
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

    operator_name = st.text_input("担当者名", value="", placeholder="氏名を入力")

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

# ファイルアップローダー
uploaded_file = st.file_uploader(
    "インポート用Excelファイル（B列にドメイン名が配置されたシート）を選択、またはドラッグ＆ドロップしてください",
    type=["xlsx"],
)

# operator_nameの有無にかかわらず、ファイルがアップされたら即時解析してボタンを表示する
if uploaded_file:
    temp_dir = Path("temp")
    temp_dir.mkdir(exist_ok=True)
    input_path = temp_dir / uploaded_file.name

    with open(input_path, "wb") as f:
        f.write(uploaded_file.getbuffer())

    try:
        # Excelの仮インポート処理
        job, assessments = ExcelService.import_excel(
            file_path=input_path,
            operator_name=operator_name if operator_name.strip() else "未指定",
            threshold_1=threshold_1,
            threshold_2=threshold_2,
            threshold_3=threshold_3,
        )

        st.success(f"ファイルを正常に読み込みました。 (対象ドメイン数: {len(assessments)}件)")

        # 2. 「調査を開始する」ボタン押下時のロジック

        if st.button("調査を開始する", use_container_width=True):
            # ボタンが押されたタイミングで、担当者名が空なら警告を出す（バリデーション）
            if not operator_name.strip():
                st.error("調査を開始するには、サイドバーから「担当者名」を入力してください。")
            else:
                # 読み取り専用プロパティの代入を避け、新しいインスタンスを再生成する
                updated_job = ScrapingJob(
                    id=job.id,
                    operator_name=operator_name.strip(),  # 新しい担当者名を指定
                    threshold_1=job.threshold_1,
                    threshold_2=job.threshold_2,
                    threshold_3=job.threshold_3,
                    status=job.status,
                    created_at=job.created_at,
                )

                # 新しく作ったオブジェクトを渡してジョブを開始
                scraper_service.start_background_job(updated_job, assessments)
                st.session_state.current_job_id = updated_job.id
                st.rerun()

    except Exception as e:
        st.error(f"ファイルのインポート処理中にエラーが発生しました: {e}")

# -----------------------------------------------------------------------------
# 5. リアルタイム進行状況モニタリング
# -----------------------------------------------------------------------------
job_id = st.session_state.current_job_id

if job_id:
    st.markdown("---")
    st.markdown("### リアルタイム実行状況")

    # 進捗データの取得
    job_progress_info = scraper_service.get_job_progress(job_id)
    current_job_opt = job_progress_info[0]
    assessments = job_progress_info[1]
    total = job_progress_info[2]
    completed = job_progress_info[3]

    if current_job_opt:
        percent = int((completed / total) * 100) if total > 0 else 0

        # 進捗バーとメトリクスの表示
        st.progress(percent)

        col1, col2, col3 = st.columns(3)
        with col1:
            st.markdown(
                '<div class="metric-card">'
                '<p style="margin:0;color:#666;font-size:0.9rem;">総ドメイン数</p>'
                f'<h2 style="margin:5px 0;color:#1F4E78;font-weight:700;">{total} 件</h2>'
                "</div>",
                unsafe_allow_html=True,
            )
        with col2:
            st.markdown(
                '<div class="metric-card">'
                '<p style="margin:0;color:#666;font-size:0.9rem;">解析完了</p>'
                f'<h2 style="margin:5px 0;color:#2e7d32;font-weight:700;">{completed} 件</h2>'
                "</div>",
                unsafe_allow_html=True,
            )
        with col3:
            st.markdown(
                '<div class="metric-card">'
                '<p style="margin:0;color:#666;font-size:0.9rem;">現在の進捗率</p>'
                f'<h2 style="margin:5px 0;color:#333;font-weight:700;">{percent} %</h2>'
                "</div>",
                unsafe_allow_html=True,
            )

        # ステータスごとの処理
        if current_job_opt.status == "processing":
            st.info(f"処理実行中... ({completed}/{total} 件完了)")
            time.sleep(1.5)  # サーバー負荷防止のため少し休止
            st.rerun()  # 画面をリロードして進捗を最新にする

        elif current_job_opt.status == "completed":
            st.success("すべてのドメインの解析が完了しました。")

            output_filename = f"result_{datetime.now().strftime('%Y%m%d%H%M%S')}.xlsx"
            output_path = Path("temp") / output_filename
            ExcelService.export_excel(assessments, output_path)

            with open(output_path, "rb") as file:
                st.download_button(
                    label="調査結果Excelをダウンロード",
                    data=file,
                    file_name=output_filename,
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                )

        elif current_job_opt.status == "failed":
            st.error("予期せぬエラーが発生したため、処理が中断されました。")
