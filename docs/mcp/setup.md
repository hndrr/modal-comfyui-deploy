# Split app の MCP 設定・運用ガイド

> [!NOTE]
> 本ページは、CPU/GPU 分離構成（`splitapp.py`）の**管理者向けドキュメント**です。MCP の接続先構築、外部サービス連携、環境やモデルの管理手順を説明します。  
> すでに接続先 URL を案内されている一般の利用者は、[MCP 利用ガイド](usage.md) を参照してください。

クライアント（ChatGPT Web、Claude Code、Codex 等）はすべて HTTPS `/mcp` エンドポイントおよび OAuth 認証を使用して接続します。
- 操作引数・戻り値: [ツールリファレンス](tools.md)
- 認証・保存データ・生成・管理の接続先: [経路図](../design/split-mcp-routing.md)

---

## 目次

1. [HTTPS 接続先と Cloudflare Access の設定](#https-接続先と-cloudflare-access-の設定)
2. [画像ビューアの導入・更新順序](#画像ビューアの導入更新順序)
3. [外部サービス課金・承認の設定](#外部サービス課金承認の設定)
4. [保存済みメディア配信用 CPU（mediaapp）の設定](#保存済みメディア配信用-cpumediaappの設定)
5. [カスタムノードとモデルの管理](#カスタムノードとモデルの管理)
6. [ローカル stdio からの移行](#ローカル-stdio-からの移行)

---

## HTTPS 接続先と Cloudflare Access の設定

以下のプレースホルダーを、取得元で確認した値に置き換えて設定を進めます。  
※`ORIGIN` は `https://` を含み、パスと末尾の `/` を含まない URL です。

| プレースホルダー | 説明・取得元 |
| :--- | :--- |
| `<SPLIT_PUBLIC_HOST>` | Cloudflare Worker の Custom Domain に登録する split の公開ホスト名（`https://` やパスは含めない） |
| `<SPLIT_MODAL_ORIGIN>` | `splitapp.py` のデプロイ出力に表示される `ui` の Web URL |
| `<MCP_ACCESS_AUD>` | 次の手順で作成する MCP 専用 Cloudflare Access アプリの Application Audience (AUD) Tag |
| `<OWNER_EMAIL>` | MCP 専用 Access アプリの Allow ポリシーに登録する管理者本人のメールアドレス |

### 手順1: split CPU イメージのデプロイ
1. `scripts/modal.sh deploy splitapp.py` を実行し、split の CPU イメージに固定版 Node.js と MCP サーバーをデプロイします（GPU コンテナに Node.js / MCP は不要です）。
2. 既存の Worker をテストした上でデプロイします。一般の中継ルートでは、内部呼び出し用ヘッダーを必ず除去してください。

### 手順2: Cloudflare Access アプリの作成
1. split ホスト全体（`<SPLIT_PUBLIC_HOST>`）を、従来のブラウザ用 Access アプリに登録します。
2. 同じホストの `/mcp` パスに対して、より具体的なパス一致を持つ**別の self-hosted Access アプリ**を作成します。
   - **ポリシー設定**: Include に管理者本人のメール（`<OWNER_EMAIL>`）1 件のみを指定した Allow ポリシーを関連付けます（ブラウザ用共有アプリとは別の AUD を使用）。
   - **Managed OAuth の有効化**: CLI のループバックアドレス、および ChatGPT が表示する**正確なコールバック URI**を許可します。
   - ※認証トークンやメールアドレスの実値を Git に記録しないでください。

> [!TIP]
> **ChatGPT 登録時のコールバック URI 確認手順**  
> ChatGPT の登録フォームで動的クライアント登録（DCR）を選択すると、コールバック URL が表示されない場合があります。その場合は「OAuthの詳細設定 → 登録方法」で一度「ユーザー定義の OAuth クライアント」を選択するとフォーム用の URL が表示されます。  
> 表示された URL を Cloudflare の「許可されたリダイレクト URI」に追加して保存した後、**同じフォームで再度 DCR に戻して**プラグインを作成してください。クライアント ID やシークレットの手入力、ワイルドカードの許可は不要です。「接続」を押して Cloudflare 画面へ遷移したらログインを完了します。

### 手順3: Cloudflare Worker へのシークレット登録
1. Worker のシークレット `SPLIT_REMOTE_MCP` に以下の JSON を対話入力して登録します。
   ```json
   {"hostname":"<SPLIT_PUBLIC_HOST>","split_hostname":"<SPLIT_PUBLIC_HOST>","origin":"<SPLIT_MODAL_ORIGIN>","audience":"<MCP_ACCESS_AUD>","owner_email":"<OWNER_EMAIL>"}
   ```
   ※既存の split ホストが解決する転送先と `origin` が一致しない限り、Worker はリクエストを中継しません。  
   ※ホスト名変更時は、既存の暗号化済みマップを置き換えずに `SPLIT_UI_ORIGIN` に `{"<SPLIT_PUBLIC_HOST>":"<SPLIT_MODAL_ORIGIN>"}` を設定することも可能です。
2. Worker の Custom Domain として登録します。

### 手順4: 認証と動作の検証
1. **未認証アクセスの検証**: 認証なしアクセスで OAuth チャレンジが返ること、および別アプリの JWT ではアクセスが拒否されることを確認します。
2. **本人の接続と台数監視**: 本人の OAuth で接続後、管理側から CPU/GPU のコンテナ数を確認します。
   - ツール一覧の取得（CPU/GPU ともに 0 台）
   - モデル一覧の取得（CPU のみ起動）
   - [小さな生成ワークフロー](../../mcp/smoke-workflow.json)の投入（GPU 起動、生成、出力回収）
3. **二重投入と停止の確認**: 生成には同じ `request_id` を使用し、二重投入されないこと、出力を回収できること、監視終了後に CPU/GPU ともに 0 台へ戻ることを確認します。
   - ※アプリの状態 API を定期照会し続けると CPU がスリープしないため、停止判定のポーリングには使用しないでください。

#### 添付ファイル・CLI 用オプション
- **Azure Blob からの添付配信**: ChatGPT の添付ファイルが Azure Blob から配信される場合、確認した配信ホストのみを `worker/` ディレクトリで `npx wrangler secret put SPLIT_ATTACHMENT_BLOB_HOSTS` にカンマ区切りで登録してください（別のストレージアカウント、ワイルドカード、署名なし URL、リダイレクトは拒否されます）。
- **非対話 CLI の利用**: 非対話環境で CLI の添付ヘルパーを使う場合は、発行済みのアクセストークンを環境変数 `SPLIT_MCP_OAUTH_TOKEN` で渡すことができます。
- **HTTPS 接続の解除**: Worker の `SPLIT_REMOTE_MCP` を削除してから専用 Access アプリを無効化します（ブラウザ用 Access アプリはそのまま維持します）。

---

## 画像ビューアの導入・更新順序

既存環境の `get_image` を Worker の画像取得経路へ切り替える場合は、**必ず Worker を先に反映**してください。配信用 CPU を設定していない構成で Modal 側を先に更新すると、旧 Worker が呼び出す上流の `get_image` が削除されているためエラーになります。

1. **Worker のデプロイ**: テストおよび型検査を完了させた後、`worker/` ディレクトリで `npm run deploy` を実行します。
2. **Modal アプリのデプロイ**: リポジトリのルートで `scripts/modal.sh deploy splitapp.py` を実行します（`mcp/remote-call.mjs` と `mcp/surface.json` は CPU イメージに組み込まれるため、Worker の反映だけでは更新されません）。
3. **クライアントでの確認**: ChatGPT の接続設定でツールを更新し、生成済みファイルを `get_image` で取得します。画像ビューアと、ログイン後に開ける閲覧リンクが正常に表示されることを確認します。

---

## 外部サービス課金・承認の設定

### 承認画面の保護
- 承認確認ページ（`/mcp-approvals/*`）には、**通常のブラウザ用 Access アプリ**を適用します（MCP 用 OAuth アプリの `/mcp` パスとは分離します）。
- Worker は署名済みブラウザ JWT のメールアドレスが `SPLIT_REMOTE_MCP.owner_email` と一致する場合にのみ承認ページを返します。MCP のアクセストークンではページの閲覧・承認は行えません。

### 外部サービス連携の設定

| 連携対象 | 設定方法 | 備考 |
| :--- | :--- | :--- |
| **Comfy Partner Node** | `COMFY_API_KEY` を持つ Modal Secret を作成し、デプロイ時に Secret 名を `SPLIT_COMFY_BILLING_SECRET` に指定 | 確認画面の表示名は `SPLIT_COMFY_BILLING_LABEL` で指定可能（空なら Secret 名）。キー自体は GPU 実行時のみ使用し、応答や履歴には含めない |
| **Gemini（直接接続）** | 対応する Secret を設定し、デプロイ時に `SPLIT_GEMINI_BILLING_LABEL` で表示名を指定 | 未指定の場合は実際の Secret 名を表示 |
| **OpenRouter（直接接続）** | 対応する Secret を設定し、デプロイ時に `SPLIT_OPENROUTER_BILLING_LABEL` で表示名を指定 | 未指定の場合は実際の Secret 名を表示 |

※直接接続ノードの設定詳細は[対応するパックとプロバイダーの Secret](../guides/split-integrations.md) を参照してください。

### セキュリティとペイロード管理
- 承認待ちの本文、送信内容、素材一覧は `data/state/review-payloads/` に分離して保存されます。期限切れ、拒否、承認完了時には本文が自動削除されます。
- 素材の検証中はジョブ台帳をロックせず、検証完了後に承認状態と環境を再確認します。
- MCP 子プロセスの接続先も厳格に制限されており、通常の HTTP クライアントを使って `/prompt` や管理操作へ迂回しても拒否されます（ワークフローの保存と必要な照会のみ許可）。
- 通常のインストール済みノードは、MCP専用の許可リスト登録を要求せずCPU/GPU振り分けへ進みます。外部送信の判定には環境側の `api_node`・実装元と既知の直接API連携を使います。これらを宣言しない任意パックの通信を自動検出する機能ではありません。
- Manager等で追加した通常パックも、環境に読み込まれるとMCPから実行可能になります。ノードが提供するサーバー上のパス・URLの読み込みやファイル保存も、この信頼範囲に含まれます。ローカル処理の入力すべてを素材固定や外部API用の入力検査に通す仕組みではありません。
- 本機能は MCP から投入される生成グラフの制御を目的としており、任意の Python パックを完全にサンドボックス化するものではありません。インストールするカスタムノードは信頼できるものに限定してください。

---

## 保存済みメディア配信用 CPU（mediaapp）の設定

過去の生成画像・音声・動画の参照を独立した配信用 CPU（`mediaapp.py`）へ分離することで、ComfyUI 本体の CPU を起動させずにメディアを高速配信できます。

`<MEDIA_MODAL_ORIGIN>` は `mediaapp.py` のデプロイ出力に表示される Web URL です（パスと末尾の `/` は除きます）。

### 構築手順
1. **索引の作成と mediaapp デプロイ**:
   - 既存の split と同じ Modal プロファイル・環境で、更新済みの `splitapp.py` をデプロイし、一度 CPU を起動します（起動時に既存台帳からジョブ別の保存記録と索引を作成します）。
   - 続いて `scripts/modal.sh deploy mediaapp.py` を実行します（既存の output / data / models Volume を参照し、モデルはメタデータのみ読み込みます）。
2. **Worker のデプロイ**: Worker のテスト・型検査後、新しいコードをデプロイします。
3. **Worker へのシークレット登録**:
   Worker のシークレット `SPLIT_MEDIA_ORIGIN` に以下を登録します（認証情報は既存 split の Proxy Auth を再利用します）。
   ```json
   {"hostname":"<SPLIT_PUBLIC_HOST>","origin":"<MEDIA_MODAL_ORIGIN>"}
   ```
4. **配信と停止の検証**:
   - Modal の直接 URL が未認証アクセスで拒否されることを確認します。
   - split CPU / GPU がともに 0 台の状態で、MCP 経由の出力一覧、画像・音声・動画、モデル一覧、ワークフロー一覧・JSON、履歴が取得できることを確認します（この間、配信用 CPU のみが起動します）。
   - 通常・途中・末尾の Range リクエストを検証し、取得停止後は配信用 CPU も 0 台へ戻ることを確認します。

### 対応フォーマットと読み込み仕様
- **対応フォーマット**:
  - 画像: PNG, JPEG, WebP, GIF
  - 音声: MP3, WAV, FLAC, Ogg, Opus, M4A
  - 動画: MP4, MOV, WebM, MKV
- **非対応フォーマット**: GLB、SVG など未対応の形式や、入力・一時プレビュー・画像変換処理は従来の ComfyUI 経路で配信されます（詳細は [メディア配信の設計](../design/split-media-delivery.md) を参照）。
- **履歴読み込みの軽量化**:
  - 保存済み履歴は `data/state/saved-jobs.jsonl` と `data/state/saved-jobs/` を使用してジョブ単位で取得されます。
  - これにより、台帳ファイル `controller.json` が 32 MiB を超えている場合でも安全に取得できます（旧形式の互換読み込み上限は 32 MiB）。
- **切り戻し**: `SPLIT_MEDIA_ORIGIN` を削除すると、メディア取得時に ComfyUI CPU を使用する旧経路へ戻ります。

---

## カスタムノードとモデルの管理

### カスタムノードの追加・更新

インストール済みクラスの確認は `list_nodes`、追加パックの検索は `search_custom_nodes` を使用します。変更は必ず検証用の「候補環境」で行います。

1. **候補環境の作成**: `get_split_status` で稼働中のバージョン（`active`）を取得し、`prepare_environment(active, request_id)` で検証用コピーの作成を開始します。`status: "running"` は準備中を示すため、同じ `request_id` の `get_admin_operation` で完了を確認し、`status: "completed"` の結果に含まれる `candidate` を使用してください（※準備中・候補環境が存在する間、通常生成は受け付けません）。
2. **ノードの操作**: `change_node(candidate, action: "install" または "update", node, request_id)` にパック ID を渡して実行します。結果は `get_admin_operation(request_id)` で確認します（`running` はまだ処理中を示します）。
3. **反映または破棄**: `apply_environment(candidate, request_id)` で候補環境を CPU および GPU で検証し、成功時のみ有効環境を切り替えます。反映しない場合は `discard_environment` で破棄できます。

### モデルの追加・退避・復元

- **モデルの追加**: `list_models` でカテゴリを確認し、`add_model(repo, file, category, request_id)` で Hugging Face から safetensors、GGUF、ONNX などのファイルを取得・追加します（専用 CPU でダウンロードするため GPU は起動しません。必要に応じて `revision` や `sha256` を指定可能）。
- **モデルの退避（削除）**: `inspect_model(path)` で取得した `version`（ファイル全体の SHA-256）を `remove_model(path, version, request_id)` に渡して退避します。
  - ※16 MiB を超える大容量ファイルでは `inspect_model` が非同期処理となり、初回応答は `status: "running"` と `request_id` が返り `version` はまだ含まれません。返された ID で `get_admin_operation(request_id)` を呼び、`status: "completed"` になった結果の `result.version` を使用してください。
  - ※退避処理（`remove_model`）自体も非同期となるため、同様に `get_admin_operation(request_id)` で完了を確認してください。対象ファイルのみを退避します。
- **モデルの復元**: `restore_model` を使用します。`removal_id` には**削除時の** `request_id` を、`request_id` には**今回の復元操作用の新しい ID** を指定してください（削除時の ID を両方に指定すると競合します）。追加・復元処理が既存ファイルを上書きすることはありません。
- **通信切断時の対応**: 管理操作の通信が途切れた場合は、同じ `request_id` で結果を確認してください。`unknown` の場合は対象の状態を確認してから次の操作を判断してください。

### モデルの取得元と上流版の更新
- **Hugging Face Token の設定**: 非公開・ゲート付きモデルを取得する場合のみ、読み取り権限を持つ `HF_TOKEN` を Modal Secret に登録し、デプロイ時に `SPLIT_MODEL_HF_SECRET` で Secret 名を指定してください（ダウンロード専用の CPU 関数のみに安全に渡されます）。
- **MCP 上流コードの更新**:
  1. `mcp/package.json` およびロックファイルを更新します。
  2. `mcp/surface.json` でアクションの増減を確認・整理します。
  3. `node mcp/surface-doc.mjs` を実行して[上流との対応表](upstream.md)を更新します。
  4. `npm --prefix mcp test` で実パッケージのスキーマと照合します。
  5. ツールやエンドポイントを変更した場合は、[ツールリファレンス](tools.md)および[経路図](../design/split-mcp-routing.md)を手動更新してください。

---

## ローカル stdio からの移行

HTTPS 接続と OAuth 認証が正常に動作することを確認した上で、旧設定を整理します。

1. **クライアント設定の整理**: 旧 `comfyui-split` の stdio 登録をクライアント設定から削除します（`comfyui-remote` の HTTPS 登録および素材転送ヘルパー `mcp/upload.mjs` は維持）。
   - ※手元でローカル MCP サーバーを起動するランチャーは提供していません。split CPU 内の `artokun/comfyui-mcp` はワークフロー編集やノード照会などに引き続き使用されます。
2. **Access サービストークンの失効**: 旧接続専用の Access サービストークンを失効させ、対応する Service Auth ポリシーおよび Worker の `SPLIT_MCP_ACCESS` を解除します（他用途のトークンやブラウザ用 Allow は維持）。
   - ※Worker 更新後は、旧専用トークンによる通常 API リクエストに対して `410 Gone` を返します。
3. **環境変数ファイルの削除**: 専用の `.env.comfy-mcp` は旧登録の解除後に削除できます（共有の `.env` は変更しません）。
