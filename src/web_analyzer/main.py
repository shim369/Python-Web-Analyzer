import streamlit as st

# ページの設定
st.set_page_config(
    page_title="サイトリニューアル可否自動判定ツール",
    page_icon="⚡",
    layout="centered",
)

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
        value="John Doe",
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

# 3. アクションエリア
if uploaded_file is not None:
    st.success(f"ファイル「{uploaded_file.name}」を正常に受け付けました！")

    # 調査開始ボタン
    if st.button("⚡ 調査を開始する", use_container_width=True, type="primary"):
        st.info("（※明日7/15に、ここを押したときのバックグラウンド処理の結合を行います！）")
else:
    st.warning("ファイルをドロップすると、ここに「調査開始」ボタンが表示されます。")
