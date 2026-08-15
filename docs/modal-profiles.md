# Modal のアカウントを 2 つ以上使う場合

Modal アカウントが 1 つなら、このページは不要。`uv run modal deploy comfyapp.py` がそのまま動く。

アカウントが 2 つあると困るのは、**どのコマンドがどっちのアカウントに行くのか分からなくなる**こと。Volume（モデルや出力）はアカウントごとに完全に別物なので、間違えると「昨日まであったモデルが消えた」ように見える。

このリポジトリは `scripts/modal.sh` で、**このフォルダで使うアカウントを 1 回決めておく**方式にしている。

## 手順

以下に出てくる `personal` と `work` は**例のアカウント名**なので、自分の環境の名前に読み替えること。実際の名前は手順 1 で確認する。

### 1. 2 つ目のアカウントを Modal CLI に教える（1 回だけ）

Modal ダッシュボード → Settings → API Tokens でトークンを発行して:

```console
$ uv run modal token set --token-id ak-xxxx --token-secret as-xxxx --no-activate
```

`--no-activate` を必ず付ける。付けないと、今使っているアカウントが新しい方に置き換わる。

登録できたか見る:

```console
$ ./scripts/modal.sh --list
pinned profile: (なし: ~/.modal.toml の既定を使用)
┏━━━┳━━━━━━━━━━┳━━━━━━━━━━━┓
┃   ┃ Profile  ┃ Workspace ┃
┡━━━╇━━━━━━━━━━╇━━━━━━━━━━━┩
│ • │ personal │ personal  │
│   │ work     │ work-team │
└───┴──────────┴───────────┘
```

**`Profile` 列がアカウントの呼び名**。次の手順でこの文字列を使う。

この名前は登録時に決まる。`token set` に `--profile` を付けなければ Modal のワークスペース名がそのまま入り、`--profile <好きな名前>` を付ければ自分で決められる（上の例の `work` は、ワークスペース名 `work-team` に対して自分で付けた名前）。

### 2. このフォルダで使うアカウントを決める（1 回だけ）

`work` の部分に、手順 1 の `Profile` 列で見た名前を入れる。

```console
$ ./scripts/modal.sh use work
pinned: work
```

登録していない名前を入れた場合は、そこで止まって登録済みの一覧を出す。

### 3. 以降は `uv run modal` の代わりに `./scripts/modal.sh` を使う

```console
$ ./scripts/modal.sh deploy comfyapp.py
modal: using profile 'work' (.modal-profile)
...
```

毎回アカウント名を打つ必要はない。手順 2 で決めた `work` に固定されている。

管理画面 (`web/`) も同じアカウントを見るので、いつもどおり `npm start` でよい。

## 確認したいとき

```console
$ ./scripts/modal.sh --list
pinned profile: work
```

管理画面は、画面上部に接続先が出る。

```
Modal ComfyUI Asset Manager  [ workspace: work-team ]
```

## 変えたいとき

```console
$ ./scripts/modal.sh use personal    # このフォルダを personal に変更
$ ./scripts/modal.sh use --clear     # 決めるのをやめる（Modal CLI の既定に戻る）
```

管理画面を起動したまま変えても切り替わらない。`npm start` をやり直す。

1 コマンドだけ別アカウントで叩きたいときは、決めた内容を変えずに済ませられる:

```console
$ ./scripts/modal.sh --profile personal volume ls comfy-model /
modal: using profile 'personal' (--profile)
```

## どのコマンドが固定先を使うか

| 起動するもの | 固定先を使うか |
| --- | --- |
| `./scripts/modal.sh <modal のコマンド>` | 使う |
| `cd web && npm start`（資産管理画面） | 使う |
| `uv run preserve_model_gui.py`（モデル保存 GUI） | 使う |
| `uv run python rename_volume.py ...` / `move_volume_file.py ...` | 使う |
| `uv run modal ...` を素で打つ | **使わない**（Modal CLI の既定） |

`uv run python ...` で起動するスクリプトは、`import modal` の前に固定先を `MODAL_PROFILE` へ反映している。素の `uv run modal` だけは Modal CLI が先にプロファイルを確定してしまうので、後から差し込めない。

つまり `uv run modal` で始まるコマンドは、すべて `./scripts/modal.sh` に置き換える。引数はそのままでよい。

```bash
uv run modal deploy comfyapp.py                 → ./scripts/modal.sh deploy comfyapp.py
uv run modal run preserve_model.py::preserve_model ...
                                                → ./scripts/modal.sh run preserve_model.py::preserve_model ...
uv run modal volume ls comfy-model /            → ./scripts/modal.sh volume ls comfy-model /
```

GUI やサーバーは置き換え不要で、その起動だけ別アカウントにしたいときだけ環境変数で上書きする。

```bash
MODAL_PROFILE=<profile> uv run preserve_model_gui.py
MODAL_PROFILE=<profile> npm start
```

## 資産管理画面（`web/`）の場合

サーバーは `modal volume` CLI と `asset_rpc.py`（Modal SDK）を子プロセスとして起動し、どちらにも固定先を渡す。起動方法はいつもどおりで、ラッパーは要らない。

```bash
cd web
npm start                              # 固定先（無ければ Modal CLI の既定）
MODAL_PROFILE=<profile> npm start      # この起動だけ別アカウント
```

**固定先を変えたらサーバーを再起動する。** Modal SDK を抱えたワーカーが常駐しているため、起動したまま `.modal-profile` を書き換えても切り替わらない。

接続先は画面ヘッダのバッジに出る。ホバーするとプロファイル名と、どの設定から来たかが出る。

```text
Modal ComfyUI Asset Manager  [ workspace: work-team ]
```

`/api/health` でも同じ情報を返す。

```bash
curl -s 127.0.0.1:7860/api/health
# {"status":"ok","modal":{"profile":"work","workspace":"work-team","source":"repo"}}
```

`source` は設定の出どころで、`env`（シェルの `MODAL_PROFILE`）/ `repo`（`.modal-profile`）/ `active`（`~/.modal.toml` の既定）のいずれか。意図した経路で切り替わっているかはここで分かる。

Volume はアカウントごとに別物なので、切り替えると一覧の中身も丸ごと変わる。空に見えるときはまずバッジを見る。

## Cloudflare Access を使っている場合

Access 越しの公開はアカウントを跨げない。切り替えたら Worker 側の secret を入れ直す。

| secret | 入れ直す理由 |
| --- | --- |
| `MODAL_ORIGINS` | デプロイ URL に workspace 名が入るため（`https://<workspace>--<app>.modal.run`） |
| `MODAL_KEY` / `MODAL_SECRET` | Proxy Auth トークンがワークスペース単位のため。新しいアカウントで作り直す |

`MODAL_ORIGINS` はホスト名 → オリジンのマップなので、**両アカウント分のホスト名を 1 つの Worker に並べて置ける**。

```json
{
  "comfy.example.com": "https://<本番の workspace>--comfyui-ui.modal.run",
  "comfy-test.example.com": "https://<検証の workspace>--comfyui-ui.modal.run"
}
```

ただし `worker/src/index.ts` は `MODAL_KEY` / `MODAL_SECRET` を**1 組しか持てない**ため、この並記だけでは片方のアカウントにしか認証が通らない。両方を Access で公開するなら、**アカウントごとに Worker を立てる**のが素直である（コード変更が不要で、検証側の設定ミスが本番を巻き込まない）。1 つの Worker に相乗りさせるには、ホスト名ごとに資格情報を持たせる改修が要る。

検証用アカウントで動作確認したいだけなら、Access を組まずに `COMFYUI_REQUIRES_PROXY_AUTH=off` で直 URL を開き、終わったら `./scripts/modal.sh app stop <app-id>` で止めるほうが早い。ただしその間 URL を知っていれば誰でも開ける状態になる。

設定手順そのものは [cloudflare-access.md](cloudflare-access.md) を参照。

## つまずきやすいところ

**`uv run modal ...` を素で打つと、決めた内容が無視される**

手順 2 で決めた内容は Modal CLI 本体の機能ではないので、`scripts/modal.sh` を通さないと効かない。素で打った場合は Modal CLI の既定アカウント（`uv run modal profile current` で分かる）に行く。デプロイなら別アカウントに出てしまい、`modal run preserve_model.py::preserve_model` なら別アカウントの Volume にモデルが保存される。

**`.env` に `MODAL_PROFILE=work` と書いても効かない**

書いても無視される。デプロイ時にエラーで止まるようにしてあるので、気付かず間違ったアカウントに出ることはない。アカウントの指定は手順 2 でやる。

**管理画面のバッジが「workspace 不明」になる**

決めたアカウント名が Modal CLI に登録されていない（打ち間違いなど）か、Modal CLI にログインできていない。`./scripts/modal.sh --list` で名前を確認する。

**アカウントを変えたらモデルが空になった**

正常。Volume はアカウントごとに別物で、共有されない。以下も同様にアカウントごとに用意が必要:

- Volume（`comfy-model` / `comfy-inputs` / `comfy-outputs`）
- Secret（`huggingface-secret`）。無いと `preserve_model` のデプロイと実行が失敗する
- Modal の Proxy Auth トークン
- Dict（`preserve-model-progress`。モデル保存 GUI の進捗表示用、初回に自動作成）

また、デプロイ URL に**ワークスペース名が入る**（`https://<workspace>--<app>.modal.run`）。アカウントを変えてデプロイし直したら、Cloudflare Access を使っている場合は Worker の `MODAL_ORIGINS` / `MODAL_KEY` / `MODAL_SECRET` も入れ直す（[cloudflare-access.md](cloudflare-access.md)）。

## 補足

**profile と Environment は別物**

Modal の "Environment" は 1 アカウントの中を dev / prod に分ける機能で、別アカウントの使い分けには使えない。ここで扱っているのは profile（アカウントごとの資格情報、`~/.modal.toml`）の方。

**どこに何が保存されるか**

| 場所 | 中身 | commit されるか |
| --- | --- | --- |
| `~/.modal.toml` | 各アカウントのトークンと、Modal CLI 全体の既定 | されない（ホーム配下） |
| `.modal-profile` | このフォルダで使うアカウント名 1 行 | されない（gitignore 済み） |

**優先順位**

CLI も管理画面も同じ順で決まる。

1. 環境変数 `MODAL_PROFILE`（`./scripts/modal.sh --profile` はこれを使う）
2. `.modal-profile`（`./scripts/modal.sh use` で書かれる）
3. `~/.modal.toml` の既定（`uv run modal profile activate` で変わる）

**CI の場合**

プロファイルを作らず、トークンを環境変数で直接渡すのが簡単。

```bash
MODAL_TOKEN_ID=ak-xxxx MODAL_TOKEN_SECRET=as-xxxx uv run modal deploy comfyapp.py
```
