# クラス設計・モデル設計

## 1. モデル設計 (Domain Models)

### 1.1. `ScrapingJob` (一括処理ジョブモデル)

* `id`: ジョブID (PK)
* `operator_name`: 担当者名 (ExcelのO列用)
* `threshold_1`: ◎ 判定の最大ページ数 (デフォルト: 10)
* `threshold_2`: ◯ 判定の最大ページ数 (デフォルト: 15)
* `threshold_3`: △ 判定の最大ページ数 (デフォルト: 20)
* `status`: 状態（`pending`, `processing`, `completed`, `failed`）
* `created_at`: 実行日時

### 1.2. `SiteAssessment` (サイト調査結果モデル)

Excelの1行分の解析データを保持するモデル。

* `id`: ID (PK)
* `job_id`: ジョブID (FK)
* `date_str`: 日付 (A列 - 例: `7/16`)
* `domain_name`: サイト名 (B列 - ドメインそのまま、または www 自動吸収)
* `evaluation_result`: 調査結果 (C列 - `◎`, `◯`, `△`, `×` に加え、ページ極小時に `要確認` を追加)
* `has_ssl`: SSLあり (D列)
* `is_always_ssl`: SSL常時 (E列)
* `max_depth`: 階層数 (F列)
* `svcmd`: svcmd (G列 - 常に空)
* `site_structure`: 構成 (H列 - グローバルナビゲーション項目を改行区切りで表示)
* `total_pages`: ページ数 (I列)
* `cms_name`: 使用CMS (J列 - WordPress、Shopify、microCMS等の優先高精度検知)
* `description`: 用途 (K列 - Description ➔ Title ➔ H1 の高精度フォールバック抽出、最大100文字)
* `contact_fields`: 問合せ項目 (L列 - Formrun, Tayori, Googleフォームなどの埋め込み自動検知、日本語リンク巡回)
* `rejection_reason`: 不可の理由 (M列 - ページ数超過、会員ログイン検知、または要確認時の理由)
* `remarks`: 備考 (N列 - クロール中HTML解析によるサイトの特徴文。WP運用、採用注力、EC機能、SNS連携を自動合成)
* `operator_name`: 担当 (O列)
