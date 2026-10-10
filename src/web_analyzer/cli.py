import sys
from pathlib import Path

from streamlit.web import cli as stcli


def main() -> None:
    """`web-analyzer` コマンドのエントリーポイント。

    main.py はインポートした時点でStreamlitの画面描画コードが実行されるため、
    直接エントリーポイントにはできない。ここでは `streamlit run main.py` を
    呼び出すだけにする(追加の引数はそのままstreamlitへ渡す)。
    """
    app_path = Path(__file__).with_name("main.py")
    sys.argv = ["streamlit", "run", str(app_path), *sys.argv[1:]]
    sys.exit(stcli.main())
