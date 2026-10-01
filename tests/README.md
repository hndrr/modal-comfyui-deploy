# テストの配置

`tests/test_*.py` はsplit本体、Volume、モデル保存、CPU復元、通常生成の回帰テストです。非公開拡張のインストールなしで実行し、任意拡張のimportを禁止します。

```sh
uv sync --locked --extra split-test
uv run --locked --extra split-test python scripts/test_standalone.py
```

`tests/integration/` はインストールした拡張とsplit本体を接続するテストです。Bridgeの受付ロック・Journal復元・GPU投入・終了処理を確認します。ワークフロー拡張のAPI・イベント・旧データ互換・併用テストは、その拡張repoで実際のsplitへ接続して実行します。

```sh
uv sync --locked --extra bridge-test
uv run --no-sync python -m unittest discover -s tests/integration -v
```

このフォルダーは独立したunittest discoveryの起点です。`__init__.py` は追加せず、標準スイートの再帰探索に含めません。CIも同じ2区分で実行します。非公開依存を取得できなかった場合はGitHub Actionsのsummaryへ未実行理由を表示します。

コンポーネントの単体テスト・基準データは各配布元で管理します。ここに残すのは、実際のController・Journal・CPU/GPU起動処理と拡張をつなぐテストです。連携用fixtureは他repoのテストコードをimportしません。ComfyUI-Modal-BridgeのCPU制御テストはこのrepoが担当します。

すべてローカル素材とモックで実行し、実デプロイ・モデル取得・実Codex実行・GPU実生成は行いません。
