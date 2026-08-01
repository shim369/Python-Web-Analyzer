# クラス設計・モデル設計

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
* `rejection_reason`: 不可の理由 (M列 - 判定保留時（接続不可・ページ数極小・階層深すぎ）の理由や、`RenewalEvaluator` が返す判定理由（改行区切りで複数格納されうる）を格納)
* `remarks`: 備考 (N列 - **現状のコードでは常に空文字列 `""` が設定される。「クロールしたテキスト群から自動生成された特徴文」を出力する機能は未実装。** 将来的な実装候補として設計に残している状態)
* `operator_name`: 担当 (O列 - アプリ実行時に指定された担当者名をそのまま格納)

## 2. サービス層のインターフェース

### 2.1. `WebCrawler.crawl_and_analyze(start_url: str)`

戻り値は **9要素のtuple**（辞書ではない）。

```python
(
    total_pages,        # int | "100ページ以上"
    max_depth,          # int | "要確認"（階層10超過時）
    contact_fields,     # str（改行区切り）
    site_structure,     # str（改行区切り、最大10件）
    description,        # str（切り詰めなし）
    combined_html_src,  # str（巡回した全ページのHTMLを連結したもの。多言語/Lightbox/GSAP検知に利用）
    cms_name,            # str
    has_attachment,      # bool
    has_login,           # bool
)
```

呼び出し元（`SiteScraperService`）はこの順序でtupleアンパックを行う。順序を変更する場合は呼び出し元も同時に修正する必要がある。

### 2.2. `RenewalEvaluator.evaluate(...)`

戻り値は `tuple[str, str]`（判定結果, 理由文字列）。判定結果は **`"◯"` または `"×"` のみ**（`要確認`はこのメソッドの外側、`SiteScraperService`側で決定される）。
