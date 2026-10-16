from bs4 import BeautifulSoup

from web_analyzer.core.crawler import HTML_PARSER, WebCrawler


def _menus(html: str) -> list[str]:
    return WebCrawler()._extract_global_nav_menus(BeautifulSoup(html, HTML_PARSER), "")


def test_picks_largest_list_in_nav_and_skips_sp_only_items() -> None:
    """shinx-corp.co.jp の構造を簡略化したもの。

    ナビ領域内の、id名に"nav"を含む小さなボタン群ではなく、項目数の多い本体のリストを採る。
    スマホ表示専用の項目(li.sp)は除外する。
    """
    html = """
    <header><div class="inner"><nav class="head">
      <div class="nav-main"><ul id="global">
        <li class="top"><a href="/">HOME</a></li>
        <li class="products has-child"><a href="/products/">商品ラインナップ</a>
          <ul class="child"><li><a href="/a/">アルミ板</a></li><li><a href="/b/">伸銅板</a></li></ul></li>
        <li class="company"><a href="/company/">会社案内</a></li>
        <li class="sp"><a href="/notice/">お知らせ</a></li>
        <li class="recruit"><a href="/recruit/">採用情報</a></li>
      </ul></div>
      <ul id="nav-sub">
        <li><a href="/estimate/">WEB見積もり</a></li>
        <li><a href="/invoice/">WEB請求書</a></li>
        <li><a href="/contact/">お問い合わせ</a></li>
      </ul>
    </nav></div></header>
    """
    assert _menus(html) == ["HOME", "商品ラインナップ", "会社案内", "採用情報"]


def test_falls_back_to_site_sidebar_nav() -> None:
    """shk-net.co.jp の構造を簡略化したもの。ヘッダーにメニューが無く、サイドバーにある。"""
    html = """
    <body>
    <header class="header"><div class="logo"><a href="/"><img alt="" src="logo.png"></a></div></header>
    <aside class="sidebar"><div class="sidebar_inner"><ul class="snav">
      <li class="pc"><a href="/">TOP</a></li>
      <li><a href="/about">当社について</a></li>
      <li><a href="/product">製品紹介</a><span class="sp_toggle"></span>
        <div class="snav_child hide"><div><a href="/p/1">製品A</a></div><div><a href="/p/2">製品B</a></div></div></li>
      <li><a href="/seminar">有機質肥料<br class="pc">WEB資料館</a></li>
    </ul></div></aside>
    <article class="main"><ul><li><a href="/news/1">2026.07.13 お知らせ</a></li></ul></article>
    </body>
    """
    assert _menus(html) == ["TOP", "当社について", "製品紹介", "有機質肥料WEB資料館"]


def test_blog_category_sidebar_is_not_used() -> None:
    """本文領域内のサイドバー(ブログのカテゴリ一覧等)はグローバルナビとみなさない。"""
    html = """
    <body>
    <header><div class="logo"><a href="/"><img alt="" src="logo.png"></a></div></header>
    <main><aside class="sidebar"><ul>
      <li><a href="/cat/1">カテゴリ1</a></li><li><a href="/cat/2">カテゴリ2</a></li><li><a href="/cat/3">カテゴリ3</a></li>
    </ul></aside></main>
    </body>
    """
    assert _menus(html) == []


def test_dropdown_parent_label_without_link() -> None:
    """shobundo.org の構造を簡略化したもの。

    - 小さなボタン群のnavより、項目数の多い本体のnavを採る
    - ドロップダウンの親項目は、見出しがリンクでなく<span>のことがある(子メニューのリンク名を拾わない)
    - class名に"menu"を含むspan(部分一致で"en"扱いされていた)を英語表記として消さない
    """
    html = """
    <header class="l-header">
      <nav class="l-header__top-menu"><ul class="l-header__top-menu-list">
        <li><span>その他</span><div class="drop"><a href="/faq/">FAQ</a><a href="/dl/">ダウンロード</a></div></li>
        <li><a href="/contact/">お問い合わせ</a></li>
      </ul></nav>
      <nav class="l-header__bottom-menu"><ul class="l-header__menu-bottom-list">
        <li class="js-has-dropdown"><a href="/line-up/"><span class="l-header__menu-bottom-box">製品のご案内</span></a>
          <div class="l-header__under-menu"><div class="heading"><a href="">製品のご案内</a></div>
            <ul><li><a href="/trump/">トランプ</a></li><li><a href="/karuta/">かるた</a></li></ul></div></li>
        <li class="js-has-dropdown"><span class="l-header__menu-bottom-box">各注意事項</span>
          <div class="l-header__under-menu"><div class="heading">各種注意事項 ご依頼の際には必ずお読みください。</div>
            <ul><li><a href="/design/">デザイン上の注意点</a></li><li><a href="/data/">版下データ</a></li></ul></div></li>
        <li><a href="/news/"><span class="l-header__menu-bottom-box">お知らせ</span></a></li>
      </ul></nav>
    </header>
    """
    assert _menus(html) == ["製品のご案内", "各注意事項", "お知らせ"]


def test_non_dropdown_text_item_is_ignored() -> None:
    """ナビのリスト内にあるコピーライト等、子メニューを持たない文字だけの項目は拾わない。"""
    html = """
    <nav class="gnav"><ul>
      <li><a href="/service/">サービス</a></li>
      <li><a href="/company/">会社情報</a></li>
      <li><a href="/contact/">お問い合わせ</a></li>
      <li class="copyright">Copyright © Example Inc.</li>
    </ul></nav>
    """
    assert _menus(html) == ["サービス", "会社情報", "お問い合わせ"]


def test_megamenu_nav_is_not_penalized_for_child_links() -> None:
    """子リンクの多いメガメニューのnavが、リンク総数で減点されて別領域に負けないこと。"""
    children = "".join(f'<li><a href="/p/{i}/">製品{i}</a></li>' for i in range(40))
    html = f"""
    <nav class="main-menu"><ul>
      <li><a href="/">トップページ</a></li>
      <li><a href="/products/">製品一覧</a><ul class="sub-menu">{children}</ul></li>
      <li><a href="/company/">会社概要</a></li>
      <li><a href="/contact/">お問い合わせ</a></li>
    </ul></nav>
    <div class="carousel-navigation"><a href="#1">前へ</a><a href="#2">次へ</a><a href="#3">1</a></div>
    """
    assert _menus(html) == ["トップページ", "製品一覧", "会社概要", "お問い合わせ"]


def test_url_encoded_footer_menu_id_is_excluded() -> None:
    """WordPressの日本語メニュー名からできたURLエンコードのid(=フッターメニュー)は採らない。"""
    footer_items = "".join(f'<li><a href="/f{i}/">フッター項目{i}</a></li>' for i in range(8))
    html = f"""
    <nav class="gnav"><ul class="menu">
      <li><a href="/news/">新着情報</a></li>
      <li><a href="/company/">会社概要</a></li>
      <li><a href="/contact/">お問い合わせ</a></li>
    </ul></nav>
    <div class="f-area"><ul class="menu" id="menu-%e3%83%95%e3%83%83%e3%82%bf%e3%83%bc">{footer_items}</ul></div>
    """
    assert _menus(html) == ["新着情報", "会社概要", "お問い合わせ"]


def test_sub_label_class_matching() -> None:
    assert WebCrawler._is_sub_label_class("en")
    assert WebCrawler._is_sub_label_class("txt-en")
    assert WebCrawler._is_sub_label_class("menu_sub")
    assert WebCrawler._is_sub_label_class("navEn")
    assert not WebCrawler._is_sub_label_class("l-header__menu-bottom-box")
    assert not WebCrawler._is_sub_label_class("center")
    assert not WebCrawler._is_sub_label_class("content")


def test_collect_nav_candidates_includes_link_groups_outside_lists() -> None:
    """ul/liを使わず、テーブル内に画像リンクを並べた古い作りのナビも候補にする(画像はaltを項目名に使う)。"""
    html = """
    <body>
    <table class="menu"><tr>
      <td><a href="/company/"><img alt="会社概要" src="1.gif"></a></td>
      <td><a href="/business/"><img alt="事業内容" src="2.gif"></a></td>
      <td><a href="/contact/"><img alt="お問い合わせ" src="3.gif"></a></td>
    </tr></table>
    <ul class="gnav"><li><a href="/">TOP</a></li><li><a href="/a/">製品</a></li><li><a href="/b/">採用</a></li></ul>
    </body>
    """
    candidates = WebCrawler().collect_nav_candidates(BeautifulSoup(html, HTML_PARSER))
    assert candidates == [["TOP", "製品", "採用"], ["会社概要", "事業内容", "お問い合わせ"]]


def test_clean_menu_text_removes_icon_names_and_arrows() -> None:
    crawler = WebCrawler()
    assert crawler._clean_menu_text("企業情報keyboard_arrow_down") == "企業情報"
    assert crawler._clean_menu_text("会社概要→") == "会社概要"
    assert crawler._clean_menu_text("● HOME") == "HOME"


def test_links_wrapping_list_items() -> None:
    """tokio-eng.com の構造を簡略化したもの。<ul><a><li>項目</li></a>…の崩れたマークアップ。

    liを包むリンクにスマホ専用のclassが付いた項目は除く。
    """
    html = """
    <header><nav class="p-header__nav"><ul class="p-header__nav__list">
      <a class="sp-only" href="/"><li>ホーム</li></a>
      <a href="/strength"><li>わたしたちの強み</li></a>
      <a href="/about"><li>会社案内</li></a>
      <a href="/works"><li>製作事例</li></a>
      <a href="/news"><li>お知らせ</li></a>
    </ul></nav></header>
    """
    assert _menus(html) == ["わたしたちの強み", "会社案内", "製作事例", "お知らせ"]


def test_label_text_beside_empty_overlay_link() -> None:
    """tonox.jp の構造を簡略化したもの。項目名はliに書き、中身の無いリンクを重ねている。"""
    html = """
    <header><ul class="header-mainnav">
      <li>特装車<a href="speq.html" class="link"></a></li>
      <li>車体製造・組立<a href="kumitate.html" class="link"></a></li>
      <li>塗装<a href="paint.html" class="link"></a></li>
      <li>レストア<a href="restore.html" class="link"></a></li>
    </ul></header>
    """
    assert _menus(html) == ["特装車", "車体製造・組立", "塗装", "レストア"]
