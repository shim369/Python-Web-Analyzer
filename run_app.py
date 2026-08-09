"""Web Site Analyzer の初回セットアップ〜起動を一括で行うランチャー本体。

このスクリプトは run_app.bat（Windows）/ run_app.command（macOS）から
`uv run --python 3.12 run_app.py` という形で呼ばれる想定。
つまり、このスクリプトが実行され始めた時点で uv 自体は既に用意されている
（wrapper側でインストール済み）。Python本体のダウンロード・管理も含めて
すべて uv に一本化しており、システムにあらかじめPythonが入っている必要はない。

やること（初回のみ、時間がかかる）:
  1. .venv が無ければ `uv venv --python 3.12` で作成（Python本体が
     未取得ならuvが自動でダウンロードする）
  2. requirements.txt の内容が前回と変わっていれば `uv pip install`
  3. Playwright（クロール時のJSレンダリングに使用）のChromiumが
     未取得なら `playwright install chromium` を実行
  4. streamlit run main.py でアプリを起動

2回目以降は、上記1〜3の完了状態をマーカーファイル(.venv内)で判定し、
変更が無ければスキップしてすぐに4.の起動だけを行う。
"""

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
MAIN_SCRIPT = PROJECT_ROOT / "main.py"
REQUIREMENTS_FILE = PROJECT_ROOT / "requirements.txt"
VENV_DIR = PROJECT_ROOT / ".venv"

# uvにダウンロード・管理させるPythonのバージョン。
# システムに何がインストールされていても、ここで指定したバージョンをuvが
# 独自に取得して使うため、システムPythonの有無・バージョンに依存しない。
PYTHON_VERSION = "3.12"

IS_WINDOWS = sys.platform.startswith("win")
VENV_PYTHON = VENV_DIR / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")

DEPS_MARKER = VENV_DIR / ".deps_installed.sha256"
PLAYWRIGHT_MARKER = VENV_DIR / ".playwright_installed"


def run(cmd: list[str], **kwargs) -> None:
    """コマンドを実行し、失敗したら例外を投げて呼び出し元で分かりやすく落とす。"""
    print(f"[RUN] {' '.join(cmd)}")
    subprocess.run(cmd, check=True, cwd=str(PROJECT_ROOT), **kwargs)


def find_uv() -> str | None:
    """PATH上、または公式インストーラーのデフォルト設置場所からuvを探す。

    通常はwrapper(.bat/.command)側でuvのインストール・PATH設定まで
    済ませてから `uv run` 経由でこのスクリプトを呼ぶため、PATH検索で
    見つかるはず。念のため、直接pythonでこのファイルを実行した場合に
    備えたフォールバックも用意しておく。
    """
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


def ensure_venv(uv_exe: str) -> None:
    if VENV_PYTHON.exists():
        return
    print(f"[SETUP] 仮想環境(.venv)が見つからないため uv で新規作成します（Python {PYTHON_VERSION}も必要なら自動取得）...")
    run([uv_exe, "venv", "--python", PYTHON_VERSION, str(VENV_DIR)])
    print("[SETUP] 仮想環境を作成しました。")


def requirements_hash() -> str:
    if not REQUIREMENTS_FILE.exists():
        return ""
    return hashlib.sha256(REQUIREMENTS_FILE.read_bytes()).hexdigest()


def ensure_dependencies(uv_exe: str) -> None:
    current_hash = requirements_hash()
    previous_hash = DEPS_MARKER.read_text().strip() if DEPS_MARKER.exists() else ""

    if current_hash and current_hash == previous_hash:
        print("[SETUP] 依存パッケージは最新です。インストールをスキップします。")
        return

    if not REQUIREMENTS_FILE.exists():
        print("[WARN] requirements.txt が見つかりません。依存パッケージのインストールをスキップします。")
        return

    print("[SETUP] uv で依存パッケージをインストールします（初回や更新時は数分かかります）...")
    run([uv_exe, "pip", "install", "--python", str(VENV_PYTHON), "-r", str(REQUIREMENTS_FILE)])
    DEPS_MARKER.write_text(current_hash)
    print("[SETUP] 依存パッケージのインストールが完了しました。")


def ensure_playwright_browser() -> None:
    if PLAYWRIGHT_MARKER.exists():
        return

    print("[SETUP] Playwright用ブラウザ(Chromium)を取得します（初回のみ・数百MB程度）...")
    try:
        run([str(VENV_PYTHON), "-m", "playwright", "install", "chromium"])
        PLAYWRIGHT_MARKER.write_text("ok")
        print("[SETUP] Playwrightのセットアップが完了しました。")
    except subprocess.CalledProcessError:
        # playwrightがrequirements.txtに含まれていない/未対応環境等でも、
        # アプリ全体の起動は止めたくないため警告のみに留める。
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
        print("[ERROR] uv が見つかりません。run_app.bat（Windows）/ run_app.command（macOS）から起動してください（uvの自動インストールはそちらで行っています）。")
        return 1

    try:
        ensure_venv(uv_exe)
        ensure_dependencies(uv_exe)
        ensure_playwright_browser()
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] セットアップ中にエラーが発生しました: {e}")
        return 1

    return launch_app()


if __name__ == "__main__":
    sys.exit(main())
