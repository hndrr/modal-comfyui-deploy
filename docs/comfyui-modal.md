# ComfyUI を Modal で起動する


ローカル開発:

```bash
uv run modal serve comfyapp.py
```

デプロイ:

```bash
uv run modal deploy comfyapp.py
```

`comfyapp.py` は `modal.App(name="comfyui")` を定義し、ComfyUI を `8000` 番ポートの Web サーバーとして起動します。

## 実装上のポイント

- Python 3.12 ベース
- PyTorch 2.10.0 + CUDA 13.0 系の wheel を利用
- `xformers`、`flash-attn`、`SageAttention` を組み込み
- `comfy-cli` で ComfyUI をインストール
- custom node を起動時イメージへ組み込み
- ComfyUI の `models` / `custom_nodes` / `output` / `input` / `user` を Modal Volume に接続
- ComfyUI の user data API を起動時に補正し、`user/workflows` 配下の workflow JSON を保存できるようにする
- ComfyUI の WebSocket 圧縮を起動時に無効化する（下記）

### WebSocket 圧縮（permessage-deflate）を切っている理由

Modal のプロキシは [permessage-deflate（RFC 7692）に未対応](https://modal.com/docs/guide/webhooks)で、圧縮を合意した WebSocket をフレーム送出前に閉じます。ComfyUI は `web.WebSocketResponse()` を既定設定で生成し、ブラウザは既定で圧縮を提示するため、そのままだと**すべての WebSocket 接続が即座に切れます**。

無効化する CLI フラグが無いので、`patch_websocket_compression()` が起動時に `server.py` の `web.WebSocketResponse()` を `web.WebSocketResponse(compress=False)` へ書き換えています。

この処理が効いていないと、次の症状が出ます。

- Modal のログに `CONNECT /ws -> 101` が 1 秒間隔で並び続ける（正常なら 1 本張ったきり完了イベントが出ない）
- `GET /api/jobs` が毎秒何度も飛ぶ（リアルタイム通知が来ずポーリングに落ちるため）
- ComfyUI Manager のノードインストールが無反応に見える（進捗と完了が WebSocket で push されるため）

切り分けるときは、圧縮の有無を変えて接続すると一発で分かります。

```bash
uv run --with websockets python -c "
import asyncio, websockets
async def main():
    url = 'wss://<workspace>--comfyui-ui.modal.run/ws?clientId=diag'
    async with websockets.connect(url, open_timeout=30) as ws:   # 既定=圧縮あり
        print(await asyncio.wait_for(ws.recv(), timeout=15))
asyncio.run(main())
"
# 正常: {"type": "status", ...} が届く
# 異常: フレーム無しで即クローズ（compression=None を渡すと届く）
```

### ComfyUI Manager からのノードインストール

既定では Manager のノードインストールが**拒否されます**。押しても次のエラーが出ます。

```text
[ERROR] ERROR: To use this action, security_level must be `normal or below`,
and network_mode must be set to `personal_cloud`.
```

ComfyUI-Manager v4 の `network_mode` が既定で `public` のためです。`public` では `security_level` が何であってもノードパックのインストールは通りません。

許可するには `COMFYUI_MANAGER_INSTALL=on` にしてデプロイし直します。起動時に `user/__manager/config.ini` へ次を書き込みます（`user/` は Volume なので設定は残ります）。

```ini
[default]
network_mode = personal_cloud
security_level = normal
```

`off` に戻すと `network_mode = public` を書き戻すので、許可が残り続けることはありません。他のキー（`channel_url` など）には触りません。`security_level` を自分で `strong` にしている場合はそちらを尊重し、インストールできない旨を警告に出します。

**有効にする前に、その URL を誰が開けるかを確認してください。** ノードインストールは任意のコードとその依存を実行環境に入れる操作です。`COMFYUI_REQUIRES_PROXY_AUTH=off` のまま公開している場合、URL を知っている人が同じことをできます。

なお Manager 経由で入れたノードの Python 依存は `site-packages` に入るため、コンテナが落ちると消えます（ノード本体は `custom_nodes` Volume に残ります）。恒久的に使うノードは `comfyapp.py` の `NODES` に足してイメージへ焼く方が確実です。

## 永続化に使う Volume

- `comfy-model`
- `comfy-custom-nodes`
- `comfy-outputs`
- `comfy-inputs`
- `comfy-user-data`

## GPU 切り替え

GPU はコード編集ではなく環境変数 `COMFYUI_GPU_PROFILE` で切り替えます。

利用可能な値:

- `rtx-pro-6000` 既定値
- `h100`
- `a100-80gb`

各プロファイルの対応:

- `rtx-pro-6000` -> Modal GPU `RTX-PRO-6000` / `TORCH_CUDA_ARCH_LIST=12.0+PTX`
- `h100` -> Modal GPU `H100` / `TORCH_CUDA_ARCH_LIST=9.0`
- `a100-80gb` -> Modal GPU `A100-80GB` / `TORCH_CUDA_ARCH_LIST=8.0`

例:

```bash
COMFYUI_GPU_PROFILE=h100 uv run modal serve comfyapp.py
```

## 環境変数

`.env.example`:

```env
COMFYUI_GPU_PROFILE=rtx-pro-6000
COMFYUI_SAGE_ATTENTION=on
COMFYUI_REQUIRES_PROXY_AUTH=off
COMFYUI_SCALEDOWN_WINDOW=30
COMFYUI_FUNCTION_TIMEOUT=1800
COMFYUI_CLI_ARGS=
COMFYUI_FORCE_BUILD=on
COMFYUI_MANAGER_INSTALL=off
```

意味:

- `COMFYUI_GPU_PROFILE`: 使用する GPU プロファイル
- `COMFYUI_SAGE_ATTENTION`: `on` または `off`
- `COMFYUI_REQUIRES_PROXY_AUTH`: Modal proxy auth を要求する場合は `on`
- `COMFYUI_SCALEDOWN_WINDOW`: 接続終了後にコンテナを縮退するまでの最大秒数（`2`〜`1200`、既定値 `30`）
- `COMFYUI_FUNCTION_TIMEOUT`: 1入力または接続の最大実行秒数（`1`〜`86400`、既定値 `1800`）
- `COMFYUI_CLI_ARGS`: `comfy launch -- ...` の末尾に追加する引数
- `COMFYUI_FORCE_BUILD`: ComfyUIのインストール層以降を再ビルドする場合は `on`
- `COMFYUI_MANAGER_INSTALL`: ComfyUI Manager からノードをインストールする場合は `on`（既定 `off`、下記）

`COMFYUI_SAGE_ATTENTION=on` が既定です。`COMFYUI_CLI_ARGS` に `--use-sage-attention` を自分で含めていない限り、自動で付与されます。

ComfyUI をその時点の最新版に更新してデプロイする場合:

```bash
COMFYUI_FORCE_BUILD=on uv run modal deploy comfyapp.py
```

この指定では CUDA、PyTorch、SageAttention のビルド層には既存キャッシュを利用し、ComfyUI のインストール層とそれ以降の層だけを再ビルドします。

Modal の公開 URL に `Modal-Key` / `Modal-Secret` ヘッダーを必須にする場合:

```bash
COMFYUI_REQUIRES_PROXY_AUTH=on uv run modal serve comfyapp.py
```

`COMFYUI_REQUIRES_PROXY_AUTH=off` が既定です。`on` にすると通常のブラウザアクセスでは開けないため、ヘッダーを付与できるクライアントやプロキシ経由で利用します。

## Cloudflare Access でログイン画面を付ける

ブラウザからそのまま使いたい場合は、Cloudflare Access を ComfyUI の手前に置き、Cloudflare Worker に `Modal-Key` / `Modal-Secret` を付与させる構成が使えます。ComfyUI 本体にも Modal にもログイン機能を足さずに、メールのワンタイム PIN や Google ログインで利用者を制限できます。

構成の説明とセットアップ手順は [cloudflare-access.md](cloudflare-access.md) にまとめています。

## アイドル時のscale-to-zero

ComfyUI用Functionは `min_containers=0` のため、アクティブな入力がなければGPUコンテナをゼロ台まで縮退できます。`COMFYUI_SCALEDOWN_WINDOW` は、最後の入力が終了してから縮退するまでの最大アイドル時間です。短くするとアイドル中のコンピュート消費を抑えやすくなりますが、次回アクセス時のコールドスタートが増えます。

`COMFYUI_FUNCTION_TIMEOUT` はアイドル停止時間ではありません。生成処理を含む1入力やWebSocket接続を継続できる最大時間です。長時間の生成を行う場合は、想定する処理時間より長い値を指定してください。

例として、接続終了から10秒で縮退対象にし、Function timeoutを1時間にする場合:

```bash
COMFYUI_SCALEDOWN_WINDOW=10 \
COMFYUI_FUNCTION_TIMEOUT=3600 \
uv run modal deploy comfyapp.py
```

これらはデプロイ時に決まる設定なので、変更後は再デプロイが必要です。不正な整数や許容範囲外の値を指定すると、イメージビルド前にエラーになります。

ComfyUIはブラウザとの状態同期にWebSocketを使います。生成が終わっていてもComfyUIのタブやAPIクライアントが接続中だと、Modalではアクティブな入力として扱われ、scale-to-zeroしない可能性があります。確実に停止させる場合は、生成完了後にすべてのComfyUIタブとクライアントを閉じてください。再アクセス時は同じURLからコールドスタートします。

GPUコンテナがゼロ台になった後も、モデル、入力、出力、workflowなどを保持するModal Volumeのストレージは維持されます。

## 追加される custom nodes

- `https://github.com/crystian/ComfyUI-Crystools`
- `https://github.com/Firetheft/ComfyUI_Local_Media_Manager`
- `https://github.com/hayden-fr/ComfyUI-Image-Browsing`
- `https://github.com/rgthree/rgthree-comfy`

![ComfyUI](../assets/2025-09-28-21-11-34.png)
