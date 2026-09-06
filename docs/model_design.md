# クラス設計・モデル設計

## 0. モジュール配置

`ScrapingJob` / `SiteAssessment` を定義する `models.py` は `src/web_analyzer/models.py` に配置されている（`src/web_analyzer/core/models.py` ではない点に注意）。`core/`配下の各モジュール（`crawler.py`, `scraper_service.py`, `job_repository.py`等）はいずれも `from web_analyzer.models import ...` の形でインポートする。

## 1. モデル設計 (Domain Models)

### 1.1. `ScrapingJob` (一括処理ジョブモデル)

処理全体のステータスを管理するイミュータブル（不変）なデータモデル。

* `id`: ジョブID (UUID文字列)
* `operator_name`: 担当者名 (ExcelのO列に引き継がれる値)
* `page_threshold`: ◯ 判定の最大ページ数 (デフォルト: `10`)
  * ※旧仕様の `threshold_1`〜`threshold_3`（3段階の閾値）は廃止され、単一の閾値に統合されている。
  * ※現在のUI（`main.py`）にはこの値を入力させる要素が存在せず、常にデフォルト値 `10` で運用される。
* `status`: 状態（`pending`: 開始前, `processing`: 処理中, `completed`: 正常終了, `failed`: 異常終了, `interrupted`: 前回起動時に異常終了したまま残っていた状態）
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
* `rejection_reason`: 不可の理由 (M列 - 判定保留時（ネットワーク機器ブロック・接続不可・ページ数極小・階層深すぎ）の理由や、`RenewalEvaluator` が返す判定理由（改行区切りで複数格納されうる。ページ数超過／3階層以上／ログイン機能／添付機能／ベーシック認証／多言語対応／Captcha／カレンダー／サイト内検索／チャットボット／動画／フローティングボタン／アコーディオン／タブ切り替え／モーダル／Lightbox等／スクロールアニメーション、の中から該当するものすべて）を格納)
* `remarks`: 備考 (N列 - 通常は空文字列。移転案内ページを検知した場合は`SiteScraperService`が`"移転先：{URL}"`を設定する。それ以外の通常判定時は、クロール結果の`subsystem_note`（別システムらしきものが一部ディレクトリに同居している場合の注記）をそのまま転記し、判定保留の理由が`PENDING_TIMEOUT_REASON`（時間切れ）だった場合はM列の代わりにその定型文をここへ出力する。)
* `operator_name`: 担当 (O列 - アプリ実行時に指定された担当者名をそのまま格納)

## 2. サービス層のインターフェース

### 2.1. `WebCrawler.crawl_and_analyze(start_url: str)`

戻り値は **15要素のtuple**（辞書ではない）。`@measure_time`デコレータが付与されており、呼び出し1回ごとの所要時間がDEBUGログに出力される。

```python
(
    total_pages,          # int | "100ページ以上"
    max_depth,            # int | "要確認"（階層10超過時）
    contact_fields,       # str（改行区切り）
    site_structure,       # str（改行区切り、最大10件）
    description,          # str（切り詰めなし）
    combined_html_src,    # str（巡回した全ページのHTMLを連結したもの。多言語/Lightbox/GSAP/Captcha/カレンダー等の検知に利用）
    cms_name,             # str
    has_attachment,       # bool
    has_login,            # bool
    has_basic_auth,       # bool（クロール中に401 Unauthorizedを検知したページが1件でもあればTrue）
    has_multilang,        # bool（ヘッダー等に配置された多言語切り替えUIをクロール中に検知していればTrue）
    blocked_reason,       # str（Fortinet等のネットワークセキュリティ機器によるブロックページを検知した場合のみ非空）
    redirect_target_url,  # str（meta refresh/JSタイマーによる別ドメインへの自動リダイレクト＝移転案内ページを検知した場合のみ非空）
    queue_exhausted,      # bool（巡回対象の内部リンクをすべて発見・訪問し尽くして自然にキューが空になった場合True）
    subsystem_note,       # str（サイトの一部ディレクトリに別システムらしきものが同居し、ページ数の大半を占めている場合の注記。該当なしは空文字）
)
```

呼び出し元（`SiteScraperService`）はこの順序でtupleアンパックを行う。順序や要素数を変更する場合は、呼び出し元・関数の戻り値型ヒント（`tuple[...]`）の両方を同時に修正する必要がある（型ヒントだけ更新が漏れると、実際に返す要素数と宣言が食い違い、mypyの`Incompatible return value type`エラーになる）。

* `has_login`は、`_has_password_login_form`によって検知される。パスワード入力欄（`type="password"`、または`autocomplete="current-password"/"new-password"`）を持つフォームが、サイト共通のヘッダー/グローバルナビ（`<header>`要素、または`_HEADER_LIKE_RE`＝`header|gnav|globalnav|utility|top-?bar|topnav|l-header`にマッチするclass/id）を伴って表示されている場合にTrueとなる。CMS管理画面のログインページを個別列挙して除外する方式は対象CMSが増えるたびにメンテナンスが必要になるため採用せず、「訪問者向けの会員ログイン・マイページは通常のページテンプレートに組み込まれるためサイト共通ヘッダーを伴うが、管理者ログイン画面は単独ページとして表示されヘッダーを伴わない」という構造的な違いで区別している。`login`/`mypage`/`member`/`account`等の文字列はログイン判定そのものには使われておらず、クロールキューの巡回優先度付けにのみ使用される。
* `has_basic_auth`は、初回アクセスおよびクロールループ内の各ページ取得時、`response.status_code == 401`を`raise_for_status()`呼び出し前に確認することで検知する。サイト全体がベーシック認証で保護されている場合だけでなく、一部の下層ページのみが保護されている場合も検知できる。
* `blocked_reason`は、取得したHTMLがFortinet Webfilter等のネットワークセキュリティ機器によるブロックページ（実サイトの内容ではない）であるかどうかを判定して設定される。SSL証明書エラーが引き金でこの警告ページが返されているケースを区別して分類する処理を含む。設定された時点で、通常の巡回・判定ロジックには進まず即座に結果を返す。
* `redirect_target_url`は、取得したHTMLがmeta refreshやJSの`location.href`書き換え等によって数秒後に別ドメインへ自動遷移する「移転案内ページ」かどうかを検知して設定される。設定された時点で即座に結果を返す。
* `queue_exhausted`は、巡回対象として発見した内部リンクをすべて訪問し終えてキューが自然に空になったか（`True`）、タイムアウトや最大ページ数（100件）到達等で未訪問のリンクを残したまま打ち切ったか（`False`）を表す。「元々ページ数の少ないサイト」なのか「クロールが途中で終わっただけ」なのかを`evaluator.py`側で区別するために使う。
* `subsystem_note`は、サイト全体のごく一部のディレクトリ配下に別システムらしきものが同居し、そこだけでページ数の大半を占めている場合の注記文字列。ページ数・階層数の集計や判定そのものには影響せず、`SiteScraperService`が通常判定時のN列（備考）にそのまま転記する参考情報。

### 2.2. `RenewalEvaluator`

`decide()`と`evaluate()`の2段構えになっている。

**`decide(total_pages, max_depth, has_login, has_attachment, has_basic_auth=False, has_multilang=False, html_src="", page_threshold=10, queue_exhausted=False) -> tuple[str, str]`**

C列（調査結果）全体、すなわち`"◯"` / `"×"` / `"要確認"`の3値すべてを決定する窓口メソッド。`total_pages`/`max_depth`は`int | str`で受け取り、クローラーの生の戻り値（`"100以上"`のような下限値表記や、`max_depth`が文字列`"要確認"`のケースを含む）をそのまま渡せる。判定は次の順で行う。

1. `total_pages == 0` の場合、`evaluate()`へは進まず即座に確定する。
   * `has_basic_auth=True` → 「ベーシック認証がかかっているページがあるため」で**`"×"`**（接続不可の汎用メッセージより、判定可能な明確な理由を優先する）
   * それ以外 → 「接続不可またはアクセス拒否のため、判定を保留しました。」で**`"要確認"`**
2. 上記に該当しなければ、`total_pages`/`max_depth`を`parse_lower_bound()`で数値化した上で必ず`evaluate()`を実行する。`parse_lower_bound()`は`int`値、または`"{数値}以上"`形式の文字列を(数値, 信頼できる下限値か)に変換し、`"要確認"`のような下限値表記を伴わない文字列は信頼できない値として扱う。
3. `evaluate()`の結果が**`"×"`**であれば、`queue_exhausted`等の状態に関わらずそのままその理由で確定する。ページ数・階層数・機能検知など、一度確定した`×`の根拠はクロールを続けても覆らないため。
4. `evaluate()`が`"×"`にならなかった場合、以下の順で「続きを巡回すれば結果が変わったかもしれない」不確実性を確認し、該当すれば`"要確認"`に倒す。
   * ページ数が信頼できる下限値でない → `PENDING_TIMEOUT_REASON`（クロールが時間切れで途中終了した旨の定型文）
   * 階層数が信頼できる下限値でない（`max_depth == "要確認"`、階層10超過相当） → 「サイト階層が深すぎるため、別途サイトエクスプローラー等での確認をお願いします。」
   * `queue_exhausted=False`（内部リンクを巡回し尽くさずに打ち切られた） → `PENDING_TIMEOUT_REASON`
5. いずれにも該当しなければ、`evaluate()`の結果（`"◯"`）をそのまま返す。

`PENDING_TIMEOUT_REASON`はM列（不可の理由）には出力されない。`SiteScraperService`側でこの定型文だけを判定し、M列を空にした上でN列（備考）へ振り替える（3.1節参照）。

`SiteScraperService`は`evaluate()`を直接呼ばず、この`decide()`のみを呼ぶ。ただし、`decide()`を呼ぶ前段階として、`SiteScraperService`自身が`blocked_reason`（ネットワーク機器ブロック）と`redirect_target_url`（移転案内ページ）の有無を先にチェックしており、いずれかが非空であればそちらを優先して結果を確定させ、`decide()`自体を呼ばない（詳細は3.1節参照）。「`要確認`に倒すかどうか」の判断は`evaluator.py`単体に集約されており、`SiteScraperService`側にはこの種の判断ロジックを持たせていない。これにより`test_evaluator.py`でカバーできる。

**`evaluate(total_pages, max_depth, has_login, has_attachment, has_basic_auth=False, has_multilang=False, html_src="", page_threshold=10) -> tuple[str, str]`**

`"要確認"`に倒す特殊ケースを考慮しない、純粋な◯/×判定ロジック。`total_pages`/`max_depth`はどちらも`int`（`decide()`側で変換済みの値）を受け取る。`html_src`は`<body>`要素の中身のみを対象に判定される（`<link>`/`<script>`/`<style>`/`<meta>`等のアセットタグは各種セレクタ判定から除外）。

判定される項目と対応するロジック:

| 判定項目 | 検知方法 |
| --- | --- |
| ページ数超過 | `total_pages > page_threshold` |
| 3階層以上 | `max_depth > 2`（1始まりの表示用深さを前提） |
| ログイン機能 | 呼び出し元から渡される`has_login`（クローラー側で判定済み） |
| 添付機能 | 呼び出し元から渡される`has_attachment`（クローラー側で判定済み） |
| ベーシック認証 | 呼び出し元から渡される`has_basic_auth`（クローラー側で判定済み） |
| 多言語対応 | 呼び出し元から渡される`has_multilang`（クローラー側で判定済み）に加え、`has_multilang=False`だった場合でもHTML内の言語切り替えUI（`_has_multilang`）を独自に再チェックする二重判定 |
| Captcha | `CAPTCHA_KEYWORDS`（画像認証プラグイン固有の識別子）に加え、`.g-recaptcha`/`.h-captcha`/`class*='captcha'`等のセレクタ |
| チャットボット | `CHATBOT_KEYWORDS`（sinclo, chamo, Zendesk, channel.io, HubSpot Conversations, Intercom, Crisp, Tidio, ChatPlus, Zoho SalesIQ 等、埋め込み時にしか出現しない固有ドメイン・識別子） |
| カレンダー | `.wp-calendar`/`.xo-event-calendar`/`iframe[src*='calendar.google']`等のセレクタ（`CALENDAR_KEYWORDS`はクラス名の一部として利用） |
| サイト内検索 | `input[type="search"]`の存在、または`SEARCH_KEYWORDS`（「絞り込み検索」「条件検索」等の文言） |
| 動画 | `<video>`タグ、または`iframe`のsrcにYouTube/Vimeo埋め込みを含む |
| フローティングボタン | クラス名に`floating`/`fixed-btn`/`follow-window`を含む、またはインラインスタイルに`position:fixed`を含む要素の存在（専用のキーワード定数は持たず、`_has_floating`メソッド内で直接判定） |
| アコーディオン | `.accordion`/`[data-bs-toggle='collapse']`/`<details>`等のセレクタ（`_has_accordion`） |
| タブ切り替え | `[role='tab']`/`.nav-tabs`/`[data-bs-toggle='tab']`等のセレクタ（`_has_tabs`） |
| モーダル | `.modal`/`.modal-dialog`/`[data-bs-toggle='modal']`等のセレクタ（`_has_modal`） |
| ギャラリー（Lightbox等） | `RICH_UI_KEYWORDS`をクラス名の一部として用いたセレクタ（`.lightbox`/`.fancybox`/`[data-lightbox]`等） |
| スクロールアニメーション | `SCROLL_ANIMATION_KEYWORDS`（gsap, scrolltrigger, data-aos, locomotive-scroll） |

理由テキストの生成に使われるキーワード等はクラス変数として`RenewalEvaluator`に集約されているが、**実際にクラス変数として定義されているのは`CAPTCHA_KEYWORDS` / `CHATBOT_KEYWORDS` / `CALENDAR_KEYWORDS` / `SEARCH_KEYWORDS` / `RICH_UI_KEYWORDS` / `MULTILANG_KEYWORDS` / `SCROLL_ANIMATION_KEYWORDS` / `VIDEO_KEYWORDS`のみ**である点に注意。フローティングボタン・アコーディオン・タブ・モーダルの4項目については、専用のキーワード定数（`FLOATING_BUTTON_KEYWORDS`等）は存在せず、各判定メソッド内でCSSセレクタを直接指定する形で実装されている（誤検知回避のため、当初1つのキーワードリストにまとめていたものを機能ごとに独立させた経緯がある）。

`PDF_LINK_THRESHOLD`（PDFリンク数によるNG判定）、および「ステップ型フォーム」の検知は、**現状のコードには実装されていない。**

Google Map埋め込みは、ほぼすべてのコーポレートサイトのアクセスページに存在し単独では判定基準として機能しにくいため、検知対象から意図的に除外されている。同様に、採用ページの有無やブログ・実績ページのボリュームによる判定も、検討の結果採用を見送っている。

## 3. オーケストレーション層・永続化層

### 3.1. `SiteScraperService`（並列オーケストレーション、v2）

1ジョブ全体を統括するバックグラウンドスレッドを起動し、その内部で`ThreadPoolExecutor`（`DEFAULT_MAX_WORKERS = 5`）を用いてジョブ内の各ドメインを同時処理する。`render_js=True`でのPlaywright起動はワーカーごとに個別のヘッドレスChromiumプロセスを立ち上げるため、並列数を上げすぎるとメモリ・CPU負荷が急増する。5並列はその安定性とスループットのバランスを取った経験則値。

`_process_single_assessment`が1ドメイン分の処理単位であり、以下の順で実行される。

1. `SslChecker.check_ssl_status()`でSSL/常時SSL判定
2. `WebCrawler(render_js=True, timeout=150.0, page_timeout=8.0).crawl_and_analyze()`でクロール（JSフレームワーク検知でPlaywrightレンダリングが走ると1回あたり15秒前後かかる上、記事系ページを大量に持つサイトでは90秒では実ページ数を大きく下回る事例があったため長めに確保。ただし300秒まで引き上げると並列ワーカー1枠を長時間占有し他ドメインの処理を押し出してしまったため、100ページ到達までの余裕とスループットのバランスを取って150秒に調整している）
3. クロール結果に`redirect_target_url`（移転案内ページ）があれば、`RenewalEvaluator.decide()`を呼ばずに`"×"`（すでにリニューアル済のため）を確定し、備考欄に移転先を記録
4. そうでなく`blocked_reason`（ネットワーク機器ブロック）があれば、同様に`decide()`を呼ばずに`"要確認"`＋検知した理由を確定
5. どちらでもなければ`RenewalEvaluator.decide()`を呼び出し、通常の判定フローに委ねる。返ってきた`rejection_reason`が`PENDING_TIMEOUT_REASON`と一致する場合はM列を空にしてN列（備考）へ振り替え、それ以外の場合はクロール結果の`subsystem_note`をN列にそのまま転記する
6. 結果を`threading.Lock`配下で`SiteAssessment`インスタンスへスレッドセーフに書き込み
7. `JobRepository.update_assessment()`でSQLiteへ即座に反映

1ドメインの処理中に予期しない例外が発生した場合、`as_completed`ループ側でこれを捕捉し、そのドメインの結果を`"要確認"`（処理中に予期せぬエラーが発生しました）としてログに記録した上で、他のドメインの処理・ジョブ全体の継続は妨げない。`ThreadPoolExecutor`自体の起動に関わる致命的な例外のみ、ジョブ全体を`"failed"`にする。

### 3.2. `JobRepository`（SQLite永続化）

`ScrapingJob`と`SiteAssessment`をSQLite（`data/jobs.db`、WALモード）へ永続化するリポジトリ。

* WALモードにより、書き込み中にプロセスが強制終了されてもDBファイル自体は破損しない（コミット前の変更が破棄されるのみ）。
* 複数ワーカースレッドから同時に呼び出されるため、`check_same_thread=False`で接続を張った上で、書き込みは`JobRepository`内部の`threading.Lock`で直列化する。
* `save_job` / `save_assessments`: ジョブ開始時に、ジョブ本体と全ドメインの初期状態（未処理）をまとめて保存する。
* `update_assessment`: 1ドメインの解析完了時に、その1行だけを更新する。ワーカースレッドが1件処理し終えるたびに呼ばれるため、ジョブが全件終わる前にプロセスが落ちても、そこまで完了した分はディスクに残る。
* `reconcile_interrupted_jobs`: アプリ起動時に一度だけ呼び出す。前回`processing`のまま残っている（＝Ctrl+C等で異常終了した）ジョブを検出し、`interrupted`状態に修正する。これを行わないと、UIが「処理実行中...」の表示のまま完了しないゾンビ状態になる。

`main.py`側では、`st.cache_resource`でプロセス全体につき1つだけ`SiteScraperService`（および内部の`JobRepository`）を生成・共有する。これにより、ブラウザが完全にリロードされて新しいセッションが作られても、サーバープロセス自体が生きている限りジョブの進捗・結果を保持できる。`SiteScraperService.get_job_progress`は、まずインメモリキャッシュを参照し、無ければ（プロセス再起動直後や別セッションからの初回参照時）`JobRepository`からDBの内容を復元してキャッシュに載せる。

## 4. ユーティリティ層 (`utils/`)

### 4.1. `utils/logger.py`

`setup_logger(level: int = logging.DEBUG)` を`main.py`の起動時（`st.session_state`によるガード付きで1回のみ）呼び出すことで、実行ごとに「年月日時分秒」までをファイル名に組み込んだ個別のログファイル（例: `logs/app_20260803_162005.log`）を生成し、ルートロガーに対してコンソール出力（INFOレベル）とファイル出力（DEBUGレベル、`mode="w"`で毎回新規作成）の両方を設定する。`dictConfig`ベースで、`disable_existing_loggers: False`により他ライブラリのロガーを無効化しない。各モジュールは`logging.getLogger(__name__)`で個別ロガーを取得するだけでよく、`scraper_service.py`が独自の`logging.basicConfig(...)`を呼び出すことはない（二重設定の防止）。

### 4.2. `utils/decorators.py`

* `measure_time`: 関数の実行時間をDEBUGログに出力するデコレータ。`WebCrawler.crawl_and_analyze`と`SslChecker.check_ssl_status`に適用されている。
* `log_action(action_name: str)`: 処理の開始・完了をINFOログに出力するデコレータ。`ExcelService.export_excel`に適用されている。

いずれも汎用的な薄いラッパーであり、特定のプロジェクト固有ロジックは含まない。Python 3.12の型パラメータ構文（`def measure_time[F: Callable[..., Any]](func: F) -> F`）を用いて実装されている。
