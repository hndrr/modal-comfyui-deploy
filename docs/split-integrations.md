# Splitの任意拡張

標準構成は拡張なしで動作します。キュー、GPU実行、履歴、個別キャンセル、Manager、CPU復元、自動停止はsplit本体が担当します。

## 設定

| 変数 | 既定 | 内容 |
| --- | --- | --- |
| `SPLIT_EXTENSIONS` | 空 | 登録済み拡張名をカンマ区切りで指定 |
| `SPLIT_NODE_PACKS` | 空 | `agent-bridge,agent-runtime,skills-loader,gemini,jev`からカンマ区切りで選択 |
| `SPLIT_AGENT_BRIDGE` | `off` | `on`でCPU/GPU間のBridge中継を追加 |
| `SPLIT_URL` | 空 | 検証スクリプトの接続先。起動には不要 |

```dotenv
SPLIT_EXTENSIONS=
SPLIT_NODE_PACKS=
SPLIT_AGENT_BRIDGE=off
```

各設定は独立しています。ノード選択や拡張の有効化だけではモデルを取得しません。

## 取得とSecret

[拡張カタログ](../comfy_split/extension_catalog.toml)に配布元・固定コミット・読込入口・パネル資産を登録します。splitはその登録情報に従って取得・読み込みを行います。Bridgeの固定先は`comfy_split/extension_sources.py`と`pyproject.toml`・`uv.lock`で揃えます。候補環境を検証してから通常の`./scripts/modal.sh deploy splitapp.py`で反映します。

非公開パッケージの取得には`GITHUB_SECRET_NAME`（既定`github-secret`）を使い、選択したrepoのContents読み取り権限を付与します。イメージ作成時だけ取得が必要な構成では、このSecretを実行中のCPU/GPUへ渡しません。

| 選択した機能 | 実行時のSecret |
| --- | --- |
| GeminiTools | `GEMINI_SECRET_NAME` |
| Jev | 使用する`TYPESAFE_SECRET_NAME`／`OPENROUTER_SECRET_NAME` |
| Bridge | `AGENT_RUNTIME_SECRET_NAME` |

未使用機能にはSecret依存を付けません。管理対象のBridgeノードと中継パッケージは同じコミットを使い、旧AgentRuntimeとの二重登録を候補環境の検証で拒否します。CPU/GPUは同じ環境版を使い、失敗時は旧環境を保持します。

## 既存設定・保存環境の互換性

旧設定・保存先名はカタログの`legacy`で定義します。新設定の空文字列と`off`も明示値として優先します。旧環境とJournal内の拡張状態は読み込みを維持し、選択解除でも保存データを削除しません。CPU/GPUは同じ環境版を使います。

## モデル配置

モデル定義は各拡張で管理します。splitはversion 1のJSON manifestに列挙された資産を、明示操作でModal Volumeへ配置します。

```sh
./scripts/modal.sh run scripts/prepare_models.py --manifest /path/to/models.json
```

manifestは`{"version": 1, "assets": [...]}`形式です。各資産に`repo_id`、40桁の固定`revision`、`filename`、`destination_subdir`を指定し、任意で`expected_sha256`を付けます。全資産の形式を検証してから配置を開始します。起動やデプロイではモデルを取得しません。拡張ごとのモデル選択・生成仕様はカタログの配布元repoで管理します。

CPU実行制御の`extensions/ComfyUI-Modal-Bridge`は、任意のBridge中継とは別のsplit用拡張です。

## ローカル検証

```sh
uv sync --locked --extra split-test
uv run --locked --extra split-test python scripts/test_standalone.py
uv sync --locked --extra bridge-test
uv run --no-sync python -m unittest discover -s tests/integration -v
```

標準スイートは拡張のimportを禁止します。連携スイートは実際の拡張を接続し、受付・復元・完了通知を検証します。[テストの配置](../tests/README.md)を参照してください。

CIの`INTEGRATION_REPO_TOKEN`には対象repoのread権限が必要です。既存CIのSecret名も互換用に受け付けます。認証情報は取得ステップだけへ渡します。未設定なら標準テストを実行し、連携テスト未実施をsummaryへ表示します。
