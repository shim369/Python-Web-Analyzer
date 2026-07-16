# Web Site Analyzer

Python学習およびポートフォリオ作成を目的として開発した、GUIベースのマルチドメインWebサイト解析・評価システムです。

実務における「非同期並行処理（マルチスレッド）」「堅牢なクローリングにおける防御機構の突破」「イミュータブル（不変）なドメインモデル設計」など、実践的なバックエンド・アーキテクチャを体系的に学ぶことを目的として開発しました。

---

## 概要

指定されたExcelリスト（B列にドメイン名が配置されたシート）から、複数ドメインの **SSL（HTTPS）対応状況、サイト構成（WordPress等の検出）、総ページ数** を自動的にクローリングし、定義したしきい値に基づきビジネス評価（◎, ◯, △）を自動付与するWebアプリケーションです。

単にデータを集めるスクレイパーにとどまらず、「サーバー負荷を最小化するクロールレート制御」「Cloudflareや各種クローラーブロック（403/503エラー）を回避するフェイクヘッダー」「HTTPフォールバック（TLSハンドシェイク失敗時の自動降格検知）」を独自実装。

フロントエンドには Streamlit を採用し、`st.rerun()` とバックグラウンドスレッドを協調させたポーリング機構により、1％刻みの進行状況を一切フリーズすることなく滑らかに描画するリアルタイム監視UIを実現しています。

---

## 主な機能

* **バックグラウンド並行クローリング（ノンブロッキング設計）**
  * `threading` を用いたバックグラウンドスレッドへの重いクローリング処理の完全委譲。
  * 重いネットワークI/O処理中も Streamlit の描画メインスレッドを100%解放し、画面フリーズを完全に防止。

* **インテリジェントなクローラー防御回避機構**
  * **HTTP Fallback 制御**: `https://` での接続（TLSハンドシェイク）が失敗した場合、即座に `http://` にフォールバックさせて接続可否を検証。SSL未対応なのかサーバー自体のダウンなのかを正確に判別。
  * **ブラウザ欺瞞（User-Agent）と通信偽装**: プレーンなPythonスクレイパーを即座にブロックする防御壁を突破するため、最新のWebブラウザ（Chrome/Firefox/Safari）のヘッダーおよび接続シーケンスを模倣。

* **WordPressおよびサイト構成の動的検出**
  * 静的なHTML構文解析だけでなく、`/wp-json/` エンドポイント、`/wp-includes/` 資産、ジェネレータータグ、および特徴的なCookieヘッダー等の複数シグナルから WordPress (WP) サイトを高精度に検出。

* **セッションステートを用いた堅牢なリアルタイムUI**
  * Streamlitのステートフル（`st.session_state`）な設計。
  * `while True` によるブロッキングループを排除し、`st.rerun()` とバックグラウンドキューによる「能動的非同期ポーリング」を採用することで、進捗表示とメトリクスカードをリアルタイム更新。

* **Mypy Strict適合を意識したイミュータブルモデル設計**
  * ドメインモデル `ScrapingJob` のプロパティをイミュータブル（Read-only）に設計し、スレッドセーフな状態管理を強制。
  * 状態の書き換えが必要な場合は、新しいインスタンスを再生成する「値オブジェクト」の設計思想を徹底。

---

## Screenshots

### Main Window

![Web Site Analyzer](docs/images/main.png)

---

## 使用技術

| 分類 | 技術 |
| --- | --- |
| Language | Python 3.12+ |
| Package Management | uv |
| Web UI Framework | Streamlit (Custom Flat UI with CSS styling) |
| Scraping & Parsing | HTTPX / Requests, BeautifulSoup4 |
| Data Processing | Pandas, OpenPyXL (Excel import / export) |
| Testing | Pytest |
| Linter & Formatter | Ruff |
| Type Check | Mypy (Strict仕様適合) |
| CI/CD | GitHub Actions |

---

## ディレクトリ構成

```text
PYTHON-WEB-ANALYZER/
├── docs/                      # ドキュメント関連ファイル
├── src/
│   └── web_analyzer/          # アプリケーションソースコード
│       ├── core/              # コアロジック
│       │   ├── crawler.py         # クローラー（Web探索・取得）
│       │   ├── evaluator.py       # 評価・判定ロジック
│       │   ├── excel_service.py   # Excel出力・フォーマット調整
│       │   ├── scraper_service.py # スクレイピング（テキスト抽出）
│       │   └── ssl_checker.py     # SSL/TLS証明書検証
│       ├── utils/             # 共通ユーティリティ関数
│       ├── __init__.py
│       ├── main.py            # アプリケーションのエントリーポイント（Streamlit UI）
│       └── models.py          # データモデル定義
├── temp/                      # 一時ファイル出力用ディレクトリ
├── tests/                     # ユニットテスト
│   ├── test_evaluator.py      # 評価ロジックのテスト
│   ├── test_excel_service.py  # Excel出力のテスト
│   └── test_ssl_checker.py    # SSL検証のテスト
├── .gitignore
├── .python-version
├── LICENSE                    # MITライセンス
├── pyproject.toml             # プロジェクト設定・依存関係定義
├── README.md                  # プロジェクト概要・評価者向け説明書
└── uv.lock                    # uvロックファイル（厳密なバージョンロック）

```

---

## 前提環境

* Python 3.12以上
* uv (一貫した開発・実行環境を再現するため必須)

`uv` がインストールされていない場合は以下を実行してください。

```bash
pip install uv

```

---

## インストールと環境再現 (uvによる依存関係ロック)

`uv` を用いて、配布環境や開発環境でのバージョンズレによるバグを100%防止し、一貫した実行環境を再現します。

```bash
git clone <repository-url>
cd PYTHON-WEB-ANALYZER

# uv を用いて lock ファイルに記録された依存関係を完全に同期 (自動で仮想環境が作成されます)
uv sync

```

---

## 起動方法

```bash
uv run streamlit run src/web_analyzer/main.py

```

---

## 品質管理・テスト

### テスト実行

```bash
uv run pytest

```

### Ruff (Linter & Formatter)

```bash
uv run ruff check .
uv run ruff format .

```

### Mypy (Type Check)

```bash
uv run mypy src tests

```

---

## GitHub Actions (CI)

GitHub Actionsを利用したCIパイプラインを構築しています。

ジョブを以下の2段階に分離し、静的解析（Lint/Type Check）に成功した場合のみテストを実行する構成としています。

### 1. Lint Job

* Ruff
* Mypy

### 2. Test Job

* Pytest

```yaml
test:
  needs: lint

```

上記により、Lintエラーが発生した場合は不要なテスト実行をスキップし、CIの実行リソースを節約します。

---

### ユニットテストの検証範囲

堅牢性を担保するため、`pytest` を用いたコアロジックの単体テストを徹底しています。

#### 1. 評価・判定ロジック (Evaluator)

* **しきい値評価の妥当性テスト**：指定されたしきい値（threshold_1, 2, 3）に基づいて、◎, ◯, × の判定が正確にマッピングされるかのマトリクステスト。
* **却下理由の自動生成検証**：ページ数過多や、外部非公開のログイン画面を検出した際に、評価結果に応じた適切な却下理由テキストが自動生成されるかの仕様検証。

#### 2. Excel入出力サービス (ExcelService)

* **データの双方向保証（I/Oテスト）**：解析結果モデル（SiteAssessment）のオブジェクト群を一度Excelファイルへエクスポートし、再度インポートした際に、データ（ドメイン名など）が欠損なく元の状態で完全復元されるかのラウンドトリップ検証。

#### 3. セキュリティ監査モジュール (SslChecker)

* **初期接続パラメーター検証**：接続試行時のデフォルトタイムアウト設定値の正常性検証。

---

## 本プロジェクトで学んだ高度な技術テーマ

### 1. スレッド間協調とノンブロッキングI/O

Streamlitは「上から下まで毎回全コードが再実行される」という特殊なライフサイクルを持っています。重いクローリング処理をメインスレッドでそのまま回すと、Web画面全体が数分間ホワイトアウトしてしまいます。
これを防ぐため、`threading.Thread` を使って実行ロジックをバックグラウンドに逃がし、メインスレッド側は `st.session_state` を媒介として「今どれくらい終わったか？」のみを安全にポーリング監視する非同期システムデザインを習得しました。

### 2. ネットワーク接続の堅牢性と欺瞞技術

実務におけるクローリングでは、通常のスクレイパーライブラリのデフォルトヘッダーは標的サーバー（特にWAFやCloudflare等）によって即座に拒否されます。
適切なタイムアウト設定、`User-Agent`の適切なローテーション、SSLハンドシェイクエラー発生時の「HTTPフォールバック（接続降格ポリシー）」の実装を通じて、商用レベルで耐えうる堅牢なネットワーククライアントの実装技術を習得しました。

### 3. イミュータブルモデルとMypyによる厳格な静的型付け

状態（State）を書き換えるコードはスレッドセーフティの観点からバグの温床になります。本システムでは、`ScrapingJob` のフィールドを意図的に読み取り専用（Read-only）にし、属性変更時にはオブジェクトを新しく構築し直すアプローチをとりました。これにより、並行処理下でも競合が発生しない、バグの入り込みにくいクリーンなドメインロジックを実現しています。

---

## License

MIT License
