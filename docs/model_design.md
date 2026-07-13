# 2. クラス設計・モデル設計 (Class & Model Design)

### 2.1. モデル設計 (Domain Models)

#### 2.1.1. `ScrapingJob` (一括処理ジョブモデル)

* `id`: ジョブID (PK)
* `operator_name`: 担当者名 (ExcelのN列用)
* `threshold_1`: ◎ 判定の最大ページ数 (デフォルト: 10)
* `threshold_2`: ◯ 判定の最大ページ数 (デフォルト: 15)
* `threshold_3`: △ 判定の最大ページ数 (デフォルト: 20)
* `status`: 状態（`pending`, `processing`, `completed`, `failed`）
* `created_at`: 実行日時

#### 2.1.2. `SiteAssessment` (サイト調査結果モデル)

Excelの1行分の解析データを保持するモデル。

* `id`: ID (PK)
* `job_id`: ジョブID (FK)
* `domain_name`: サイト名 (A列)
* `evaluation_result`: 調査結果 (B列)
* `has_ssl`: SSLあり (C列)
* `is_always_ssl`: SSL常時 (D列)
* `max_depth`: 階層数 (E列)
* `svcmd`: svcmd (F列 - 常に空)
* `site_structure`: 構成 (G列)
* `total_pages`: ページ数 (H列)
* `cms_name`: 使用CMS (I列)
* `description`: 用途 (J列)
* `contact_fields`: 問合せ項目 (K列)
* `rejection_reason`: 不可の理由 (L列)
* `remarks`: 備考 (M列)

### 2.2. サービス・ロジッククラス設計

#### 2.2.1. `ExcelService`

Excelファイルのパースおよび生成を担当。

* `import_excel(file_path, operator_name, thresholds)`: Excelからドメイン名を抽出し、パラメータを紐付けて `ScrapingJob` を生成。
* `export_excel(job_id)`: 指定ジョブの結果を要件通りのExcelフォーマット（B〜N列埋め）で生成・出力。

#### 2.2.2. `SiteScraperService`

対象サイトへリクエストを送り、HTMLを解析して生データを取得（A列のドメインには自動でプロトコル補完）。

* `fetch_site_info(domain)`: 10秒のタイムアウト制御付きで実行されるメインメソッド。
* `count_total_pages(domain)`: H列用（10秒制約）。
* `analyze_contact_page(domain)`: K列用（見つからない場合は早期に `None` を返す）。

#### 2.2.3. `RenewalEvaluator`

* `__init__(threshold_1, threshold_2, threshold_3)`: 動なしきい値をセット。
* `evaluate_rank(cms_name, total_pages)`: B列の「◎, ◯, △, ×」を動的に判定。
* `compile_rejection_reason(total_pages, has_login)`: L列の理由テキストを生成。
