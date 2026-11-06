# agentにセットアップを依頼する

このリポジトリを新しく導入する場合と、既存環境を別マシンや別agentで再構築する場合の入口です。ModalへのデプロイからCloudflare Accessでの認証付き公開まで進め、SplitではMCP接続も選べます。

## コピペ用の依頼文

次の依頼文をagentに渡してください。山括弧の項目を埋め、分からない項目は「未定」としてください。設定値や認証情報を埋めた依頼文はGitに保存せず、Secretの実値はログイン画面や管理コンソールで登録します。

```text
このリポジトリを、AGENTS.md と docs/guides/agent-setup.md に従ってセットアップしてください。

- 作業: <新規導入 / 既存環境の再構築>
- 構成: <通常 standard / CPU・GPU分離 split>
- MCP接続: <不要 / 必要。splitのみ。利用クライアントも指定>
- Modal: <利用するプロファイル・ワークスペース / 未定>
- Cloudflare: <利用するアカウント / 未定>
- 公開ホスト: <PUBLIC_HOST / 未定。https:// やパスを含めない>

まず既存の設定とリソースを調べ、調査で分かることは確認質問をせずに進めてください。
不足する選択や情報はまとめて確認し、既存設定・Secret・Volumeを再利用してください。
新規環境は追加拡張なし、ブラウザ認証はOne-time PINを基本にしてください。
環境準備とデプロイには scripts/setup.sh を使い、認証付き公開と動作確認まで進めてください。
本人のログインなどが必要な場合は、必要な操作と、その後の再開地点を示してください。
実際のURL・メールアドレス・認証情報・リソース識別子をドキュメントやコミットに書かないでください。
完了時は構成、確認結果、残っている作業を報告してください。未確認項目は明記してください。
```

## 1. 現状と依頼内容を確認する

最初に [AGENTS.md](../../AGENTS.md) と [README](../../README.md) を読み、作業ツリーの変更を確認します。`.env`、`.modal-profile`、Workerの設定や登録済みSecretを確認し、設定値やSecretをログへ一括出力しないでください。

| 確認すること | 判断・取得元 |
| --- | --- |
| 新規導入か再構築か | 再構築では既存のApp、Worker、Accessアプリ、ドメイン、Volumeを確認して再利用する |
| 通常かSplitか | [通常構成](comfyui-modal.md)はComfyUI全体をGPUで実行。[Split](comfyui-split.md)はUIをCPU、生成をGPUで実行する実験的構成 |
| MCPを使うか | Splitのみ対応。必要なら接続するクライアントも確認する |
| Modalの接続先 | ログイン済みプロファイルとワークスペース。固定先と環境変数も確認する |
| Cloudflareの接続先 | ログイン済みアカウントと、公開ホストを割り当てる管理対象ゾーン |
| 許可する利用者 | Accessの既存Allowポリシー。新規作成時は許可するメールアドレスを本人に確認する |

調査で確定できない選択だけを、デプロイ前に確認します。通常構成でMCPが必要と指定された場合は、Splitへの変更が必要なことを説明して構成を確定してください。

既存の設定とSecretを再利用し、必要な項目だけ変更します。新規Splitは `.env.example` のとおり追加拡張なしで始めます。モデル保存、任意拡張、外部API、メディア配信専用CPUなどの追加設定は、依頼に必要な場合に各ガイドへ進みます。

## 2. 環境準備とログイン

### ローカル環境を準備する

macOSまたはLinuxのBashで実行します。先にGit、uv、`.node-version` に指定されたNode.jsとnpmを利用できる状態にしてください。Pythonは `.python-version` に従ってuvが用意します。既存のバージョン管理ツールなどで切り替え、ローカルでDockerは起動しません。

リポジトリのルートで実行します。

```bash
./scripts/setup.sh prepare
```

このコマンドは `uv sync --locked --extra split-test`、未作成の場合だけ `.env.example` から `.env` のコピー、`worker/` の `npm ci` を行います。既存の `.env` を保持するため、再実行できます。Node.jsはCIと同じ版にそろえるため、指定版と異なる場合は依存導入前に終了します。

### Modalの接続先を確認する

未ログインの場合は本人が次を実行し、ブラウザでログインします。

```bash
uv run modal setup
```

ログイン後はプロファイルを確認します。

```bash
./scripts/modal.sh --list
```

複数アカウントがある場合は、[Modalプロファイルの手順](../operations/modal-profiles.md)に従い、必要ならこのチェックアウトの接続先を固定します。

```bash
./scripts/modal.sh use "<MODAL_PROFILE>"
./scripts/modal.sh profile current
./scripts/modal.sh app list
```

既存の固定先を変える必要がなければ `use` は省略します。デプロイ時の `--profile` はその呼び出しだけの指定で、既定プロファイルや `.modal-profile` を書き換えません。優先順位は `--profile` → 環境変数 `MODAL_PROFILE` → `.modal-profile` → Modal CLIの既定です。`MODAL_PROFILE` を `.env` に書かないでください。

### Cloudflareの接続先を確認する

```bash
(cd worker && npx wrangler whoami)
```

未ログインの場合は、本人が次のコマンドとブラウザでログインしてから再確認します。

```bash
(cd worker && npx wrangler login)
```

複数アカウントがある場合は、利用するアカウントのIDを環境変数 `CLOUDFLARE_ACCOUNT_ID` で指定します。実値を `worker/wrangler.jsonc` に追加しないでください。

本人の操作を待つ場合は、例えば「Modalへのログインを完了後、プロファイル一覧の確認から再開する」のように、操作と再開地点を具体的に伝えます。ログインやトークン発行を完了したものとして先へ進めないでください。

## 3. Modalをデプロイする

**新規導入**はここから進めます。**既存環境の再構築・更新**では、先に次の反映順序を確認してください。

- 既存の通常構成を直URLから認証付き公開へ無停止で切り替える場合は、[Cloudflare Accessの手順](../operations/cloudflare-access.md)に従い、Cloudflare側を先に整えてからModalをデプロイします。
- 既存MCPの画像取得経路を更新する場合は、[画像ビューアの導入・更新順序](../mcp/setup.md#画像ビューアの導入更新順序)に従い、Workerを先、Splitを後に反映します。

通常構成では、デプロイ前に `.env` の該当行を次の値に変更して保存します。他の設定はそのまま残してください。

```dotenv
COMFYUI_REQUIRES_PROXY_AUTH=on
```

選択した構成だけをデプロイします。

```bash
# 通常構成
./scripts/setup.sh deploy standard

# CPU/GPU分離構成
./scripts/setup.sh deploy split
```

呼び出しごとにModalプロファイルを指定する場合は、末尾へ追加します。

```bash
./scripts/setup.sh deploy split --profile "<MODAL_PROFILE>"
```

通常構成では、スクリプトが `.env` に `COMFYUI_REQUIRES_PROXY_AUTH=on` が保存されていることを確認します。シェルの環境変数で無効に上書きされている場合も、デプロイ前に終了して解除を案内します。設定を `.env` に残すことで、後からREADMEの `./scripts/modal.sh deploy comfyapp.py` を使っても直URLの保護が維持されます。SplitのProxy Authは常に必須です。

デプロイ出力にある `ui` のWeb URLを控えます。通常構成では `<COMFYUI_MODAL_ORIGIN>`、Splitでは `<SPLIT_MODAL_ORIGIN>` として扱い、URLの形式から推測して組み立てません。Workerへ設定するオリジンは `https://` を含み、パスと末尾の `/` を含めません。

イメージのビルドはModal上で行われます。既存Volumeはそのまま利用され、新規環境で必要なVolumeはアプリの定義に従って作成されます。

## 4. Cloudflareで認証付き公開を設定する

[Cloudflare Accessの手順](../operations/cloudflare-access.md)に沿って進めます。このガイドでは新規のModalにも最初からProxy Authを要求するため、Cloudflare経由で設定が完了すると利用できるようになります。

1. 対象ModalワークスペースのProxy Authトークンを用意します。既存のトークンが利用できる場合は再利用します。
2. 公開ホスト全体を保護するブラウザ用Accessアプリを作成・確認します。新規ではOne-time PINと、許可する利用者を指定したAllowポリシーを基本にします。Googleなどの既存IdPは再利用できます。
3. 対象のCloudflareアカウントと既存Workerがこのリポジトリ用であることを確認し、次でWorkerを反映します。既存テスト、型検査、デプロイの順に実行され、途中で失敗すると終了します。

   ```bash
   ./scripts/setup.sh deploy worker
   ```

4. `MODAL_ORIGINS`、`TEAM_DOMAIN`、`POLICY_AUD`、`MODAL_KEY`、`MODAL_SECRET` を確認し、不足する設定を `worker/` の `npx wrangler secret put <NAME>` で登録します。実値は管理コンソールや対話入力で扱います。
5. 公開ホストをWorkerのCustom Domainに登録し、DNSと証明書の反映を確認します。Accessの保護対象を設定してからドメインを割り当ててください。

既存の転送先マップを、今回の接続先1件だけで置き換えないでください。設定を取得できない場合は推測せず、管理者へ確認します。ホスト追加や別Modalワークスペースへの接続には、既存ガイドの `MODAL_ADDITIONAL_ORIGINS` と `MODAL_PROXY_CREDENTIALS` を使えます。

環境固有の値はCloudflareのSecretとGit管理外の設定に保存します。`worker/.dev.vars` は `wrangler dev` 用で、本番のSecret登録は別に必要です。`workers_dev: false` と `preview_urls: false` を維持してください。

## 5. MCPを接続する（Splitで選択した場合）

[MCPの設定・運用ガイド](../mcp/setup.md)と[MCP利用ガイド](../mcp/usage.md)に沿って設定します。

- ブラウザ用Accessアプリでホスト全体を保護したうえで、同じホストの `/mcp` を対象とする専用Accessアプリを用意します。
- 専用アプリは本人のメールアドレスだけをAllowに指定し、Managed OAuthを有効にします。利用クライアントに必要な正確なコールバックURIを登録します。
- Workerの `SPLIT_REMOTE_MCP` に、公開ホスト、Modalオリジン、専用アプリのAUD、本人のメールアドレスを登録します。既存の転送先との一致を確認してください。
- 利用するクライアントで `https://<SPLIT_PUBLIC_HOST>/mcp` にOAuth接続し、本人がログインします。

各クライアントの登録方法は利用ガイドを参照します。手元でローカルMCPサーバーを起動する必要はありません。

## 6. 動作確認と完了報告

デプロイの成功に加え、選んだ構成で次を確認します。動作確認は短時間のCPU/GPU実行を伴うため、モデル不要の小さなワークフローを使います。

- **認証と画面**: 未ログインのブラウザではAccessのログインが必要で、認証後にComfyUIの画面を操作できること。WebSocketが接続し、状態を受信できること。
- **直URLの保護**: Modalの `ui` URLへの認証なしリクエストが `401` になること。Accessを経由した画面は引き続き操作できること。
- **Splitの実行**: `EmptyImage`（64×64）→ `SaveImage` を1件実行し、出力を取得できること。[既存のスモーク用JSON](../../mcp/smoke-workflow.json)を参照できます。追加の確認が必要なら[Splitの検証手順](split-internals.md#実機検証観点と検証スクリプト)を使います。
- **Splitの停止**: 分離モードで処理後にGPUが0台へ戻ること。確認後はUIや接続を閉じ、CPUもアイドル停止することをModalの管理画面などで確認します。停止待ちにアプリの状態APIを繰り返し呼び出さないでください。
- **MCPを選んだ場合**: 未認証ではOAuthチャレンジが返り、別AccessアプリのJWTが拒否されること。本人のOAuthでツール一覧を取得し、スモーク用JSONを1件実行して出力を取得できること。再試行は同じ `request_id` を使い、二重投入を避けます。

完了報告には、選択した構成とMCPの有無、各確認の結果、未確認項目や残作業を記載します。ブラウザ操作などを本人へ引き継いだ場合は、その確認が済むまでセットアップ完了としないでください。接続先や設定の実値を引き継ぐ際はGit管理外で扱い、ドキュメントには書き込みません。
