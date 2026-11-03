# ドキュメント

アプリの概要と最初の起動は[リポジトリの README](../README.md)を参照してください。各ページのコマンド例は、特記がなければリポジトリのルートで実行します。

`<SPLIT_PUBLIC_HOST>` などの山括弧付きの値は、各手順で説明する実環境の値に置き換えます。`HOST` はホスト名だけ、`ORIGIN` は `https://` を含む URL（パス・末尾の `/` なし）です。Modal の URL はデプロイ結果からコピーし、ワークスペース名から組み立てないでください。JSON や設定ファイルのプレースホルダーは自動では展開されません。

## 使い方 — `guides/`

| ガイド | 内容 |
| --- | --- |
| [通常構成の ComfyUI](guides/comfyui-modal.md) | 起動・デプロイ、GPU、環境変数、Volume、custom nodes |
| [CPU/GPU 分離構成](guides/comfyui-split.md) | split の起動・運用、保存先、GPU の操作（Experimental） |
| [split の任意拡張](guides/split-integrations.md) | 拡張・追加ノード・中継の選択、認証、取得・更新 |
| [モデル保存](guides/preserve-model.md) | Hugging Face からの取り込み、CLI・GUI、Modal へのデプロイ |
| [資産管理画面](guides/asset-manager.md) | split の `/assets`・`/models` とローカルの管理画面 |
| [Volume 操作](guides/volume-tools.md) | Volume のコピー、ファイル・ディレクトリの移動 |
| [既知の不具合と注意点](guides/known-issues.md) | 症状と回避策 |

## 運用・開発 — `operations/`

| 手順 | 内容 |
| --- | --- |
| [Cloudflare Access](operations/cloudflare-access.md) | Worker の設定・デプロイ、認証、Modal への中継 |
| [Modal プロファイル](operations/modal-profiles.md) | 複数アカウントを使う場合のチェックアウトごとの接続先固定 |
| [CI](operations/ci.md) | 変更箇所に応じたジョブの実行条件 |
| [テスト](../tests/README.md) | ローカルでの全体・部分実行とテストの配置 |

## MCP — `mcp/`

| ガイド | 内容 |
| --- | --- |
| [接続と操作](mcp/usage.md) | ChatGPT Web・Claude Code・Codex からワークフロー・素材・生成を扱う |
| [管理手順](mcp/setup.md) | HTTPS・OAuth、課金先の確認、メディア配信、上流版の更新 |
| [ツール一覧](mcp/tools.md) | 引数・戻り値・実行先 |
| [上流との対応表](mcp/upstream.md) | 固定版の上流ツール・アクションをどう扱うか（`node mcp/surface-doc.mjs` で生成） |

認証、保存データ、ComfyUI CPU、GPU の接続関係は[MCP の経路図](design/split-mcp-routing.md)を参照してください。

## 設計・検証記録 — `design/`

設計の理由や過去の検証を残しています。導入・更新時の手順は、上の使い方・運用ガイドを参照してください。

| 文書 | 内容 |
| --- | --- |
| [MCP の経路](design/split-mcp-routing.md) | 操作ごとの実行先と CPU・GPU を起動する条件 |
| [メディア配信](design/split-media-delivery.md) | 現在の参照用 CPU と、未実装の R2 配信案 |
| [split のストレージ](design/split-storage.md) | 保存先の役割と移行記録 |
| [split の実行環境とストレージ](design/runtime-and-storage.md) | Volume とイメージの使い分け |
| [Access と Worker](design/cloudflare-access.md) | 認証・WebSocket 中継・迂回経路の遮断 |
| [CPU Memory Snapshots](design/cpu-memory-snapshot.md) | split への組み込みと独立検証の記録 |
| [アイドル停止計画](design/modal-idle-scale-to-zero.md) | アイドル時の縮退を導入する際の計画 |
| [Sleep/Wake 構成案](design/modal-power-control.md) | 手動・自動の GPU 操作と分離構成の検討 |
| [PyTorch / CUDA の検討](design/pytorch-cu130-upgrade.md) | アップグレードの調査と検証案 |

説明用のスクリーンショットは `assets/` に置きます。実環境のログや測定結果は Git 管理外の `validation/` で扱います。
