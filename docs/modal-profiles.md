# Modal の複数アカウントを使い分ける

個人用と仕事用のように Modal アカウントが複数ある場合、**profile**（`~/.modal.toml` のセクション）で切り替える。

以下、`<profile>` は自分で登録したプロファイル名に読み替えること。

## profile と Environment は別物

| | 単位 | 用途 |
| --- | --- | --- |
| **profile** | アカウント（ワークスペース）ごとの資格情報 | 複数アカウントの使い分け。**こちらが今回の話** |
| **Environment** | 1 ワークスペース内の名前空間 | 同じアカウントで dev / prod を分ける |

`modal environment` は同じアカウントの中を分割するコマンドなので、別アカウントには使えない。

## 2 つ目のアカウントを登録する

Modal ダッシュボード → Settings → API Tokens でトークンを発行してから:

```bash
uv run modal token set --token-id ak-... --token-secret as-... --no-activate
uv run modal profile list
```

- `--profile` を省略すると**ワークスペース名がそのまま profile 名**になる。明示したい場合は `--profile <profile>` を付ける。
- **`--no-activate` が肝**。付けないと既定プロファイルが新しい方に奪われる。
- ブラウザ認証で入れる場合は `uv run modal token new --profile <profile> --no-activate`。
- 資格情報は `~/.modal.toml` に入る。リポジトリには入らないので commit されない。

## 切り替える

```bash
# 既定そのものを変える（~/.modal.toml の active が動く）
uv run modal profile activate <profile>

# そのコマンドだけ上書きする（既定は動かさない）
MODAL_PROFILE=<profile> uv run modal deploy comfyapp.py

# ラッパー経由（未登録プロファイルなら実行前に止まる）
./scripts/modal.sh <profile> deploy comfyapp.py
./scripts/modal.sh --list

# 今どれが有効か
uv run modal profile current
uv run modal profile list
```

`MODAL_PROFILE` はシェルの環境変数として渡すこと。**`.env` に書いても効かない**（`modal` は `import modal` の時点でプロファイルを確定するので、`comfyapp.py` の `load_dotenv()` では遅い）。黙って既定のアカウントへデプロイされると気付けないため、`comfyapp.py` と `preserve_model.py` は `.env` に `MODAL_PROFILE` があるとデプロイ前にエラーで止まる。

## ワークスペース単位のもの（取り違えると壊れる）

プロファイルを切り替えると、以下は**まるごと別物**になる。

- Volume: `comfy-model` / `comfy-inputs` / `comfy-outputs`。別アカウントでは空に見える。モデルの再アップロードが必要
- Secret: `huggingface-secret` は各アカウントで作り直す
- Proxy Auth トークン（`MODAL_KEY` / `MODAL_SECRET`）
- デプロイ URL: `https://<workspace>--<app>.modal.run` のように**ワークスペース名が URL に入る**

したがってアカウントを変えてデプロイし直した場合は、Cloudflare Access 用 Worker の `MODAL_ORIGINS` / `MODAL_KEY` / `MODAL_SECRET` を入れ直すこと。手順は [cloudflare-access.md](cloudflare-access.md) を参照。

## 資産管理画面（`web/`）の場合

`web/` の Hono サーバは `modal volume` CLI と `asset_rpc.py`（Modal SDK）を子プロセスとして起動し、その際に自分の環境変数を引き継ぐ。つまり**サーバを起動したシェルのプロファイルが使われる**。

```bash
cd web
npm start                              # 既定（active）プロファイル
MODAL_PROFILE=<profile> npm start      # 別アカウントの Volume を見る
```

画面ヘッダのバッジに接続先ワークスペースが出る。`/api/health` でも同じ情報を返す。

```bash
curl -s 127.0.0.1:7860/api/health
# {"status":"ok","modal":{"profile":"...","workspace":"..."}}
```

バッジが `workspace 不明`（`"modal": null`）のときは、`MODAL_PROFILE` に未登録の名前を指定しているか、Modal CLI にログインできていない。`uv run modal profile list` で確認する。

## CI での認証

CI ではプロファイルを作らず、トークンを環境変数で直接渡すのが簡単。

```bash
MODAL_TOKEN_ID=ak-... MODAL_TOKEN_SECRET=as-... uv run modal deploy comfyapp.py
```
