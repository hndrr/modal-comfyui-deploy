# CIの実行条件

`Tests`はPRと`main`へのpushで動く。PR用ブランチへのpushでは重複実行しない。
PRのない作業ブランチで確認したい場合は、Actionsから`workflow_dispatch`で手動実行できる。

最初に変更ファイルを判定し、対象のジョブだけを起動する。

| 変更箇所 | 実行するテスト |
| --- | --- |
| Pythonファイル、`tests/`、Splitのモデルパス設定、ComfyUI拡張のWebファイル、`scripts/modal.sh` | Python |
| `web/` | Asset manager |
| `worker/` | Access proxy |
| `asset_manager.py`、`asset_rpc.py`、`preserve_model.py`、`pyproject.toml`、`uv.lock`、`.python-version` | Python＋Asset manager（Pythonバックエンドを利用するため） |
| `.github/workflows/tests.yml` | 全種類 |
| `README.md`、`docs/`、`ambient/docs/`だけ | テストなし |

PRでは直近コミットだけでなく、PR全体の差分を判定する。前のコミットで変更した部分も検証対象に残る。
このため、CI設定の変更を含むPRでは全種類を実行する。
`main`へのpushでは直前のpushとの差分を使う。

手動実行では変更箇所にかかわらず全種類を実行する。変更判定に失敗した場合も全種類を実行し、
判定失敗によって必要なテストが省略されないようにする。

ワークフロー全体をパス条件で止めず、対象外のジョブをskipする。既存のチェック名は維持し、
必須チェックを未実行のまま待たせない。
