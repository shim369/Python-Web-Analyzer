import time
from datetime import datetime
from pathlib import Path

import streamlit as st

from web_analyzer.core.excel_service import ExcelService
from web_analyzer.core.scraper_service import SiteScraperService
from web_analyzer.models import ScrapingJob

# ページの設定
st.set_page_config(
    page_title="サイトリニューアル可否自動判定ツール",
    page_icon="⚡",
    layout="centered",
)

# サービスの初期化（セッションを跨いで保持）
if "scraper_service" not in st.session_state:
    st.session_state.scraper_service = SiteScraperService()
if "current_job_id" not in st.session_state:
    st.session_state.current_job_id = None

scraper_service: SiteScraperService = st.session_state.scraper_service

# ヘッダー
st.title("⚡ サイトリニューアル可否自動判定ツール")
st.markdown(
    "インポートされたExcelファイルのドメインリストから、"
    "SSL状態・ページ数・CMS・問い合わせ項目などを自動巡回解析し、判定結果を出力します。"
)

st.write("---")

# 1. ツール設定エリア
st.header("⚙️ ツール設定")

col1, col2 = st.columns([1, 1])

with col1:
    operator_name = st.text_input(
        "担当者名",
        value="山田 太郎",
        placeholder="担当者の名前を入力してください",
        help="出力Excelの「担当」列に書き込まれます。",
    )

with col2:
    st.markdown("**判定ページ数基準（しきい値）**")
    threshold_1 = st.number_input(
        "◎ 判定の基準 (ページ以下)",
        min_value=1,
        value=10,
        step=1,
        help="CMSなし、かつこのページ数以下の場合に「◎」判定となります。",
    )
    threshold_2 = st.number_input(
        "◯ 判定の基準 (ページ以下)",
        min_value=1,
        value=15,
        step=1,
    )
    threshold_3 = st.number_input(
        "△ 判定の基準 (ページ以下)",
        min_value=1,
        value=20,
        step=1,
        help="これを超えると自動的に「×：ページ数が多いため」になります。",
    )

st.write("---")

# 2. ファイルアップロードエリア
st.header("📂 ファイル読み込み")

uploaded_file = st.file_uploader(
    "解析対象のExcelファイル（.xlsx）をドラッグ＆ドロップしてください",
    type=["xlsx"],
    help="B列にドメイン名（サイト名）が記載されている必要があります。",
)

st.write("---")

# 3. アクション＆進捗エリア
if uploaded_file is not None:
    st.success(f"ファイル「{uploaded_file.name}」を正常に受け付けました！")

    # 調査開始ボタン（実行中は非活性に）
    is_processing = st.session_state.current_job_id is not None

    if st.button("⚡ 調査を開始する", use_container_width=True, type="primary", disabled=is_processing):
        # 1. 一時ファイルとして保存してインポート
        temp_path = Path(f"temp_{uploaded_file.name}")
        with open(temp_path, "wb") as f:
            f.write(uploaded_file.getbuffer())

        try:
            # 【修正①】operator_name を第2引数として正しく渡す
            _, assessments = ExcelService.import_excel(temp_path, operator_name)

            if not assessments:
                st.error("Excelファイルから有効なドメイン（B列）を検出できませんでした。")
            else:
                # 2. 新しいジョブの作成
                job_id = f"job_{int(time.time())}"
                job = ScrapingJob(
                    id=job_id,
                    operator_name=operator_name,
                    threshold_1=int(threshold_1),
                    threshold_2=int(threshold_2),
                    threshold_3=int(threshold_3),
                    status="pending",
                    created_at=datetime.now(),
                )

                # 3. バックグラウンドスレッドでスクレイピング開始
                scraper_service.start_background_job(job, assessments)
                st.session_state.current_job_id = job_id
                st.rerun()

        except Exception as e:
            st.error(f"Excelファイルの読み込み中にエラーが発生しました: {e}")
        finally:
            if temp_path.exists():
                temp_path.unlink()

    # 4. プログレス監視（ポーリング）ループ
    if st.session_state.current_job_id:
        job_id = st.session_state.current_job_id

        # 進捗プレースホルダーの作成
        progress_bar = st.progress(0)
        status_text = st.empty()

        # 状態が完了または失敗になるまでループ監視
        while True:
            # 【修正②】戻り値の4つの要素を正しくアンパックして受け取る
            job_info, assessments_list, total, completed = scraper_service.get_job_progress(job_id)

            if not job_info:
                break

            # 進捗率の計算
            progress_ratio = completed / total if total > 0 else 0.0
            progress_bar.progress(progress_ratio)
            status_text.markdown(f"**ステータス: {job_info.status.upper()}** ({completed} / {total} 件完了)")

            if job_info.status in ("completed", "failed"):
                if job_info.status == "completed":
                    st.success("🎉 全てのサイト調査が完了しました！")
                    # セッションに結果を一時保存（エクスポート機能用）
                    st.session_state.completed_assessments = assessments_list
                else:
                    st.error("❌ 調査中に致命的なエラーが発生し、停止しました。")

                # ジョブIDをリセットして再活性化
                st.session_state.current_job_id = None
                break

            # 0.5秒待機して再取得（簡易ポーリング）
            time.sleep(0.5)
            st.rerun()

else:
    st.warning("ファイルをドロップすると、ここに「調査開始」ボタンが表示されます。")
