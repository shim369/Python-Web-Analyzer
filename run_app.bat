@echo off
chcp 65001 >nul
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
    echo [INFO] uv が見つからないため、インストールします（インターネット接続が必要です）...
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
)

rem インストール直後は同一セッションのPATHにまだ反映されていないことがあるため、
rem 既定の設置場所を明示的に先頭へ追加しておく（既にPATHが通っていても無害）
set "PATH=%USERPROFILE%\.local\bin;%PATH%"

where uv >nul 2>nul
if errorlevel 1 (
    echo [ERROR] uv のインストールに失敗しました。
    echo         https://docs.astral.sh/uv/getting-started/installation/ を参照し、
    echo         手動でインストールしてから再実行してください。
    pause
    exit /b 1
)

echo [INFO] 初回起動時は Python本体・依存パッケージの取得のため数分かかることがあります。
uv run --python 3.12 run_app.py

if errorlevel 1 (
    echo [ERROR] アプリの起動に失敗しました。上のログを確認してください。
)
pause
