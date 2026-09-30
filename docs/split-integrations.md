# Splitの任意拡張と移行

`splitapp.py` の標準構成は、Ambient Studio・旧Ambient API・private追加ノードを必要としません。
通常のComfyUIキュー、履歴、生成、個別キャンセル、Manager、CPUスナップショット、GPU自動停止はsplit自身が担当します。

## 設定

| 変数 | 既定 | 有効にする機能 |
| --- | --- | --- |
| `SPLIT_EXTENSIONS` | 空 | `ambient`でStudioのワークフローAPI・実行表示・パネルを追加 |
| `SPLIT_NODE_PACKS` | 空 | `agent-bridge,agent-runtime,skills-loader,gemini,jev`からカンマ区切りで選択 |
| `SPLIT_AGENT_BRIDGE` | `off` | `on`でCPU/GPU間のMac Bridge中継を有効化 |
| `SPLIT_URL` | 空 | 検証スクリプトが接続するCPU UIのURL。起動には不要 |

新規の標準構成は次の設定です。既存の旧Ambient設定も明示的に上書きできます。

```dotenv
SPLIT_EXTENSIONS=
SPLIT_NODE_PACKS=
SPLIT_AGENT_BRIDGE=off
```

Studioから動画を生成するだけなら`SPLIT_EXTENSIONS=ambient`を設定します。
Codex準備を使う場合は`SPLIT_NODE_PACKS=agent-bridge`と`SPLIT_AGENT_BRIDGE=on`を設定します。
ComfyUI側で直接エージェントを実行する場合だけ`agent-runtime`を追加し、共有Skill Loaderには`skills-loader`、Jevタグ付けには`jev`を追加します。
GeminiToolsだけ使う場合は`SPLIT_NODE_PACKS=gemini`で、Ambient拡張もBridgeも不要です。
モデルやユーザー管理のcustom nodesは既存の配置を使います。これらの設定はモデルを取得しません。

選択したprivate追加ノード、Ambient拡張、Bridge中継パッケージの取得に`GITHUB_SECRET_NAME`（既定`github-secret`）を使います。
追加ノードを選択せずAmbient拡張・Bridge中継だけを有効にした構成では、このSecretはイメージ作成時だけに渡し、実行中のCPU/GPUへは渡しません。
トークンには、利用する非公開repo（`hndrr/ComfyUI-Ambient`、`hndrr/ComfyUI-AgentBridge`、選択した追加ノード）のContents読み取り権限を付けてください。
Geminiには`GEMINI_SECRET_NAME`、Jevには使用する`TYPESAFE_SECRET_NAME`／`OPENROUTER_SECRET_NAME`、
Bridgeには`AGENT_RUNTIME_SECRET_NAME`を渡します。未使用機能にはSecret依存を付けません。
Bridgeノードを手動導入している場合も、中継は`SPLIT_AGENT_BRIDGE`で独立して有効化できます。

## 旧構成からの移行

旧`COMFYUI_AMBIENT_MODE=on`は、新設定が未指定の項目だけについて、Ambient拡張・全追加ノード・Bridgeを有効にします。
新設定の空文字列や`off`も明示指定として優先します。新しい設定例には新変数があるため、旧フラグではなく新変数を編集してください。

既存のワークフローrevision・参照画像・Journal・Volume・ライブラリは保持します。
追加ノードは新環境の`node_packs/`へ保存し、旧環境の`ambient_nodes/`も読めます。
選択解除したノードの保存データは削除せず、ComfyUIの検索対象から外します。
デプロイ単位の管理IDは`SPLIT_DEPLOYMENT_ID`です。過去のCPUスナップショットの保護記録は維持します。

拡張を有効にしたsplitは従来の`/ambient/workflows`、`/ambient/executions`、`ambient_execution`イベントを提供します。
LocalとModalのモデル既定値は別プロファイルです。保存済みのグラフやモデル選択を置き換えません。
拡張なしのsplitはAmbientパネル・APIを公開せず、標準ComfyUIのHTTP/WebSocket契約を提供します。

## コードの所有と配布

共有レシピ、ワークフロー編集、Bridgeテンプレート、パネル、split用アダプターは、
非公開の[ComfyUI-Ambient](https://github.com/hndrr/ComfyUI-Ambient) repoが正本です。
Studioは拡張のHTTP APIを使い、拡張のソースや梱包処理を持ちません。
LocalはNativeキュー、splitアダプターはsplitのJournalを利用します。
Native用の受付処理をsplitのCPU/GPUプロセスへ登録することはありません。

Modalは`comfy_split/extension_sources.py`の固定コミットをGitHubから取得し、標準のPythonパッケージとしてイメージにインストールします。
隣接repoのチェックアウト、wheelの手作り、repo間のコピー、Release公開は不要です。
通常のデプロイは次のとおりです。この操作は実際のModalデプロイを行います。

```sh
SPLIT_EXTENSIONS=ambient ./scripts/modal.sh deploy splitapp.py
```

更新時は`comfy_split/extension_sources.py`と`pyproject.toml`を同じ拡張コミットへ変更し、
GitHubの認証がある環境で`uv lock --upgrade-package ambient-comfyui`を実行します。
テスト後に通常のデプロイを行ってください。既存のVolumeや保存済みグラフの移動は不要です。

Studioの接続設定と保存済み動画の操作は、[Studioの接続ガイド](https://github.com/hndrr/ambient-studio/blob/main/docs/modal-split.md)・[保存データ](https://github.com/hndrr/ambient-studio/blob/main/docs/saved-modal-library.md)で管理します。
Ambientのレシピ・旧バックエンドの設計や測定記録は[ComfyUI-Ambient](https://github.com/hndrr/ComfyUI-Ambient/blob/main/docs/history/modal-backend/README.md)へ移管しています。

## モデルの明示的な準備

このrepoに残る`scripts/prepare_ambient_h3.py`は、共通パッケージのモデル定義を使ってModal Volumeにモデルを配置します。
起動・デプロイ・設定編集だけではモデルを取得しません。準備が必要な場合に、このrepoで次を実行します。

```sh
./scripts/modal.sh run scripts/prepare_ambient_h3.py --mode h3
```

対象は`h3`、`h3-turbo-4step`、`h3-fused-4step`、`fasth3`、`fasth3-8step-t2v`、`fasth3-8step-i2v`です。
`model_manifests.py`が固定revisionとチェックサムを保持し、`scripts/modal.sh`はこのコマンド時にAmbient依存を選択します。
生成先のノードは別途導入し、モデル・ノードの不足は投入前のワークフロー検証で確認します。

## ローカル検証

```sh
uv sync --locked --extra split-test
uv run --locked --extra split-test python scripts/test_standalone.py
uv sync --locked --extra ambient-test --extra bridge-test
uv run --locked --extra ambient-test --extra bridge-test python -m unittest discover -s tests/integration -v
```

最初の検証はAmbient・Bridgeを取得・インストールせず、両方のimportを禁止します。
任意拡張のテストには非公開repoへのGit読み取り権限が必要です。GitHub CLIを使う場合は、`gh auth setup-git`でGitの認証を設定できます。
GitHub Actionsでは`INTEGRATION_REPO_TOKEN` SecretにAmbientとBridgeのread権限を設定します。既存の`AMBIENT_REPO_TOKEN`も同じ権限があれば利用できます。未設定時も標準splitのテストは実行し、任意拡張のテスト未実施をサマリーに表示します。次の検証で任意拡張・保存済みワークフローの互換性も確認します。
模擬ComfyUI／Modalとローカル素材を使用し、実デプロイ・モデル取得・GPU実生成は行いません。

2026-09-28の分離時には、Ambient未インストールかつimport禁止の環境で通常生成・起動・CPU復元を確認しました。
Studio Coordinatorと実際のsplitアダプターをローカル接続し、編集revision、重複投入防止、進捗、Studio再起動後の同一ジョブへの再接続、動画保存とJevタグ付けを確認しています。GPU実行と外部プロバイダーだけを模擬し、動画はローカルで作成した1秒の素材です。
また、公式Frontend **1.52.7** をローカルのHTTP/WebSocketモックで起動し、パネル登録、実行グラフ、ノード進捗、リロード・切断後の進捗復元を確認しました。本体・Frontendの固定バージョンは変更していません。

## Agent Bridgeの独立配布

非公開の[ComfyUI-AgentBridge](https://github.com/hndrr/ComfyUI-AgentBridge)が4ノード、Macとの通信SDK、split用中継を所有します。
splitには任意拡張の呼び出しと、無効時の受付/APIガードだけを残しています。
`extensions/ComfyUI-Modal-Bridge`はCPU実行制御用なので別機能として維持します。

- Studioは同じnpm import名を使い、新repoの固定SHAからインストールします。`prepare`がTypeScriptをビルドし、起動は従来の`npm run dev` / `npm start`です。
- Modalは中継有効時だけPythonパッケージをイメージへ取得します。通常importではComfyUIノード/APIを登録しません。
- `agent-bridge`の管理ノードとPythonパッケージは`extension_sources.BRIDGE`の同じSHAです。AgentRuntimeもBridge除去済みの`extension_sources.AGENT_RUNTIME`へ固定します。
- 全候補ノードの取得後に旧AgentRuntimeと新Bridgeの二重登録を検査し、CPUでロード検証してから採用します。Manager候補とCPU/GPUの起動時にも検査します。失敗時は保存済み旧環境を使い、候補のノードを混ぜません。
- `AgentRuntimeBridge*`、`/agent_runtime/bridge/*`、protocol v1とJournalの`agent_bridge`形式は維持します。接続が変わったジョブや結果不明のジョブを自動再実行しません。

Bridge更新時は`extension_sources.BRIDGE`と`pyproject.toml`を同じSHAにして`uv lock --upgrade-package comfyui-agent-bridge`を実行します。
AgentRuntimeの固定先は[分離PR](https://github.com/hndrr/ComfyUI-AgentRuntime/pull/10)の検証済みコミットです。
設定後は通常どおり`./scripts/modal.sh deploy splitapp.py`で取得・配置されます。手作業でファイルを渡す必要はありません。

分離時の検証: BridgeはPython 41件、Node 9件、自動切断・HTTP転送7件が成功。
Studioは265件とtypecheck/lint、AgentRuntimeはPython 370件（環境依存7件skip）とJS 18件が成功しました。
splitは両拡張未インストール/import禁止の169件と、固定Git依存を取得した統合環境の217件が成功しています。
旧ノード契約の固定fixture、候補環境失敗・復帰、接続の受付ロック、結果不明ジョブの復元、大きな転送と取消も検証対象です。
CIではprivate repoの認証情報を取得ステップだけに渡し、テストには渡しません。実Modalデプロイ・モデル取得・実Codex・GPU実生成は未実施です。
