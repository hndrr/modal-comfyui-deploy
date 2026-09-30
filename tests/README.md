# テストの配置

`tests/test_*.py` はsplit本体、Volume、モデル保存、CPU復元、通常生成の回帰テストです。非公開拡張のインストールなしで実行し、AmbientとAgentBridgeのimportを禁止します。

```sh
uv sync --locked --extra split-test
uv run --locked --extra split-test python scripts/test_standalone.py
```

`tests/integration/` はインストールした拡張とsplit本体を接続するテストです。Bridgeの受付ロック・Journal復元・GPU投入・終了処理、AmbientのHTTP API・イベント、設定の組み合わせ、Modalのモデル準備を確認します。

```sh
uv sync --locked --extra ambient-test --extra bridge-test
uv run --no-sync python -m unittest discover -s tests/integration -v
```

このフォルダーは独立したunittest discoveryの起点です。`__init__.py` は追加せず、標準スイートの再帰探索に含めません。CIも同じ2区分で実行します。非公開依存を取得できなかった場合はGitHub Actionsのsummaryへ未実行理由を表示します。

| 実装・テストの所有repo | 対象 |
| --- | --- |
| ComfyUI-AgentBridge | Bridgeノード、通信、素材転送、大容量メッセージ、接続待ち・切断、SDK、専用UI |
| ComfyUI-Ambient | 生成レシピ、Local/Modalのモデルプロファイル、ワークフローrevision、タグ付け、パネル |
| ambient-studio | 生成準備、Coordinator、Codex起動ポリシー、StudioとBridgeの連携 |
| modal-comfyui-deploy | split本体とインストール済み拡張の連携、Modalのモデル準備 |

コンポーネントの単体テストやその基準データは所有repoへ置きます。`ambient_fixtures.py` は連携用のComfyUI応答・入力データで、他repoのテストコードをimportしません。ComfyUI-Modal-BridgeのCPU制御テストはこのrepoに残します。

すべてローカル素材とモックで実行し、実デプロイ・モデル取得・実Codex実行・GPU実生成は行いません。
