# テスト

| 配置 | 検証対象 |
| --- | --- |
| `standard/` | 通常版 `comfyapp.py` の設定、Manager、WebSocketパッチ |
| `split/` | Journal、Gateway、GPU worker、起動・CPU復元、Volume、候補環境、拡張の選択・配布 |
| `assets/` | アセット管理とRPC。Web側のテストは `web/` で実行 |
| `models/` | モデル準備、保存・commit、公開APIの設定 |

全体実行も部分実行も同じ入口を使います。非公開拡張のインストールは不要で、全登録拡張のimportを禁止します。各テストは一度だけ実行します。

```sh
uv sync --locked --extra split-test
uv run --locked --extra split-test python scripts/test_standalone.py

# 変更した機能だけ実行（standard / split / assets / models）
uv run --locked --extra split-test python scripts/test_standalone.py --suite split

# ファイルを絞って実行
uv run --locked --extra split-test python scripts/test_standalone.py --suite split --pattern test_worker.py
```

`split/test_deployment.py` の子プロセスは、未importの状態でイメージ構成とSecretを確認するためだけに使います。その中で他のテストスイートを再実行しません。実行時のimport禁止は共通ランナーが担います。

共通の拡張テストは、登録情報に従うロード・APIガード・GPU実行のラップ順序・Secretの分離を検証します。候補環境の検査・失敗時の復帰・CPU/GPUの環境一致もsplit本体のテストです。

実際の任意パッケージとの通信・受付ロック・保存形式・再接続は各拡張repoの`tests/split/`で管理します。`scripts/test_split.py --split-root /path/to/modal-comfyui-deploy`で実hostに接続して検証します。消費側のテストコードをimportしません。

CPU実行制御用のComfyUI-Modal-Bridgeはsplit本体の一部なので、このrepoで検証します。すべてローカル素材とモックを使い、実デプロイ・モデル取得・実Codex・GPU生成は行いません。

追加するときは対象機能のファイルに置き、別のテストケースをimport・継承して再利用しないでください。複数箇所で必要なModalのモックだけを `split/support.py` にまとめています。通常版とモデル保存で共通する環境変数の境界値は `env_checks.py`、import禁止は `isolation.py` で管理します。ファイルの存在確認だけのテストや、既存ケースに含まれる検証は増やさず、実際の動作を検証します。
