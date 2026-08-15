# Modal のアカウントを 2 つ以上使う場合

Modal アカウントが 1 つなら、このページは不要。`uv run modal deploy comfyapp.py` がそのまま動く。

アカウントが 2 つあると困るのは、**どのコマンドがどっちのアカウントに行くのか分からなくなる**こと。Volume（モデルや出力）はアカウントごとに完全に別物なので、間違えると「昨日まであったモデルが消えた」ように見える。

このリポジトリは `scripts/modal.sh` で、**このフォルダで使うアカウントを 1 回決めておく**方式にしている。

## 手順

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

`Profile` 列がアカウントの呼び名。次の手順で使う。

### 2. このフォルダで使うアカウントを決める（1 回だけ）

```console
$ ./scripts/modal.sh use work
pinned: work
```

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

## つまずきやすいところ

**`uv run modal deploy comfyapp.py` と打つと、決めた内容が無視される**

`./scripts/modal.sh deploy comfyapp.py` を使う。手順 2 で決めた内容は Modal CLI 本体の機能ではないので、`scripts/modal.sh` を通さないと効かない。うっかり素で打った場合は Modal CLI の既定アカウント（`uv run modal profile current` で分かる）に行く。

**`.env` に `MODAL_PROFILE=work` と書いても効かない**

書いても無視される。デプロイ時にエラーで止まるようにしてあるので、気付かず間違ったアカウントに出ることはない。アカウントの指定は手順 2 でやる。

**管理画面のバッジが「workspace 不明」になる**

決めたアカウント名が Modal CLI に登録されていない（打ち間違いなど）か、Modal CLI にログインできていない。`./scripts/modal.sh --list` で名前を確認する。

**アカウントを変えたらモデルが空になった**

正常。Volume はアカウントごとに別物で、共有されない。以下も同様にアカウントごとに用意が必要:

- Volume（`comfy-model` / `comfy-inputs` / `comfy-outputs`）
- Secret（`huggingface-secret`）
- Modal の Proxy Auth トークン

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
