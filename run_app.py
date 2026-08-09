"""Web Site Analyzer の初回セットアップ〜起動を一括で行うランチャー本体。"""

import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

# main.py のパスを src/web_analyzer/main.py に変更
MAIN_SCRIPT = PROJECT_ROOT / "src" / "web_analyzer" / "main.py"
VENV_DIR = PROJECT_ROOT / ".venv"

PYTHON_VERSION = "3.12"
IS_WINDOWS = sys.platform.startswith("win")
VENV_PYTHON = VENV_DIR / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")

PLAYWRIGHT_MARKER = VENV_DIR / ".playwright_installed"


def run(cmd: list[str], **kwargs) -> None:
    """コマンドを実行し、失敗したら例外を投げて呼び出し元で分かりやすく落とす。"""
    print(f"[RUN] {' '.join(cmd)}")
    subprocess.run(cmd, check=True, cwd=str(PROJECT_ROOT), **kwargs)


def find_uv() -> str | None:
    """PATH上、または公式インストーラーのデフォルト設置場所からuvを探す。"""
    found = shutil.which("uv")
    if found:
        return found

    home = Path.home()
    exe_name = "uv.exe" if IS_WINDOWS else "uv"
    candidates = [
        home / ".local" / "bin" / exe_name,
        home / ".cargo" / "bin" / exe_name,
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def ensure_dependencies(uv_exe: str) -> None:
    """requirements.txt ではなく uv sync を使用して pyproject.toml 等から依存関係を同期する"""
    print("[SETUP] uv sync で依存パッケージを同期・インストールします...")
    run([uv_exe, "sync"])


def ensure_playwright_browser() -> None:
    if PLAYWRIGHT_MARKER.exists():
        return

    print("[SETUP] Playwright用ブラウザ(Chromium)を取得します（初回のみ・数百MB程度）...")
    try:
        run([str(VENV_PYTHON), "-m", "playwright", "install", "chromium"])
        PLAYWRIGHT_MARKER.write_text("ok")
        print("[SETUP] Playwrightのセットアップが完了しました。")
    except subprocess.CalledProcessError:
        print("[WARN] Playwrightブラウザの取得に失敗しました。JSレンダリングを使う機能は動作しない可能性があります。")


def launch_app() -> int:
    if not MAIN_SCRIPT.exists():
        print(f"[ERROR] main.py が見つかりません: {MAIN_SCRIPT}")
        return 1

    print("[INFO] アプリを起動します...")
    result = subprocess.run(
        [str(VENV_PYTHON), "-m", "streamlit", "run", str(MAIN_SCRIPT)],
        cwd=str(PROJECT_ROOT),
    )
    return result.returncode


def main() -> int:
    uv_exe = find_uv()
    if not uv_exe:
        print("[ERROR] uv が見つかりません。")
        return 1

    try:
        ensure_dependencies(uv_exe)
        ensure_playwright_browser()
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] セットアップ中にエラーが発生しました: {e}")
        return 1

    return launch_app()


if __name__ == "__main__":
    sys.exit(main())
