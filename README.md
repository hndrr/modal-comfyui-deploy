# modal-comfyui

Modal 上で ComfyUI を動かし、Hugging Face のモデルや生成素材を Modal Volume で管理するリポジトリです。

![ComfyUI](docs/assets/2025-09-28-21-11-34.png)

## アプリと使い方

| やりたいこと | 入口 | ガイド |
| --- | --- | --- |
| GPU 上で ComfyUI を動かす | `comfyapp.py` | [通常構成](docs/guides/comfyui-modal.md) |
| UI を CPU、生成をオンデマンド GPU で動かす | `splitapp.py` | [CPU/GPU 分離構成](docs/guides/comfyui-split.md)（Experimental・開発検証中） |
| チャットから split を操作する | Cloudflare Worker の `/mcp` | [ChatGPT Web・Claude Code・Codex の接続と操作](docs/mcp/usage.md) |
| Hugging Face のモデルを保存する | `preserve_model.py` / `preserve_model_gui.py` | [モデル保存の CLI・Web GUI・ローカル GUI](docs/guides/preserve-model.md) |
| モデル・入力・出力を管理する | split の `/assets` または `web/` | [資産管理画面](docs/guides/asset-manager.md) |
| Volume をコピーする・ファイルを移動する | `scripts/rename_volume.py` / `scripts/move_volume_file.py` | [Volume 操作](docs/guides/volume-tools.md) |
| 公開アプリにログイン認証を付ける | `worker/` | [Cloudflare Access の設定](docs/operations/cloudflare-access.md) |

全ドキュメントの入口は [docs/README.md](docs/README.md) です。運用手順、MCP のツール一覧、設計・検証記録もそこからたどれます。

## セットアップと起動

```bash
git clone https://github.com/hndrr/modal-comfyui-deploy.git
cd modal-comfyui-deploy
uv sync
cp .env.example .env
```

Modal CLI でログインし、`.env` を必要に応じて編集してください。Hugging Face のモデル保存には、Modal Secret `huggingface-secret` が必要です。複数の Modal アカウントを使う場合は、先に[このチェックアウトのプロファイルを固定](docs/operations/modal-profiles.md)してください。

通常構成の開発・デプロイ:

```bash
./scripts/modal.sh serve comfyapp.py
./scripts/modal.sh deploy comfyapp.py
```

CPU/GPU 分離構成は、別の Modal App としてデプロイします。

```bash
./scripts/modal.sh deploy splitapp.py
```

通常構成は `.env` を自動で読み込み、設定済みのシェル環境変数を優先します。GPU・Volume・追加ノードの設定は、それぞれの構成のガイドを参照してください。

## ディレクトリ構成

```text
.
├── comfyapp.py              # 通常構成の Modal アプリ
├── splitapp.py              # CPU/GPU 分離構成の Modal アプリ
├── mediaapp.py              # split の保存済みメディア配信
├── split_guis.py            # split の /models・/assets 用 GUI
├── preserve_model.py        # モデル保存処理と Modal 上の Web GUI
├── preserve_model_gui.py    # ローカルのモデル保存 GUI
├── asset_manager.py         # Volume 操作
├── asset_rpc.py             # 資産管理画面と Python バックエンド間の RPC
├── comfy_split/             # split の実行・状態管理・ストレージ
├── web/                     # 資産管理画面（React + Hono）
├── worker/                  # Cloudflare Access の認証・中継・MCP 入口
├── mcp/                     # MCP パッケージ設定・CLI ヘルパー・対応表生成
├── extensions/              # 同梱の ComfyUI 拡張
├── scripts/                 # Volume 操作・モデル準備・検証・Modal CLI ラッパー
├── docs/                    # 使い方・運用・MCP・設計記録・説明用画像
└── tests/                   # Python の機能別テスト
```

デプロイと GUI の起動に使う Python ファイルはルートに置き、補助コマンドは `scripts/` にまとめています。コマンド例は、特記がなければリポジトリのルートで実行します。

検証方法は[テストの説明](tests/README.md)、CI の実行条件は[CI の説明](docs/operations/ci.md)を参照してください。任意拡張や追加ノードを使う場合は、[split の任意拡張](docs/guides/split-integrations.md)を参照してください。
