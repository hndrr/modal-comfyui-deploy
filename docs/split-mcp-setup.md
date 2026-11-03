# ComfyUI MCP の管理

すでに HTTPS の接続先がある場合は、[利用ガイド](split-mcp.md)から始めてください。このページは split の管理者向けの手順です。クライアントはすべて HTTPS `/mcp` と OAuth を使います。

操作の引数・戻り値は[ツール一覧](split-mcp-tools.md)、認証・保存データ・生成・管理の接続先は[経路図](design/split-mcp-routing.md)を参照してください。

## HTTPS の接続先を用意する

1. split の CPU イメージに固定版 Node.js と MCP をデプロイする（`scripts/modal.sh deploy splitapp.py`）。GPU に Node/MCP は不要。既存 Worker をテスト後にデプロイする。一般の中継ルートでは内部呼び出し用ヘッダーを必ず取り除く。
2. split ホスト全体を従来のブラウザ用 Access アプリに登録する。同じホストの `/mcp` に、より具体的なパスを持つ別の self-hosted Access アプリを設定し、Include が本人のメール1件のみの Allow を関連付ける。ブラウザ用共有アプリとは別の AUD を使う。Managed OAuth を有効にし、CLI のループバックと、ChatGPT が表示する**正確なコールバック URI**を許可する。トークンや本人メールは Git に記録しない。

   ChatGPT の登録フォームで動的クライアント登録（DCR）を選ぶと、コールバック URL が見えない場合がある。「OAuthの詳細設定 → 登録方法」で一度「ユーザー定義の OAuth クライアント」を選ぶと、このフォーム用の URL が表示される。それを Cloudflare の「許可されたリダイレクト URI」に追加して保存し、**同じフォームで DCR に戻して**プラグインを作成する。クライアント ID やシークレットの手入力、ワイルドカードの許可は不要。「接続」で Cloudflare に移ったら本人のログインを完了する。
3. Worker の secret `SPLIT_REMOTE_MCP` に次の JSON を対話入力する。既存の split ホストが解決する転送先と `origin` が一致しない限り中継されない。ホスト変更時は、既存の暗号化済みマップを置き換えずに `SPLIT_UI_ORIGIN` に `{"split.example.com":"https://example.modal.run"}` を設定できる。Worker の Custom Domain として登録する。

   ```json
   {"hostname":"split.example.com","split_hostname":"split.example.com","origin":"https://example.modal.run","audience":"<専用アプリのAUD>","owner_email":"owner@example.com"}
   ```

4. 認証なしでは OAuth のチャレンジとなること、別アプリの JWT では拒否されることを確認する。本人の OAuth で接続後、管理側から CPU/GPU の台数を読む。アプリの状態 API を定期照会すると CPU を起こすため、停止判定には使わない。

ChatGPT 添付が Azure Blob から配信される場合は、ChatGPT の添付で確認した配信ホストだけを `worker/` で `npx wrangler secret put SPLIT_ATTACHMENT_BLOB_HOSTS` にカンマ区切りで登録する。別のストレージアカウント、ワイルドカード、署名なしの Blob URL、リダイレクトは許可しない。配信ホストや署名付き URL は Git に残さない。

適用後は、Modal の管理画面で台数を見ながら、ツール一覧（CPU/GPU ともに0台）、モデル一覧（CPU のみ）、[小さな生成](../mcp/smoke-workflow.json)の順で確認する。生成には同じ `request_id` を使い、二重投入されないこと、出力を回収できること、監視終了後に両方0台へ戻ることを確認する。ジョブがなくても split の状態 API を呼び続けると CPU はアイドルにならない。

非対話環境で CLI の添付ヘルパーを使う場合は、この MCP 用に発行済みのアクセストークンを環境変数 `SPLIT_MCP_OAUTH_TOKEN` で渡せる。

HTTPS 接続を閉じるには、Worker の `SPLIT_REMOTE_MCP` を削除してから、専用の Access アプリを無効にする。ブラウザ用 Access アプリは維持する。

## 外部サービスの課金先を設定する

確認ページの `/mcp-approvals/*` には **通常のブラウザ用 Access アプリ**を適用する。MCP の OAuth アプリは `/mcp` のままにする。Worker は署名済みブラウザ JWT のメールが `SPLIT_REMOTE_MCP.owner_email` と一致する場合だけページを返す。MCP のアクセストークンでは閲覧・承認できない。

Comfy の Partner Node でクレジットを使う場合は、`COMFY_API_KEY` を持つ Modal Secret を作り、その**名前**をデプロイ時の `SPLIT_COMFY_BILLING_SECRET` に指定する。確認画面に表示する課金アカウントの説明は `SPLIT_COMFY_BILLING_LABEL` に指定できる（空なら Secret 名）。キー自体は GPU の実行時だけ使い、履歴・承認ページ・MCP の応答には含めない。未設定なら課金先の確認はできるが実行ボタンは無効になる。直接接続ノードでは、[対応するパックとプロバイダーの Secret](split-integrations.md) を設定する。

更新時は Worker と split の両方を更新する。MCP は専用の生成受付ルートにのみ送信するため、未対応の旧バックエンドには生成を投入できない。標準の ComfyUI ブラウザからの手動生成は従来どおり。これは MCP から投入する生成グラフの制御であり、任意の Python パックをサンドボックス化する機能ではない。インストール・更新するカスタムノードのコードは管理者が信頼できるものに限定する。

直接接続するGemini・OpenRouterの表示名は `SPLIT_GEMINI_BILLING_LABEL`・`SPLIT_OPENROUTER_BILLING_LABEL` で指定できる。未指定なら、実際に設定した各プロバイダーのSecret名を使う。CPUとGPUに同じ表示名を渡し、キーそのものは表示しない。

承認待ちの本文・送信内容・素材一覧は `data/state/review-payloads/` に分離して保存する。期限切れ・拒否・承認後は本文を削除する。素材の検証中はジョブ台帳をロックせず、検証後に承認状態と環境を再確認する。MCP子プロセスの接続先にも制限を置き、通常のHTTPクライアントを使って `/prompt` や管理操作へ迂回しても拒否する。新しいワークフローの保存と必要な照会だけを許可する。

## 保存済みメディアの配信用 CPU

1. 既存の split と同じ Modal プロファイル・環境で、先に更新した `splitapp.py` をデプロイし、一度CPUを起動する。起動時に既存台帳からジョブ別の保存記録と索引を作る。この更新ではComfyUIのバージョン・依存パッケージは変えない。
   次に `scripts/modal.sh deploy mediaapp.py` を実行する。既存の output / data / models Volume を参照し、モデルはファイル情報だけを読む。
2. Worker のテスト・型検査後、新しいコードをデプロイする。
3. Worker の secret `SPLIT_MEDIA_ORIGIN` に以下を登録する。実値は Git に残さない。認証情報は既存 split の Proxy Auth を再利用し、追加登録しない。

   ```json
   {"hostname":"split.example.com","origin":"https://example-media.modal.run"}
   ```

4. Modal の直接URLが未認証で拒否されることを確認する。管理APIから split CPU / GPU が0台であることを確認し、
   MCP の出力一覧・画像・音声・動画、モデル保存一覧、ワークフロー一覧・元JSON、履歴を取得する。この間起動してよいのは参照用 CPU だけ。
   通常・途中・末尾の Range を検証し、取得停止後は配信用 CPU も0台へ戻ることを確認する。

既存の `/view` と `/api/view` では、PNG・JPEG・WebP・GIF、MP3・WAV・FLAC・Ogg・Opus・M4A、MP4・MOV・WebM・MKVの元の output ファイルも同じ配信用 CPU を使う。
GLB・SVGなど未対応の形式、入力・一時プレビュー・画像変換は従来の ComfyUI 経路。詳細と未実装の R2 案は
[メディア配信の設計](design/split-media-delivery.md)を参照。

保存済み履歴の新しい読取経路は `data/state/saved-jobs.jsonl` と `data/state/saved-jobs/` を使う。台帳全体や承認待ちの本文を読み込まないため、`controller.json` が32 MiBを超えてもジョブ単位で取得できる。旧形式の読取互換は32 MiBまでなので、大きな既存台帳がある場合は手順1のCPU起動を先に済ませる。ジョブの保持期限・出力ファイルの保持方針は変わらない。

切り戻す場合は `SPLIT_MEDIA_ORIGIN` を削除する。旧経路に戻り、取得時に ComfyUI CPU を使う。
配信アプリを停止しても Volume は削除しない。設定済みの配信先に障害があるときは、暗黙には旧経路へ戻さない。

## ローカル stdio からの移行

HTTPS の登録と OAuth 接続を確認してから、旧 `comfyui-split` の stdio 登録だけをクライアント設定から外す。
`comfyui-remote` の HTTPS 登録と、CLI の素材転送ヘルパー `mcp/upload.mjs` は残す。
手元で artokun MCP サーバーを起動するランチャーは提供しない。
split CPU 内の `artokun/comfyui-mcp` はワークフロー編集・ノード照会などに引き続き使う。

旧接続専用の Access サービストークンは失効し、対応する Service Auth ポリシーと Worker の
`SPLIT_MCP_ACCESS` を外す。他用途のトークンやブラウザ用 Allow は削除しない。
Worker更新後は、旧専用トークンによる通常のAPI要求を410で拒否する。自動生成したキーで再投入する互換経路は残さない。
専用の `.env.comfy-mcp` は旧登録を外した後に削除できる。共有の `.env` は変更しない。

### モデルの取得元と上流版の更新

公開モデルの取得に追加設定は不要です。非公開・アクセス制限付きモデルを使う場合だけ、読み取り用の `HF_TOKEN` を Modal Secret に登録し、デプロイ時に `SPLIT_MODEL_HF_SECRET` でその Secret 名を指定します。ダウンロード専用の CPU 関数だけに渡されます。

上流を更新する場合は `mcp/package.json` とロックファイルを更新し、`mcp/surface.json` で増減したアクションの扱いを決めます。`node mcp/surface-doc.mjs` で[上流との対応表](split-mcp-upstream.md)を更新し、`npm --prefix mcp test` で実パッケージの登録スキーマと照合してください。

HTTPS側のツールや実行先を変更した場合は、[ツール一覧](split-mcp-tools.md)と[経路図](design/split-mcp-routing.md)も更新します。この2ページは利用者向けに編集する文書で、上流対応表の生成では上書きしません。`tools/list` と照合し、プレイヤー専用の内部ツールをエージェント向けの件数に含めないでください。
