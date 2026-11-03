# modal-comfyui

Modal 上で ComfyUI を動かし、Hugging Face のモデルや生成素材を Modal Volume で管理するリポジトリです。

![ComfyUI](docs/assets/2025-09-28-21-11-34.png)

## アプリと使い方

| やりたいこと | 入口 | ガイド |
| --- | --- | --- |
| GPU 上で ComfyUI を動かす | `comfyapp.py` | [通常構成](docs/guides/comfyui-modal.md) |
| UI を CPU、生成をオンデマンド GPU で動かす | `splitapp.py` | [CPU/GPU 分離構成](docs/guides/comfyui-split.md)（Experimental・開発検証中） |
| Split app をチャットから使う（MCP） | `splitapp.py` + Cloudflare Worker の `/mcp` | [Split app の MCP 利用ガイド](docs/mcp/usage.md) |
| Hugging Face のモデルを保存する | `preserve_model.py` / `preserve_model_gui.py` | [モデル保存の CLI・Web GUI・ローカル GUI](docs/guides/preserve-model.md) |
| モデル・入力・出力を管理する | split の `/assets` または `web/` | [資産管理画面](docs/guides/asset-manager.md) |
| Volume をコピーする・ファイルを移動する | `scripts/rename_volume.py` / `scripts/move_volume_file.py` | [Volume 操作](docs/guides/volume-tools.md) |
| 公開アプリにログイン認証を付ける | `worker/` | [Cloudflare Access の設定](docs/operations/cloudflare-access.md) |

追加ノードや拡張を使う場合は[split の任意拡張](docs/guides/split-integrations.md)、不具合の回避策は[既知の不具合と注意点](docs/guides/known-issues.md)を参照してください。

## MCP（Split app 専用）

このリポジトリの MCP は、CPU/GPU 分離構成の **Split app（`splitapp.py`）専用**です。ChatGPT Web・Claude Code・Codex から接続できます。通常構成の `comfyapp.py` は対象に含みません。

| 読者 | ガイド | 内容 |
| --- | --- | --- |
| 利用者 | [Split app の MCP 利用ガイド](docs/mcp/usage.md) | 接続、チャットでの生成依頼、素材の送信、結果の再生・保存 |
| 管理者 | [Split app の MCP 設定・運用](docs/mcp/setup.md) | HTTPS・OAuth、課金先、メディア配信、ノード・モデルの管理 |
| 開発者・ツールを直接呼ぶ人 | [MCP ツールリファレンス](docs/mcp/tools.md) | 各ツールの引数・戻り値・実行先 |
| 開発者 | [上流との対応表](docs/mcp/upstream.md) | 固定版の上流ツール・アクションをどう扱うか |

認証や CPU・GPU の起動条件など、内部の仕組みは[MCP の経路図](docs/design/split-mcp-routing.md)を参照してください。

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

各ガイドのコマンド例は、特記がなければリポジトリのルートで実行します。`<SPLIT_PUBLIC_HOST>` などの山括弧付きの値は、各手順で説明する実環境の値に置き換えます。`HOST` はホスト名だけ、`ORIGIN` は `https://` を含む URL（パス・末尾の `/` なし）です。Modal の URL はデプロイ結果からコピーし、ワークスペース名から組み立てないでください。JSON や設定ファイルのプレースホルダーは自動では展開されません。

## 運用・開発

| 手順 | 内容 |
| --- | --- |
| [Cloudflare Access](docs/operations/cloudflare-access.md) | Worker の設定・デプロイ、認証、Modal への中継 |
| [Modal プロファイル](docs/operations/modal-profiles.md) | 複数アカウントを使う場合のチェックアウトごとの接続先固定 |
| [CI](docs/operations/ci.md) | 変更箇所に応じたジョブの実行条件 |
| [テスト](tests/README.md) | ローカルでの全体・部分実行とテストの配置 |

## 設計・検証記録

設計の理由や過去の検証を残しています。導入・更新時は、上の使い方・運用ガイドを参照してください。

| 文書 | 内容 |
| --- | --- |
| [MCP の経路](docs/design/split-mcp-routing.md) | 操作ごとの実行先と CPU・GPU を起動する条件 |
| [メディア配信](docs/design/split-media-delivery.md) | 現在の参照用 CPU と、未実装の R2 配信案 |
| [split のストレージ](docs/design/split-storage.md) | 保存先の役割と当時の一度限りの移行方針 |
| [split の実行環境とストレージ](docs/design/runtime-and-storage.md) | Volume とイメージの使い分け |
| [Access と Worker](docs/design/cloudflare-access.md) | 認証・WebSocket 中継・迂回経路の遮断 |
| [CPU Memory Snapshots](docs/design/cpu-memory-snapshot.md) | split への組み込みと独立検証の記録 |
| [アイドル停止計画](docs/design/modal-idle-scale-to-zero.md) | アイドル時の縮退を導入する際の計画 |
| [Sleep/Wake 構成案](docs/design/modal-power-control.md) | 手動・自動の GPU 操作と分離構成の検討 |
| [PyTorch / CUDA の検討](docs/design/pytorch-cu130-upgrade.md) | アップグレードの調査と検証案 |

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

デプロイと GUI の起動に使う Python ファイルはルートに置き、補助コマンドは `scripts/` にまとめています。説明用のスクリーンショットは `docs/assets/`、実環境のログや測定結果は Git 管理外の `validation/` で扱います。

## ライセンス

**本リポジトリ自身のライセンスは未指定です。** 以下は依存ソフトウェアの確認結果で、本リポジトリの独自コードにライセンスを付与するものではありません。

### 主な依存ソフトウェア

現在のデプロイ定義・ロックファイルにある版について、上流のライセンス本文またはパッケージメタデータを確認した結果です。通常構成では ComfyUI の版を固定しない設定もあるため、実際に導入した版の条件を確認してください。

| 依存先 | 確認した版 | ライセンス・出典 |
| --- | --- | --- |
| ComfyUI | v0.43.0（Split app の固定コミット） | [GNU GPL v3](https://github.com/Comfy-Org/ComfyUI/blob/426f5eea52a7ece1dd599fff4cb6cd1f352ddd24/LICENSE) |
| ComfyUI frontend | 1.57.4 | [GNU GPL v3](https://github.com/Comfy-Org/ComfyUI_frontend/blob/v1.57.4/LICENSE) |
| ComfyUI-Manager | 4.2.2 | [GPL-3.0-only](https://pypi.org/project/comfyui-manager/4.2.2/) |
| comfy-cli | 1.7.3 | [GPL-3.0-only](https://pypi.org/project/comfy-cli/1.7.3/) |
| comfyui-mcp | 0.52.206 | [MIT](https://github.com/artokun/comfyui-mcp/blob/c64a8ec76f3345446f0f472152afcd95af846a37/LICENSE) |
| Modal SDK | 1.1.4 | [Apache-2.0](https://pypi.org/project/modal/1.1.4/) |
| Gradio | 5.47.2 | [Apache-2.0](https://pypi.org/project/gradio/5.47.2/) |
| Pillow | 11.3.0 | [MIT-CMU](https://pypi.org/project/pillow/11.3.0/) |
| python-dotenv | 1.2.2 | [BSD-3-Clause](https://pypi.org/project/python-dotenv/1.2.2/) |
| aiohttp | 3.12.15 | [Apache-2.0 AND MIT](https://pypi.org/project/aiohttp/3.12.15/) |

### Split app に導入するカスタムノード

`splitapp.py` の `NODE_REVISIONS` で固定したコミットのライセンスです。リンク先も確認したコミットに固定しています。

| ノードパック | ライセンス・出典 |
| --- | --- |
| ComfyUI-Crystools | [MIT](https://github.com/crystian/ComfyUI-Crystools/blob/2f18256c5b5063937106f29a8e0a7db3ae3869b7/LICENSE) |
| ComfyUI_Local_Media_Manager | [GNU GPL v3](https://github.com/Firetheft/ComfyUI_Local_Media_Manager/blob/5e74ce0cc708798ed25a77097d6059b6c796da87/LICENSE) |
| ComfyUI-Image-Browsing | [GNU GPL v3](https://github.com/hayden-cn/ComfyUI-Image-Browsing/blob/3d0b5f8233d9d6b322ed3ff9a6cb15efbcf7bed7/LICENSE) |
| rgthree-comfy | [MIT](https://github.com/rgthree/rgthree-comfy/blob/2c5342a8cb0eaecaabf61435a5f37dd594c510ba/LICENSE) |
| ComfyUI-MiniMax-H3-MotionCache-FastVAE | [MIT（ノードのコード）](https://github.com/Mozer/ComfyUI-MiniMax-H3-MotionCache-FastVAE/blob/b719329e0ecf35f0ae08d241c363ed1e56adbb95/LICENSE) |

この一覧は主な依存先をまとめたもので、推移的依存を含むすべての配布物を網羅していません。追加する任意拡張・ノード・モデル、実行イメージに含まれる CUDA・FFmpeg などには、それぞれのライセンス・利用条件が適用されます。導入するコードやモデル、イメージを再配布する場合は、対象の版の条件と必要な著作権表示・ライセンス文書を確認してください。
