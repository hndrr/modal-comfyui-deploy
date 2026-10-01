# テストの配置

`tests/test_*.py` はsplit本体、Volume、モデル保存、CPU復元、通常生成の回帰テストです。非公開拡張のインストールなしで実行し、任意拡張のimportを禁止します。

```sh
uv sync --locked --extra split-test
uv run --locked --extra split-test python scripts/test_standalone.py
```

共通の拡張テストは、登録情報に従うロード・APIガード・GPU実行のラップ順序・Secretの分離を検証します。候補環境の検査・失敗時の復帰・CPU/GPUの環境一致も本体のテストです。

実際の任意パッケージとの通信・受付ロック・保存形式・再接続は各拡張repoの`tests/split/`で管理します。`scripts/test_split.py --split-root /path/to/modal-comfyui-deploy`で実hostに接続して検証します。消費側のテストコードをimportしません。

CPU実行制御用のComfyUI-Modal-Bridgeはsplit本体の一部なので、このrepoで検証します。すべてローカル素材とモックを使い、実デプロイ・モデル取得・実Codex・GPU生成は行いません。
