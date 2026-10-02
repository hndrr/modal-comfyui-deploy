# Splitの任意拡張

splitは任意拡張なしで動作します。ComfyUI標準のワークフロー作成・編集・保存・読み込みも利用できます。キュー、GPU実行、履歴、個別キャンセル、Manager、CPU復元、自動停止はsplit本体が担当します。

## 設定

以下の既定値は`COMFYUI_AMBIENT_MODE`を指定していない場合の値です。この変数を使っている場合は[設定の優先順位](#設定の優先順位と保存済みデータ)を参照してください。

| 変数 | 既定 | 内容 |
| --- | --- | --- |
| `SPLIT_EXTENSIONS` | 空 | カタログの`extensions`からカンマ区切りで選択。登録名は`ambient` |
| `SPLIT_NODE_PACKS` | 空 | `agent-bridge`、`agent-runtime`、`skills-loader`、`gemini`、`jev`からカンマ区切りで選択 |
| `SPLIT_AGENT_BRIDGE` | `off` | `on`でAgentBridge中継を有効化。`agent-bridge`ノードの選択とは独立 |
| `SPLIT_URL` | なし | `scripts/verify_cpu_snapshot.py`の接続先。このスクリプトの実行時だけ必須 |

```dotenv
SPLIT_EXTENSIONS=
SPLIT_NODE_PACKS=
SPLIT_AGENT_BRIDGE=off
```

各設定は独立しています。ノード選択や拡張の有効化だけではモデルを取得しません。

## 取得と更新

[拡張カタログ](../comfy_split/extension_catalog.toml)に配布元、取得するコミット、読み込み先を定義しています。設定や固定コミットを変更したら、`./scripts/modal.sh deploy splitapp.py`で再デプロイします。取得のタイミングは次のとおりです。

| 対象 | 取得する版 | 取得・検証のタイミング |
| --- | --- | --- |
| `SPLIT_EXTENSIONS`の拡張、`SPLIT_AGENT_BRIDGE`の中継 | カタログの固定コミット | デプロイのイメージ作成時にPythonパッケージをインストールし、配布バージョンを確認 |
| `SPLIT_NODE_PACKS`の追加ノード | `revision`があれば固定コミット、なければ既定ブランチのHEAD | デプロイ後、生成・候補環境・GPUセッションのない分離モードでのCPU起動時に更新を確認 |

追加ノードに変更があれば、候補環境を作り、固定依存との整合性とCPUでのノード読み込みを検査してから採用します。この自動更新ではGPU検証を行いません。Managerで追加・更新した候補の反映はCPUとGPUで検証します。[Managerの操作](comfyui-split.md#操作)を参照してください。

`agent-bridge`ノードとAgentBridge中継は同じ配布元・固定コミットを共有します。ノードの組み合わせはカタログの`node_conflicts`でimportや採用の前に検査します。CPU/GPUは同じ環境版を使い、候補の検証に失敗した場合は更新前の有効環境を維持します。

追加ノードの自動更新に失敗しても、更新前のノード一式が再利用可能なら、同じデプロイ・保存版では取得を繰り返しません。初回導入や保存済みノードの欠落・破損で再利用できない場合は、失敗時刻をJournalに保存し、同じデプロイ・環境では5分間、取得を再試行しません。5分経過後の次のアイドル状態でのCPU起動時に再試行します。待機のためにCPUを稼働させ続けることはありません。再デプロイ・環境変更、または管理者によるJournalの`node_packs_refresh`の明示リセットでは、この待機を解除します。

## Secret

非公開パッケージの取得には、`GITHUB_SECRET_NAME`で指定するModal Secret（既定`github-secret`）内の`GITHUB_TOKEN`を使います。トークンには選択したrepoのContents読み取り権限が必要です。イメージ作成だけで取得が完了する構成では、実行中のCPU/GPUへ渡しません。追加ノードを選択した場合は起動時の取得のためCPUへ渡しますが、GPUへは渡しません。

Secret名が`github-secret`なら、`.env`への追記は不要です。別の名前を使う場合だけ`GITHUB_SECRET_NAME=<Secret名>`を追加します。

外部サービスの認証は、使う機能に応じて次の設定を`.env`へ追加します。値はModal Secretの名前で、キー本体はModal Secret内に保存します。表のSecret名は例です。

| 使う機能 | `.env`に追加する例 | Modal Secret内のキー |
| --- | --- | --- |
| `gemini`ノードのGemini API | `GEMINI_SECRET_NAME=gemini-secret` | `GEMINI_API_KEY` |
| `jev`ノードのTypesafe API | `TYPESAFE_SECRET_NAME=typesafe-secret` | `TYPESAFE_API_KEY` |
| `jev`ノードのOpenRouter API | `OPENROUTER_SECRET_NAME=openrouter-secret` | `OPENROUTER_API_KEY` |
| AgentBridge中継（`SPLIT_AGENT_BRIDGE=on`） | `AGENT_RUNTIME_SECRET_NAME=agent-runtime-secret` | `AGENT_RUNTIME_BRIDGE_TOKEN` |

未使用機能にはSecret依存を付けません。AgentBridgeの中継用Secretは`SPLIT_AGENT_BRIDGE=on`の場合だけ渡し、ノードを選択しただけでは渡しません。

## 設定の優先順位と保存済みデータ

`SPLIT_EXTENSIONS`、`SPLIT_NODE_PACKS`、`SPLIT_AGENT_BRIDGE`は、それぞれ明示した値を使います。空文字列による拡張・追加ノードの無効化と、`off`による中継の無効化も優先します。

`COMFYUI_AMBIENT_MODE=on`を設定している場合に限り、未指定の項目を補います。`SPLIT_EXTENSIONS`は`ambient`、`SPLIT_NODE_PACKS`はカタログ内の全追加ノード、`SPLIT_AGENT_BRIDGE`は`on`として扱います。これらの変数をいずれも設定していなければ、拡張・追加ノード・中継はすべて無効です。

拡張の選択解除だけでは、保存済みデータやJournal内の拡張状態を削除しません。

## モデル配置

モデル定義は各拡張で管理します。`scripts/prepare_models.py`は、version 1のJSON manifestに列挙された資産を`preserve_model.py`の保存処理で`comfy-model` Volumeへ配置します。

```sh
./scripts/modal.sh run scripts/prepare_models.py --manifest /path/to/models.json
```

manifestは`{"version": 1, "assets": [...]}`形式です。各資産に`repo_id`、40桁の固定`revision`、`filename`、`destination_subdir`を指定し、任意で`expected_sha256`を付けます。全資産の形式を検証してから配置を開始します。起動やデプロイではモデルを取得しません。拡張ごとのモデル選択・生成仕様はカタログの配布元repoで管理します。

CPU実行制御の`extensions/ComfyUI-Modal-Bridge`とGPU表示の`extensions/ComfyUI-Modal-Control`はsplitに同梱する拡張です。ここで選択する任意パッケージとは別に読み込まれます。

## ローカル検証

```sh
uv sync --locked --extra split-test
uv run --locked --extra split-test python scripts/test_standalone.py
```

標準スイートは全登録拡張のimportを禁止し、通常生成・CPU復元・拡張フック・候補環境・Secretの分離を検証します。[テストの配置](../tests/README.md)を参照してください。

実パッケージとのAPI・通信・保存形式の契約は各拡張repoで管理します。検証対象のsplitチェックアウトを明示し、実際のController・Journal・workerに接続して検証します。splitのPython依存とCIは非公開repoの取得権限を必要としません。
