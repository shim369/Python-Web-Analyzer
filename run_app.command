#!/bin/bash
cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
    echo "[INFO] uv が見つからないため、インストールします（インターネット接続が必要です）..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi

# インストール直後は同一セッションのPATHにまだ反映されていないことがあるため、
# 既定の設置場所を明示的に先頭へ追加しておく（既にPATHが通っていても無害）
export PATH="$HOME/.local/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
    echo "[ERROR] uv のインストールに失敗しました。"
    echo "        https://docs.astral.sh/uv/getting-started/installation/ を参照し、"
    echo "        手動でインストールしてから再実行してください。"
    read -p "Enterキーで終了..."
    exit 1
fi

echo "[INFO] 初回起動時は Python本体・依存パッケージの取得のため数分かかることがあります。"
uv run --python 3.12 run_app.py
status=$?

if [ $status -ne 0 ]; then
    echo "[ERROR] アプリの起動に失敗しました。上のログを確認してください。"
fi

read -p "Enterキーで終了..."
exit $status
