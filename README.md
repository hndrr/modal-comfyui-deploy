# modal-comfyui

Modal 上で ComfyUI を動かし、Hugging Face のモデルや生成素材を Modal Volume で永続管理するリポジトリです。

![ComfyUI](docs/assets/2025-09-28-21-11-34.png)

## 構成の選択

用途に合わせて 2 つの構成を提供しています。

| 構成 | 特徴・向いている用途 | エントリポイント | ガイド |
| --- | --- | --- | --- |
| **通常構成** | GPU 上で ComfyUI 全体を常時起動。短時間の集中作業や、既存 ComfyUI そのままの挙動・拡張性を求める場合に最適。 | `comfyapp.py` | [通常構成ガイド](docs/guides/comfyui-modal.md) |
| **CPU/GPU 分離構成 (Split)**<br>*(Experimental)* | UI・ファイル管理は CPU、生成時のみオンデマンドで GPU を起動（アイドル時は自動停止）。コストを抑えたい場合や、**MCP 連携（AI チャットからの画像生成）** を行いたい場合に最適。 | `splitapp.py` | [分離構成ガイド](docs/guides/comfyui-split.md) |

---

## クイックスタート

### 1. 環境準備

```bash
git clone https://github.com/hndrr/modal-comfyui-deploy.git
cd modal-comfyui-deploy
uv sync
cp .env.example .env
```

1. Modal CLI でログインします (`uv run modal setup`)。
2. 必要に応じて `.env` を編集します。Hugging Face のモデル保存を行う場合は、Modal Secret `huggingface-secret` を作成してください。
3. 複数アカウントを使い分ける場合は、[Modal プロファイルの固定手順](docs/operations/modal-profiles.md)を確認してください。

### 2. デプロイ

選択した構成に応じてデプロイします。

- **通常構成**:
  ```bash
  ./scripts/modal.sh serve comfyapp.py   # ローカル開発・検証
  ./scripts/modal.sh deploy comfyapp.py  # 本番デプロイ
  ```
- **CPU/GPU 分離構成 (Split app)**:
  ```bash
  ./scripts/modal.sh deploy splitapp.py
  ```

### 3. 公開とアクセス制御

安全に外部公開するために、Cloudflare Worker と Cloudflare Access を組み合わせた認証中継を推奨しています。
設定手順は [Cloudflare Access の設定](docs/operations/cloudflare-access.md) を参照してください。

> [!TIP]
> **ドキュメントのパラメータ表記について**  
> コマンド例の `<SPLIT_PUBLIC_HOST>` などの山括弧は実環境の値に置き換えます。`HOST` はホスト名のみ、`ORIGIN` はプロトコルを含む URL（例: `https://example.com`、末尾スラッシュなし）を表します。Modal の URL はデプロイ出力からコピーしてください。

---

## 主な機能とツール

### MCP（Model Context Protocol）連携
CPU/GPU 分離構成（Split app）は MCP に対応しており、ChatGPT Web、Claude Code、Codex などの AI チャットから自然言語で画像生成を指示できます。
- 利用者向け: [Split app の MCP 利用ガイド](docs/mcp/usage.md)
- 管理者向け: [MCP 設定・運用ガイド](docs/mcp/setup.md)
- ツール仕様: [MCP ツールリファレンス](docs/mcp/tools.md) / [上流ツール対応表](docs/mcp/upstream.md)

### モデルとストレージの管理
- **モデルの保存**: Hugging Face から Volume へモデルをダウンロード・永続化（[モデル保存ガイド](docs/guides/preserve-model.md)）
  - CLI: `preserve_model.py`
  - Web GUI: `preserve_model.py`
  - ローカル GUI: `preserve_model_gui.py`
- **資産管理画面**: モデル・入力・出力ファイルを Web UI から一覧・管理（[資産管理画面ガイド](docs/guides/asset-manager.md)）
- **Volume 操作ツール**: Volume の複製やファイル移動用スクリプト（[Volume 操作ガイド](docs/guides/volume-tools.md)）

---

## ドキュメント一覧

### 利用・運用ガイド
- [通常構成ガイド](docs/guides/comfyui-modal.md)
- [CPU/GPU 分離構成ガイド](docs/guides/comfyui-split.md)
- [Split app の任意拡張ノード](docs/guides/split-integrations.md)
- [既知の不具合と注意点](docs/guides/known-issues.md)
- [Cloudflare Access の設定](docs/operations/cloudflare-access.md)
- [Modal プロファイルの運用](docs/operations/modal-profiles.md)
- [CI 設定](docs/operations/ci.md)
- [テスト実行方法](tests/README.md)

### 設計・検証記録
<details>
<summary>設計の経緯やアーキテクチャの検証記録を展開</summary>

| ドキュメント | 内容 |
| --- | --- |
| [MCP の経路図](docs/design/split-mcp-routing.md) | 操作ごとの実行先と CPU・GPU を起動する条件 |
| [メディア配信](docs/design/split-media-delivery.md) | 現在の参照用 CPU と、未実装の R2 配信案 |
| [split のストレージ](docs/design/split-storage.md) | 保存先の役割と当時の一度限りの移行方針 |
| [split の実行環境とストレージ](docs/design/runtime-and-storage.md) | Volume とイメージの使い分け |
| [Access と Worker](docs/design/cloudflare-access.md) | 認証・WebSocket 中継・迂回経路の遮断 |
| [CPU Memory Snapshots](docs/design/cpu-memory-snapshot.md) | split への組み込みと独立検証の記録 |
| [アイドル停止計画](docs/design/modal-idle-scale-to-zero.md) | アイドル時の縮退を導入する際の計画 |
| [Sleep/Wake 構成案](docs/design/modal-power-control.md) | 手動・自動の GPU 操作と分離構成の検討 |
| [PyTorch / CUDA の検討](docs/design/pytorch-cu130-upgrade.md) | アップグレードの調査と検証案 |

</details>

---

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
├── docs/                    # 各種ガイド・設計資料
└── tests/                   # Python の機能別テスト
```

---

## ライセンス

**本リポジトリ自身のライセンスは未指定です。** 以下は依存ソフトウェアの確認結果で、本リポジトリの独自コードにライセンスを付与するものではありません。

<details>
<summary>主な依存ソフトウェアのライセンス</summary>

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

#### Split app に導入するカスタムノード

| ノードパック | ライセンス・出典 |
| --- | --- |
| ComfyUI-Crystools | [MIT](https://github.com/crystian/ComfyUI-Crystools/blob/2f18256c5b5063937106f29a8e0a7db3ae3869b7/LICENSE) |
| ComfyUI_Local_Media_Manager | [GNU GPL v3](https://github.com/Firetheft/ComfyUI_Local_Media_Manager/blob/5e74ce0cc708798ed25a77097d6059b6c796da87/LICENSE) |
| ComfyUI-Image-Browsing | [GNU GPL v3](https://github.com/hayden-cn/ComfyUI-Image-Browsing/blob/3d0b5f8233d9d6b322ed3ff9a6cb15efbcf7bed7/LICENSE) |
| rgthree-comfy | [MIT](https://github.com/rgthree/rgthree-comfy/blob/2c5342a8cb0eaecaabf61435a5f37dd594c510ba/LICENSE) |
| ComfyUI-MiniMax-H3-MotionCache-FastVAE | [MIT（ノードのコード）](https://github.com/Mozer/ComfyUI-MiniMax-H3-MotionCache-FastVAE/blob/b719329e0ecf35f0ae08d241c363ed1e56adbb95/LICENSE) |

</details>

この一覧は主な依存先をまとめたもので、推移的依存を含むすべての配布物を網羅していません。追加する任意拡張・ノード・モデル、実行イメージに含まれる CUDA・FFmpeg などには、それぞれのライセンス・利用条件が適用されます。導入するコードやモデル、イメージを再配布する場合は、対象の版の条件と必要な著作権表示・ライセンス文書を確認してください。
