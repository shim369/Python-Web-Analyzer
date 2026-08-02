# クラス設計・モデル設計

## 0. モジュール配置

`ScrapingJob` / `SiteAssessment` / `LOGIN_KEYWORDS` を定義する `models.py` は `src/web_analyzer/models.py` に配置されている（`src/web_analyzer/core/models.py` ではない点に注意）。`core/`配下の各モジュール（`crawler.py`, `scraper_service.py`等）はいずれも `from web_analyzer.models import ...` の形でインポートする。

## 1. モデル設計 (Domain Models)

### 1.1. `ScrapingJob` (一括処理ジョブモデル)

処理全体のステータスを管理するイミュータブル（不変）なデータモデル。

* `id`: ジョブID (UUID文字列)
* `operator_name`: 担当者名 (ExcelのO列に引き継がれる値)
* `page_threshold`: ◯ 判定の最大ページ数 (デフォルト: `10`)
  * ※旧仕様の `threshold_1`〜`threshold_3`（3段階の閾値）は廃止され、単一の閾値に統合されている。
  * ※現在のUI（`main.py`）にはこの値を入力させる要素が存在せず、常にデフォルト値 `10` で運用される。
* `status`: 状態（`pending`: 開始前, `processing`: 処理中, `completed`: 正常終了, `failed`: 異常終了）
* `created_at`: 実行日時 (`datetime` 型。`field(default_factory=datetime.now)` により、インスタンス生成の都度、現在時刻で初期化される)

### 1.2. `SiteAssessment` (サイト調査結果モデル)

Excelの1行分の解析・調査結果を保持するミュータブル（可変）なデータモデル。バックグラウンド処理によって各フィールドが順次書き換えられる。

* `id`: 調査レコードID (UUID文字列)
* `job_id`: 紐付くジョブのID (FK)
* `date_str`: 日付 (A列 - アプリ実行時の日付を `M/D` 形式で格納)
* `domain_name`: サイト名 (B列 - インポートされたドメイン名)
* `evaluation_result`: 調査結果 (C列 - **`◯`, `×`, `要確認`** の3値。旧仕様の `◎`/`△` は廃止。判定ロジックの詳細は要件定義書を参照)
* `has_ssl`: SSLあり (D列 - 有効なSSL証明書が確認できれば **`あり`**、不可なら **`なし`**、判定不能時は空欄)
* `is_always_ssl`: SSL常時 (E列 - `http://` から `https://` への自動リダイレクトが確認できれば **`◯`**、そうでなければ **`×`**、判定不能時は空欄)
* `max_depth`: 階層数 (F列 - `int | None` 型。ただし実際にクローラーから返る値は `int` または文字列 **`"要確認"`**（階層10超過時）であるため、この列には数値以外の文字列が入り得る点に注意)
* `svcmd`: svcmd (G列 - 常に**空欄**を維持)
* `site_structure`: 構成 (H列 - 自動検出したグローバルナビゲーションのメニューテキストを最大10件、**改行区切り**で格納。多言語切り替えメニュー等は除外)
* `total_pages`: ページ数 (I列 - `int | None` 型。巡回できた総ページ数。接続失敗時は `0`。※クロール中に100ページへ到達した場合はクローラーが `"100ページ以上"` という文字列を返し、`total_pages` には `100` が、Excel出力用の表示値には `"100以上"` が格納される)
* `cms_name`: 使用CMS (J列 - HTMLシグネチャから幅広い国内外CMSを多角的に判定。WordPress, baserCMS, EC-CUBE, Movable Type, MODX, Drupal, Joomla!, TYPO3, concrete5, XOOPS, NetCommons, PowerCMS, Craft CMS, Sitecore, Kentico, SilverStripe, Jimdo, Wix, Shopify, ColorMe Shop, MakeShop, futureshop, a-blog cms, RCMS, HeartCore, BlueMonkey, BiNDup 等を検知し、非CMSなら空文字列)
* `description`: 用途 (K列 - メタディスクリプション ➔ `<title>` ➔ `<h1>` の優先度順で抽出したWebサイトの概要文。**現状のコードには文字数の切り詰め処理はなく、抽出した文字列がそのまま格納される**）
* `contact_fields`: 問合せ項目 (L列 - フォーム内のinput/textarea/selectのラベルをaria属性・label紐付け・祖先要素探索などから抽出し、項目名を**改行区切り**で格納。HubSpot/Tayori等の外部埋め込みフォームを検出した場合はその旨のテキストを格納。iframe内フォームも最大2階層まで再帰的に解析)
* `rejection_reason`: 不可の理由 (M列 - 判定保留時（接続不可・ページ数極小・階層深すぎ）の理由や、`RenewalEvaluator` が返す判定理由（改行区切りで複数格納されうる。ページ数超過／3階層以上／ログイン機能／添付機能／ベーシック認証／Captcha／ステップ型フォーム／カレンダー／サイト内検索／チャットボット／PDF資料多数／Lightbox等／多言語対応／スクロールアニメーション、の中から該当するものすべて）を格納)
* `remarks`: 備考 (N列 - **現状のコードでは常に空文字列 `""` が設定される。「クロールしたテキスト群から自動生成された特徴文」を出力する機能は未実装。** 将来的な実装候補として設計に残している状態)
* `operator_name`: 担当 (O列 - アプリ実行時に指定された担当者名をそのまま格納)

## 2. サービス層のインターフェース

### 2.1. `WebCrawler.crawl_and_analyze(start_url: str)`

戻り値は **10要素のtuple**（辞書ではない）。`@measure_time`デコレータが付与されており、呼び出し1回ごとの所要時間がDEBUGログに出力される。

```python
(
    total_pages,        # int | "100ページ以上"
    max_depth,          # int | "要確認"（階層10超過時）
    contact_fields,     # str（改行区切り）
    site_structure,     # str（改行区切り、最大10件）
    description,        # str（切り詰めなし）
    combined_html_src,  # str（巡回した全ページのHTMLを連結したもの。多言語/Lightbox/GSAP/Captcha/カレンダー等の検知に利用）
    cms_name,           # str
    has_attachment,     # bool
    has_login,          # bool
    has_basic_auth,     # bool（クロール中に401 Unauthorizedを検知したページが1件でもあればTrue）
)
```

呼び出し元（`SiteScraperService`）はこの順序でtupleアンパックを行う。順序や要素数を変更する場合は、呼び出し元・関数の戻り値型ヒント（`tuple[...]`）の両方を同時に修正する必要がある（型ヒントだけ更新が漏れると、実際に返す要素数と宣言が食い違い、mypyの`Incompatible return value type`エラーになる）。

`has_basic_auth`は、初回アクセス（`primary_url`/`fallback_url`）およびクロールループ内の各ページ取得時、`response.status_code == 401`を`raise_for_status()`呼び出し前に確認することで検知する。サイト全体がベーシック認証で保護されている場合だけでなく、一部の下層ページのみが保護されている場合も検知できる。

### 2.2. `RenewalEvaluator`

`decide()`と`evaluate()`の2段構えになっている。

**`decide(total_pages, max_depth, has_login, has_attachment, has_basic_auth=False, html_src="", page_threshold=10) -> tuple[str, str]`**

C列（調査結果）全体、すなわち`"◯"` / `"×"` / `"要確認"`の3値すべてを決定する窓口メソッド。`total_pages`/`max_depth`は`int | str`で受け取り、クローラーの生の戻り値（`max_depth`が文字列`"要確認"`のケースを含む）をそのまま渡せる。以下の判定を`evaluate()`より先に行い、該当すれば即座に`"要確認"`とその理由を返す。

1. `total_pages == 0` → 「接続不可またはアクセス拒否のため、判定を保留しました。」
2. `1 <= total_pages <= 2` → 「クロールできたページ数が極端に少ないため判定を保留しました (取得数: {n}ページ)。」
3. `max_depth == "要確認"`（階層10超過） → 「サイト階層が深すぎるため、別途サイトエクスプローラー等での確認をお願いします。」

いずれにも該当しなければ、`max_depth`を`int`に変換した上で内部的に`evaluate()`を呼び出し、その結果をそのまま返す。

`SiteScraperService`は`evaluate()`を直接呼ばず、この`decide()`のみを呼ぶ。以前は「`要確認`に倒すかどうか」の分岐が`scraper_service.py`側にif/elifの連なりとして存在しており、判定ロジックが2ファイルに分散していた（かつその分岐にはユニットテストが存在しなかった）。`decide()`への集約により、判定ロジック全体が`evaluator.py`単体で完結し、`test_evaluator.py`でカバーできるようになっている。

**`evaluate(total_pages, max_depth, has_login, has_attachment, has_basic_auth=False, html_src="", page_threshold=10) -> tuple[str, str]`**

`"要確認"`に倒す特殊ケースを考慮しない、純粋な◯/×判定ロジック。`total_pages`/`max_depth`はどちらも`int`（`decide()`側で変換済みの値）を受け取る。`html_src`は関数冒頭で`.lower()`を1回だけ計算し、以降の全キーワード判定で使い回す（判定のたびに毎回`.lower()`し直すことを避けるため）。

理由テキストの生成に使われる判定基準（キーワード等）はクラス変数として`RenewalEvaluator`に集約されている。

* `CAPTCHA_KEYWORDS`: 画像認証（Captcha）検知用
* `CHATBOT_KEYWORDS`: 主要チャットボットサービス（sinclo, chamo, zendesk, channel.io, hubspot-messages等）検知用
* `CALENDAR_KEYWORDS`: カレンダー機能（wp-calendar, xo-event-calendar等）検知用
* `SEARCH_KEYWORDS`: サイト内検索・絞り込み検索検知用
* `RICH_UI_KEYWORDS`: Lightbox等のギャラリー機能検知用
* `MULTILANG_KEYWORDS`: 多言語切り替え機能検知用
* `SCROLL_ANIMATION_KEYWORDS`: GSAP等のスクロールアニメーション検知用
* `VIDEO_KEYWORDS`: `<video>`タグ・YouTube/Vimeo埋め込み検知用
* `FLOATING_BUTTON_KEYWORDS`: 画面追従ボタン検知用
* `ACCORDION_MODAL_KEYWORDS`: アコーディオン／タブ切り替え／モーダル検知用
* `RECRUIT_KEYWORDS`: 採用（リクルート）ページ検知用
* `BLOG_NEWS_KEYWORDS`: ブログ・お知らせ系リンク検知用（`BLOG_NEWS_LINK_THRESHOLD`件以上の出現で該当）
* `WORKS_KEYWORDS`: 製品・実績系リンク検知用（`WORKS_LINK_THRESHOLD`件以上の出現で該当）
* `PDF_LINK_THRESHOLD` / `BLOG_NEWS_LINK_THRESHOLD` / `WORKS_LINK_THRESHOLD`: いずれもデフォルト5。`combined_html_src`内での該当文字列の出現回数がこの値以上であれば「多い」と判定する（巡回ページ数そのものをカウントしているわけではなく、該当リンク文字列の出現回数を代理指標として利用している点に注意）。

Google Map埋め込みは、ほぼすべてのコーポレートサイトのアクセスページに存在し単独では判定基準として機能しにくいため、検知対象から意図的に除外されている。

## 3. ユーティリティ層 (`utils/`)

### 3.1. `utils/logger.py`

`setup_logger(level: int = logging.INFO)` を`main.py`の起動時（`st.session_state`によるガード付きで1回のみ）呼び出すことで、ルートロガーに対してコンソール出力とファイル出力（`logs/app_YYYYMMDD.log`）の両方を設定する。`dictConfig`ベースで、`disable_existing_loggers: False`により他ライブラリのロガーを無効化しない。各モジュールは`logging.getLogger(__name__)`で個別ロガーを取得するだけでよく、`scraper_service.py`が以前行っていた独自の`logging.basicConfig(...)`呼び出しは削除済み（二重設定の防止）。

### 3.2. `utils/decorators.py`

* `measure_time`: 関数の実行時間をDEBUGログに出力するデコレータ。`WebCrawler.crawl_and_analyze`と`SslChecker.check_ssl_status`に適用されている。
* `log_action(action_name: str)`: 処理の開始・完了をINFOログに出力するデコレータ。`ExcelService.export_excel`に適用されている。

いずれも汎用的な薄いラッパーであり、特定のプロジェクト固有ロジック（旧版に含まれていた言語仕様検証コード等）は含まない。
