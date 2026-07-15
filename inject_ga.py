import pathlib

import streamlit as st
from bs4 import BeautifulSoup, Tag


def inject_ga() -> None:
    """Streamlitシステム内のindex.htmlにGA4のタグを直接挿入する関数。"""
    # Streamlitパッケージ内にある大元の index.html のパスを取得
    index_path = pathlib.Path(st.__file__).parent / "static" / "index.html"
    soup = BeautifulSoup(index_path.read_text(encoding="utf-8"), "html.parser")

    # head要素を取得（存在しない場合は処理を中断）
    head: Tag | None = soup.head
    if head is None:
        return

    # すでに挿入済みでないかチェック
    if not soup.find(id="google-analytics-custom"):
        # GA4のタグ（あなたの測定IDに書き換えてあります）
        ga_script = soup.new_tag("script", id="google-analytics-custom")
        ga_script["async"] = ""  # mypy対策: boolではなく空文字列を代入
        ga_script["src"] = "https://www.googletagmanager.com/gtag/js?id=G-2WN3P34LZQ"
        head.append(ga_script)

        # 初期化コード
        ga_init = soup.new_tag("script")
        ga_init.string = """
          window.dataLayer = window.dataLayer || [];
          function gtag(){dataLayer.push(arguments);}
          gtag('js', new Date());
          gtag('config', 'G-2WN3P34LZQ');
        """
        head.append(ga_init)

        # 変更を書き戻す
        index_path.write_text(str(soup), encoding="utf-8")


if __name__ == "__main__":
    inject_ga()
