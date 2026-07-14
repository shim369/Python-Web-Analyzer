# クラス設計・モデル設計

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
* `date_str`: 日付 (A列 - 例: `7/14`)
* `domain_name`: サイト名 (B列 - 旧A列)
* `evaluation_result`: 調査結果 (C列 - 旧B列)
* `has_ssl`: SSLあり (D列 - 旧C列)
* `is_always_ssl`: SSL常時 (E列 - 旧D列)
* `max_depth`: 階層数 (F列 - 旧E列)
* `svcmd`: svcmd (G列 - 常に空)
* `site_structure`: 構成 (H列 - 旧G列)
* `total_pages`: ページ数 (I列 - 旧H列)
* `cms_name`: 使用CMS (J列 - 旧I列)
* `description`: 用途 (K列 - 旧J列)
* `contact_fields`: 問合せ項目 (L列 - 旧K列)
* `rejection_reason`: 不可の理由 (M列 - 旧L列)
* `remarks`: 備考 (N列 - 旧M列)
* `operator_name`: 担当 (O列 - 旧N列)
