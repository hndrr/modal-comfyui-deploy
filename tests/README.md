# テストと CI

本リポジトリにおけるローカルテストの実行方法、テストの設計原則、および GitHub Actions（CI）での自動テスト実行条件について説明します。

すべてのテストスイートは `scripts/test_standalone.py` を共通ランナーとして実行でき、非公開の外部拡張を別途インストールする必要はありません。

---

## 目次

1. [テストスイートと検証対象](#テストスイートと検証対象)
2. [ローカルでのテスト実行方法](#ローカルでのテスト実行方法)
3. [テストの設計原則と運用方針](#テストの設計原則と運用方針)
4. [GitHub Actions（CI）の実行条件と差分判定](#github-actionsciの実行条件と差分判定)

---

## テストスイートと検証対象

| ディレクトリ | 主な検証対象 |
| :--- | :--- |
| `tests/standard/` | 通常構成（`comfyapp.py`）の設定、ComfyUI Manager 連携、WebSocket 圧縮無効化パッチ、セットアップスクリプトの認証設定・設定保持・失敗時の停止 |
| `tests/split/` | CPU/GPU 分離構成（`splitapp.py`）の Journal、Gateway、GPU worker、起動・CPU Snapshot 復元、Volume 整合性、候補環境の検証、拡張機能の配布 |
| `tests/assets/` | アセット管理および RPC 連携（※Web 側の単体テストは `web/` 配下で実行） |
| `tests/models/` | モデルの事前準備、保存・コミット処理、公開 API の設定 |

---

## ローカルでのテスト実行方法

事前にテスト用の依存関係を同期した上で実行します。

```bash
# テスト環境のセットアップ（依存関係の同期）
uv sync --locked --extra split-test

# 全テストスイートの実行
uv run --locked --extra split-test python scripts/test_standalone.py

# 特定のスイートのみ実行（standard / split / assets / models）
uv run --locked --extra split-test python scripts/test_standalone.py --suite split

# 特定のテストファイルに絞り込んで実行
uv run --locked --extra split-test python scripts/test_standalone.py --suite split --pattern test_worker.py
```

---

## テストの設計原則と運用方針

### 1. 外部拡張の隔離（import 禁止）
- テスト実行時は、登録された全任意拡張の直接 import を禁止しています。
- `isolation.py` がこのインポート禁止ルールを管理し、拡張の有無に依存しない再現性を担保します。
- 共通の拡張連携テストでは、登録情報に基づくロード順序、API ガード、GPU 実行のラップ順序、Secret の分離を検証します。

### 2. 外部依存のモック化
- すべてのテストはローカル素材とモックを用いて実行します。実際のリモートデプロイ、モデルダウンロード、外部 AI API、実 GPU インスタンスの起動は行いません。
- 複数テストで共有する Modal のモックは `split/support.py` に集約しています。
- 通常構成とモデル保存で共通する環境変数の境界値検証は `env_checks.py` で管理します。

### 3. 分離構成（split）特有の検証
- **未 import での環境検証**: `split/test_deployment.py` の子プロセス実行は、モジュール未 import のクリーンな状態でイメージ構成と Secret 定義を確認するためだけに使用します（子プロセス内で他のテストスイートを再実行することはありません）。
- **本体テストの範囲**: 候補環境の依存検査、失敗時のロールバック、CPU/GPU 間の環境バージョン一致確認は split 本体のテストで保証します。
- **外部拡張リポジトリとの境界**: 各任意拡張との実通信、受付ロック、保存形式、再接続テストは各拡張リポジトリ側の `tests/split/` で `scripts/test_split.py --split-root /path/to/modal-comfyui-deploy` を用いて検証します（消費側のテストコードを直接 import しません）。
- **ComfyUI-Modal-Bridge の検証**: CPU 実行制御を担う [ComfyUI-Modal-Bridge](../extensions/ComfyUI-Modal-Bridge/README.md) は split 本体の一部であるため、本リポジトリ側で検証します。

### 4. テスト追加時の指針
- 新規テストは対象機能のファイルに配置し、既存の別テストケースを import やクラス継承で使い回さないでください（テスト間の不要な結合を防ぐため）。
- 単なるファイルの存在確認や、既存ケースと重複する冗長なテストは追加せず、実際の動作や境界条件を検証する最小限のテストを作成してください。

---

## GitHub Actions（CI）の実行条件と差分判定

リポジトリの GitHub Actions（`Tests` ワークフロー）は、PR の作成・更新時および `main` ブランチへの push 時に自動実行されます。PR のない作業ブランチへの push では重複実行されませんが、必要に応じて GitHub Actions 画面から `workflow_dispatch` による手動実行が可能です。

### 差分判定と実行ジョブ

CI は変更されたファイルを自動判定し、影響のあるテストジョブのみを選択して実行します。対象外のジョブはスキップ（skip）され、全体の実行時間を短縮します。

| 変更対象ファイル | 実行されるテスト | 理由・備考 |
| :--- | :--- | :--- |
| Python ファイル、`tests/`、Split モデルパス設定、ComfyUI 拡張 Web ファイル、`scripts/modal.sh`、`scripts/setup.sh` | **Python** | Python バックエンドおよび ComfyUI 関連の回帰テスト |
| `web/` | **Asset manager** | Web フロントエンドのテスト |
| `worker/`、`mcp/` | **Access proxy** | Worker と MCP パッケージのテスト、Worker の型検査（`typecheck`） |
| `asset_manager.py`、`asset_rpc.py`、`preserve_model.py`、`pyproject.toml`、`uv.lock`、`.python-version` | **Python** + **Asset manager** | Python バックエンドと Web アセット管理の両方に依存するため |
| `.node-version` | **Asset manager** + **Access proxy** | Node.js バージョン指定ファイルの変更は Web と Worker の両方に影響するため |
| `.github/workflows/tests.yml` | **全テスト** | CI 定義自体の変更のためすべてのジョブを実行 |
| `README.md`、`docs/` のみ | **テストなし**（全スキップ） | ドキュメントのみの変更時はテストを省略 |

※Python ジョブでは `scripts/test_standalone.py` を使用し、通常版・split・アセット管理・モデル保存の各スイートを一度ずつ実行します。

### 判定ロジックと動作仕様

- **Pull Request**: 直近のコミット差分だけでなく、**PR 全体の差分**を判定します。途中のコミットで変更されたファイルも漏れなく検証対象に含まれます。
- **`main` への push**: 直前の push との差分を判定して実行します。
- **手動実行（`workflow_dispatch`）**: 変更箇所に関わらず、**全種類のテストを実行**します。
- **判定失敗時の安全動作**: 差分判定スクリプトが失敗した場合は、必要なテストのスキップを防ぐため安全側に倒して**全種類のテストを実行**します。
- **ジョブのステータス管理**: 対象外となったジョブはエラーで失敗させるのではなく、正常に skip されます。ワークフロー全体を途中で止めず、GitHub の必須チェック（Status Checks）が未実行のまま待たされることがないよう設計されています。
