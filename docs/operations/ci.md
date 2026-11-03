# CI（GitHub Actions）の実行条件

リポジトリの GitHub Actions（`Tests` ワークフロー）は、PR の作成・更新時および `main` ブランチへの push 時に自動実行されます。PR のない作業ブランチへの push では重複実行されませんが、必要に応じて GitHub Actions 画面から `workflow_dispatch` による手動実行が可能です。

---

## 差分判定と実行ジョブ

CI は変更されたファイルを自動判定し、影響のあるテストジョブのみを選択して実行します。対象外のジョブはスキップされ、全体の実行時間を短縮します。

| 変更対象ファイル | 実行されるテスト | 理由・備考 |
| :--- | :--- | :--- |
| Python ファイル、`tests/`、Split モデルパス設定、ComfyUI 拡張 Web ファイル、`scripts/modal.sh` | **Python** | Python バックエンドおよび ComfyUI 関連の回帰テスト |
| `web/` | **Asset manager** | Web フロントエンドのテスト |
| `worker/`、`mcp/` | **Access proxy** | Worker と MCP パッケージのテスト、Worker の型検査（`typecheck`） |
| `asset_manager.py`、`asset_rpc.py`、`preserve_model.py`、`pyproject.toml`、`uv.lock`、`.python-version` | **Python** + **Asset manager** | Python バックエンドと Web アセット管理の両方に依存するため |
| `.github/workflows/tests.yml` | **全テスト** | CI 定義自体の変更のためすべてのジョブを実行 |
| `README.md`、`docs/` のみ | **テストなし**（全スキップ） | ドキュメントのみの変更時はテストを省略 |

※Python ジョブでは `scripts/test_standalone.py` を使用し、通常版・split・アセット管理・モデル保存の各スイートを一度ずつ実行します（任意拡張の直接 import は禁止されています）。

---

## 判定ロジックと動作仕様

### PR と push での差分取得の違い
- **Pull Request**: 直近のコミット差分だけでなく、**PR 全体の差分**を判定します。途中のコミットで変更されたファイルも漏れなく検証対象に含まれます。
- **`main` への push**: 直前の push との差分を判定して実行します。

### 手動実行とフォールバック
- **手動実行（`workflow_dispatch`）**: 変更箇所に関わらず、**全種類のテストを実行**します。
- **判定失敗時の安全動作**: 差分判定スクリプトが失敗した場合は、必要なテストのスキップを防ぐため安全側に倒して**全種類のテストを実行**します。

### ジョブのステータス管理
- 対象外となったジョブはエラーで失敗させるのではなく、正常に **skip** されます。
- ワークフロー全体を途中で止めず、GitHub の必須チェック（Status Checks）が未実行のまま待たされることがないよう設計されています。

---

## 関連リンク
- ローカルでの各テストスイートの実行方法: [テストの説明](../../tests/README.md)
