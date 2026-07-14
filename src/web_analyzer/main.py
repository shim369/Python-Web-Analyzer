import time
from datetime import datetime
from pathlib import Path

import streamlit as st

from web_analyzer.core.excel_service import ExcelService
from web_analyzer.core.scraper_service import SiteScraperService
from web_analyzer.models import ScrapingJob, SiteAssessment

# ページの設定
st.set_page_config(
    page_title="サイトリニューアル可否自動判定ツール",
    page_icon="⚡",
    layout="centered",
)

# セッション管理の初期化
if "scraper_service" not in st.session_state:
    st.session_state.scraper_service = SiteScraperService()
if "current_job_id" not in st.session_state:
    st.session_state.current_job_id = None
if "completed_assessments" not in st.session_state:
    st.session_state.completed_assessments = None
if "operator_name" not in st.session_state:
    st.session_state.operator_name = "山田 太郎"

scraper_service: SiteScraperService = st.session_state.scraper_service

# ヘッダー
st.title("⚡ サイトリニューアル可否自動判定ツール")
st.markdown("インポートされたExcelファイルのドメインリストから、SSL状態・ページ数・CMS・問い合わせ項目などを自動巡回解析し、判定結果を出力します。")

st.write("---")

# 1. ツール設定エリア
st.header("⚙️ ツール設定")

col1, col2 = st.columns([1, 1])

with col1:
    operator_input = st.text_input(
        "担当者名",
        value=st.session_state.operator_name,
        placeholder="担当者の名前を入力してください",
        help="出力Excelの「担当」列に書き込まれます。",
    )
    st.session_state.operator_name = operator_input

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

# 3. アクション＆進捗・完了エリア
if uploaded_file is not None:
    st.success(f"ファイル「{uploaded_file.name}」を正常に受け付けました！")

    is_processing = st.session_state.current_job_id is not None

    col_btn1, col_btn2 = st.columns([1, 1])

    with col_btn1:
        if st.button("⚡ 調査を開始する", use_container_width=True, type="primary", disabled=is_processing):
            # 新規調査開始時、古い完了キャッシュをクリア
            st.session_state.completed_assessments = None

            temp_path = Path(f"temp_{uploaded_file.name}")
            with open(temp_path, "wb") as f:
                f.write(uploaded_file.getbuffer())

            try:
                # ExcelServiceからデータをインポート
                _, assessments = ExcelService.import_excel(temp_path, st.session_state.operator_name)

                if not assessments:
                    st.error("Excelファイルから有効なドメイン（B列）を検出できませんでした。")
                else:
                    job_id = f"job_{int(time.time())}"
                    job = ScrapingJob(
                        id=job_id,
                        operator_name=st.session_state.operator_name,
                        threshold_1=int(threshold_1),
                        threshold_2=int(threshold_2),
                        threshold_3=int(threshold_3),
                        status="pending",
                        created_at=datetime.now(),
                    )

                    # バックグラウンド処理スタート
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
        progress_bar = st.progress(0)
        status_text = st.empty()

        while True:
            job_info, assessments_list, total, completed = scraper_service.get_job_progress(job_id)

            if not job_info:
                break

            progress_ratio = completed / total if total > 0 else 0.0
            progress_bar.progress(progress_ratio)
            status_text.markdown(f"**ステータス: {job_info.status.upper()}** ({completed} / {total} 件完了)")

            if job_info.status in ("completed", "failed"):
                if job_info.status == "completed":
                    st.success("🎉 全てのサイト調査が完了しました！")
                    st.session_state.completed_assessments = assessments_list
                else:
                    st.error("❌ 調査中に致命的なエラーが発生し、停止しました。")

                st.session_state.current_job_id = None
                st.rerun()
                break

            time.sleep(0.5)
            st.rerun()

    # 5. 解析完了画面＆エクスポート処理
    if st.session_state.completed_assessments is not None:
        st.write("---")
        st.header("📊 解析結果のダウンロード")

        # 簡易的なサマリー表示
        results: list[SiteAssessment] = st.session_state.completed_assessments
        total_sites = len(results)
        ok_count = sum(1 for r in results if r.evaluation_result in ("◎", "◯", "△"))
        ng_count = total_sites - ok_count

        col_res1, col_res2, col_res3 = st.columns(3)
        col_res1.metric("総調査サイト数", f"{total_sites} 件")
        col_res2.metric("リニューアル推奨 (◎/◯/△)", f"{ok_count} 件")
        col_res3.metric("対象外 (×)", f"{ng_count} 件")

        # openpyxlで生成したExcelをメモリ上のバイナリに変換
        try:
            # Excel用の一時ファイルを作成
            temp_out_path = Path("temp_output.xlsx")
            ExcelService.export_excel(results, temp_out_path)

            # バイナリとして読み込み
            with open(temp_out_path, "rb") as f:
                excel_data = f.read()

            # 一時ファイルの削除
            if temp_out_path.exists():
                temp_out_path.unlink()

            # ダウンロードボタンの設置
            st.download_button(
                label="📥 調査結果Excelをダウンロードする",
                data=excel_data,
                file_name=f"site_assessment_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
        except Exception as e:
            st.error(f"ダウンロードファイルの生成中にエラーが発生しました: {e}")

else:
    st.warning("ファイルをドロップすると、ここに「調査開始」ボタンが表示されます。")
