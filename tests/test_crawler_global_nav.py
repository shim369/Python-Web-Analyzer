import pytest
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


def test_english_label_without_class_is_removed() -> None:
    """is-kentetsu.jp / kasumicc.co.jp の構造を簡略化したもの。

    classの無い要素の英語表記も、項目名の前後どちらにあっても、英字で始まる項目名・「・」を含む項目名でも除く。
    英字だけの項目名(TOP等)はそのまま残す。
    """
    html = """
    <nav id="global-nav"><ul>
      <li><a href="/">TOP</a></li>
      <li><a href="/sdgs">SDGs宣言<span>SDGs</span></a></li>
      <li><a href="/flat">階段・手摺り<span>FLAT</span></a></li>
      <li><a href="/company/"><span>COMPANY</span>会社概要</a></li>
      <li><a href="/about/">NEXTEPについて<span>ABOUT US</span></a></li>
    </ul></nav>
    """
    assert _menus(html) == ["TOP", "SDGs宣言", "階段・手摺り", "会社概要", "NEXTEPについて"]


def test_sp_nav_loses_to_pc_nav() -> None:
    """itogawa.co.jp の構造を簡略化したもの。

    - 子メニューまで展開したスマホ用のnav(class="sp_navi")より、PC用のナビを採る
    - PC用ナビの中で、先に出てくるメガメニューの製品一覧と項目数が同じでも、ナビらしいclassのリストを採る
    """
    html = """
    <header><div id="global-nav">
      <nav class="pc_navi">
        <div class="mega"><ul>
          <li><a href="/p/1/">円筒型タンク</a></li><li><a href="/p/2/">角型タンク</a></li><li><a href="/p/3/">撹拌タンク</a></li>
        </ul></div>
        <div class="page_links"><ul class="nav-list gnavi_pc">
          <li><a href="/">TOP</a></li><li><a href="/features/">強み</a></li><li><a href="/faq/">FAQ</a></li>
        </ul></div>
      </nav>
      <nav class="sp_navi"><ul class="nav-list nav-list-global">
        <li><a href="/">TOP</a></li><li><a href="/features/">強み</a></li><li><a href="/faq/">よくあるご質問</a></li>
        <li><a href="/company/">会社案内</a></li><li><a href="/news/">ニュース</a></li><li><a href="/recruit/">採用情報</a></li>
      </ul></nav>
    </div></header>
    """
    assert _menus(html) == ["TOP", "強み", "FAQ"]


def test_sp_nav_attr_matching() -> None:
    assert WebCrawler.SP_NAV_ATTR_RE.search("sp_navi")
    assert WebCrawler.SP_NAV_ATTR_RE.search("siteheader__nav-sp")
    assert WebCrawler.SP_NAV_ATTR_RE.search("nav pc-none")
    assert not WebCrawler.SP_NAV_ATTR_RE.search("gnav sp-none")
    assert not WebCrawler.SP_NAV_ATTR_RE.search("special-menu")
    assert WebCrawler.PC_NAV_ATTR_RE.search("pc_menu align-right")
    assert not WebCrawler.PC_NAV_ATTR_RE.search("pc-hide")


def test_accordion_label_and_sibling_lists_with_same_class() -> None:
    """k-hoshinosato.jp の構造を簡略化したもの。

    - アコーディオンの親項目は<label>が見出し(子リンクの1つ目を拾わない)
    - 同じclassのリストに分けたナビはまとめる。SNSの項目は除く
    """
    html = """
    <header><nav class="l-gnav">
      <ul class="l-gnav__list">
        <li class="l-gnav__item"><div class="h_option">
          <input type="checkbox" id="toggle1"><label for="toggle1"><img alt="アイコン" src="i.png">概要一覧</label>
          <div class="content"><a href="/about"><p>星の里とは</p></a><a href="/company"><p>指定管理者情報</p></a></div>
        </div></li>
        <li class="l-gnav__item"><a href="/pricelist"><img alt="アイコン" src="i.png">ご利用料金一覧</a></li>
        <li class="l-gnav__item"><a href="/faq">よくあるご質問</a></li>
      </ul>
      <ul class="l-gnav__list">
        <li><a href="/access">アクセス</a></li>
        <li><a href="https://lin.ee/x">LINE</a></li>
        <li><a href="https://www.instagram.com/x/">Instagram</a></li>
      </ul>
      <ul class="sub-buttons"><li><a href="/contact">お問い合わせ</a></li><li><a href="/reserve">WEB予約</a></li><li><a href="/">トップへ戻る</a></li></ul>
    </nav></header>
    """
    assert _menus(html) == ["概要一覧", "ご利用料金一覧", "よくあるご質問", "アクセス"]


def test_text_only_labels_among_links() -> None:
    """intemjapan.co.jp の構造を簡略化したもの。子メニューを別の場所に置いた親項目は、liに文字列しか無い。

    検索欄・閉じるボタン等の文字列だけの項目は拾わない。
    """
    html = """
    <header><ul class="mainMenu">
      <li><span class="cont sub hasc1"><a href="/newslist/"><span>ニュース一覧</span></a></span></li>
      <li><span class="cont sub hasc2"><a href="/company/"><span>会社概要</span></a></span></li>
      <li><span class="cont sub hasc3"><i class="bx"></i><span>業務内容</span></span></li>
      <li><span class="cont sub hasc4"><a href="/csr/"><span>CSR事業</span></a></span></li>
      <li><span class="cont sub hasc5"><i class="bx"></i><span>採用情報</span></span></li>
      <li><form action="/search"><input type="text" name="s">SEARCH</form></li>
      <li>CLOSE</li>
    </ul></header>
    """
    assert _menus(html) == ["ニュース一覧", "会社概要", "業務内容", "CSR事業", "採用情報"]


def test_local_nav_in_main_is_not_used() -> None:
    """jefma.or.jp の構造を簡略化したもの。ヘッダーをJavaScriptで書き出しており、静的HTMLには本文中のローカルナビしか無い。"""
    html = """
    <body><div id="wrap"><main class="content"><section id="about">
      <nav class="local__nav"><ul>
        <li><a href="/about/greeting/">ご挨拶</a></li><li><a href="/about/purpose/">工業会の目的</a></li><li><a href="/about/history/">沿革</a></li>
      </ul></nav>
    </section></main></div></body>
    """
    assert _menus(html) == []


def test_main_wrapping_whole_page_is_not_excluded() -> None:
    """<main>が<header>ごとページ全体を包んでいる作りでは、ヘッダーのナビを採る。"""
    html = """
    <body><main>
      <header><nav class="gnav"><ul>
        <li><a href="/">ホーム</a></li><li><a href="/company/">会社概要</a></li><li><a href="/contact/">お問い合わせ</a></li>
      </ul></nav></header>
      <section><p>本文</p></section>
    </main></body>
    """
    assert _menus(html) == ["ホーム", "会社概要", "お問い合わせ"]


def test_links_in_dropdown_panel_without_list() -> None:
    """tokulabo.co.jp の構造を簡略化したもの。ul/liを使わないナビで、ドロップダウンの子リンクは項目にしない。"""
    html = """
    <header><nav class="navbar d-none d-xl-block"><div class="pc_menu">
      <span class="nav-item dropdown"><a class="nav-link dropdown-toggle" href="#">事業案内</a>
        <div class="dropdown-menu"><a href="/biz/">事業について</a><a href="/catalog/">製品カタログ</a></div></span>
      <a class="nav-link" href="/aboutus/">会社案内</a>
      <a class="nav-link" href="/contact/">お問い合わせ</a>
    </div></nav></header>
    """
    assert _menus(html) == ["事業案内", "会社案内", "お問い合わせ"]


def test_page_wrapper_with_side_class_is_not_sidebar_nav() -> None:
    """ページ全体を包む<div class="side">(本文を含む)はサイドバーではない。
    ヘッダーをJavaScriptで差し込むため静的HTMLの<header>が空でも、本文中のお知らせ一覧を拾わない。"""
    html = """
    <body><div class="side">
      <header id="header"></header>
      <div class="main"><main>
        <section class="info"><ul class="info_list">
          <li><a href="info/1"><time>2026.07.16</time><span>夏季休業のお知らせ</span></a></li>
          <li><a href="info/2"><time>2025.11.27</time><span>年末年始休業のお知らせ</span></a></li>
          <li><a href="info/3"><time>2025.10.29</time><span>お取引条件変更のご案内</span></a></li>
        </ul></section>
      </main></div>
    </div></body>
    """
    crawler = WebCrawler()
    assert crawler._extract_global_nav_menus(BeautifulSoup(html, "lxml"), "") == []
    assert crawler._has_empty_header(html)


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ('<header id="header"></header><main><p>本文</p></main>', True),
        ('<div id="header"> </div><p>本文</p>', True),
        ('<header><a href="/">ホーム</a></header>', False),
        ('<header><h1><img src="logo.png" alt=""></h1></header>', False),
        # 記事の見出し用の<header>はページのヘッダーではない
        ("<main><article><header></header></article></main>", False),
        ("<p>ヘッダーの無いページ</p>", False),
    ],
)
def test_has_empty_header(html: str, expected: bool) -> None:
    assert WebCrawler._has_empty_header(html) is expected


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        # WordPressのCSS・JavaScriptの"canvas"や、"premix"等の語の一部には反応しない
        ("<style>:root{--wp-editor-canvas-background:#ddd}</style><script>e.canvas.width</script><p>premixs</p>", ""),
        ('<link href="https://static.canva.com/x.css">', "canva"),
        ('<img src="https://framerusercontent.com/images/a.png">', "framerusercontent"),
        ("<script>window.__remixContext = {}</script>", "__remixcontext"),
    ],
)
def test_detect_js_framework_ignores_partial_words(html: str, expected: str) -> None:
    body = "<body>" + "<p>本文の段落です。</p>" * 30 + '<a href="/a">A</a><a href="/b">B</a><a href="/c">C</a><a href="/d">D</a></body>'
    assert WebCrawler()._detect_js_framework(html + body) == expected


def test_extracts_nav_from_menu_iframe() -> None:
    """kyoto-hokenkai.or.jp の構造を簡略化したもの。ナビを別ファイルにしてiframeで読み込む古い作り。"""
    import httpx

    top = """
    <body><table><tr><td><iframe src="parts/top.htm"></iframe></td></tr>
    <tr><td><iframe src="parts/menu.htm"></iframe></td></tr></table>
    <iframe src="https://www.youtube.com/embed/x"></iframe><p>本文</p></body>
    """
    pages = {
        "https://a.example/parts/top.htm": '<body><a href="/"><img alt="ロゴ"></a><a href="../mail-form.htm"><img alt="お問い合せ"></a></body>',
        "https://a.example/parts/menu.htm": """<body><table><tr>
            <td><a href="https://a.example/" target="_top"><b>トップ(紹介･概要)</b></a></td>
            <td><a href="../tomo/index.htm" target="_top"><b>友の会･基金</b></a></td>
            <td><a href="../koukai/index.htm" target="_top"><b>情報公開</b></a></td>
            <td><a href="../saiyou/index.htm" target="_top"><b>採用情報</b></a></td>
            </tr></table></body>""",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        return httpx.Response(200, text=pages[url], headers={"content-type": "text/html; charset=utf-8"}) if url in pages else httpx.Response(404)

    crawler = WebCrawler()
    soup = BeautifulSoup(top, HTML_PARSER)
    assert crawler._extract_global_nav_menus(soup, "") == []
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert crawler._extract_iframe_nav_menus(soup, "https://a.example/", client, "") == ["トップ(紹介･概要)", "友の会･基金", "情報公開", "採用情報"]


def test_menu_iframe_filter_skips_news_iframes() -> None:
    """お知らせ等のiframe(topics.html・headline.html)はナビのiframeとみなさず、取得もしない。"""
    import httpx

    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, text='<a href="/a">A</a><a href="/b">B</a><a href="/c">C</a>', headers={"content-type": "text/html"})

    soup = BeautifulSoup('<iframe src="topics.html"></iframe><iframe src="parts/headline.htm"></iframe><iframe src="/top.htm"></iframe>', HTML_PARSER)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert WebCrawler()._extract_iframe_nav_menus(soup, "https://a.example/", client, "") == []
    assert requested == []
