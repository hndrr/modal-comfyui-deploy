# 上流と split のツール対応表

artokun/comfyui-mcp 0.52.206 の全ツール（41件）とそのアクションを示します。使い方は[チャットから ComfyUI を使う](split-mcp.md)を参照してください。

「上流を使用」は split の CPU で元の npm パッケージを呼びます。「split で代替」は右欄に記載した範囲の機能を提供します。「未提供」はこの HTTPS 接続では呼べません。クライアントは HTTPS 接続に統一しています。CPU 内で呼ぶ上流パッケージは残します。

| 上流ツール | アクション | HTTPS での扱い | 使い方・理由 |
|---|---|---|---|
| `apply_manifest` | 引数 action なし | 未提供 | 複数依存の一括変更は候補とモデル単位の確認を迂回する。 |
| `apps` | `list`、`get`、`run`、`run_status`、`import` | 未提供 | 上流アプリ保管庫・実行台帳は共有していない。 |
| `batch` | `submit`、`status`、`output`、`wait` | 未提供 | 永続バッチ台帳は未実装。個々のジョブは ID で追跡できる。 |
| `bisect` | `start`、`good`、`bad`、`reset`、`status` | 未提供 | 再起動を伴うノード二分探索のセッション管理は未対応。 |
| `calculate` | 引数 action なし | 上流を使用 | 解像度・シードなどの式を計算する。 |
| `call_tool` | 引数 action なし | 未提供 | 汎用ディスパッチによる非公開操作の迂回を許可しない。 |
| `clear_vram` | 引数 action なし | 未提供 | 全ジョブに影響する。GPU はジョブ終了後に縮退。 |
| `comfy_cli` | `status` | split で代替 | get_split_status で active と candidate を確認。 |
| `comfy_cli` | `jobs_list`、`jobs_status`、`jobs_wait`、`jobs_watch`、`jobs_cancel` | split で代替 | queue、get_job_progress、cancel_job。待機はクライアントが同じ ID で再照会。 |
| `comfy_cli` | `search_nodes`、`workflow_validate`、`workflow_run` | split で代替 | list_nodes、create_workflow(validate)、enqueue_workflow。 |
| `comfy_cli` | `transfer_upload`、`transfer_download` | split で代替 | upload_media と get_image／get_audio／get_video。 |
| `comfy_cli` | `models_list_folders`、`models_list_folder`、`models_search`、`models_show`、`models_download`、`models_remove` | split で代替 | list_models、inspect_model、add_model、remove_model。追加は Hugging Face の単一ファイル。 |
| `comfy_cli` | `server_start`、`server_stop`、`server_restart`、`skills_list`、`skills_show`、`skills_validate`、`skills_install`、`skills_status`、`skills_uninstall` | 未提供 | サーバーは Modal が管理。クライアントのローカル設定やスキルは変更しない。 |
| `create_workflow` | `create`、`modify`、`validate`、`node_info` | 上流を使用 | 同名ツールを使用。 |
| `describe_tool` | 引数 action なし | split で代替 | MCP の tools/list が実際に公開する入力スキーマを返す。 |
| `download_model` | `download`、`status` | split で代替 | add_model と get_admin_operation。任意 URL は受け付けない。 |
| `download_model` | `cancel`、`search`、`search_civitai`、`search_creators`、`download_civitai`、`resolve_missing` | 未提供 | ダウンロード中止・カタログ横断検索・不足モデルの自動選定は未対応。 |
| `enqueue_workflow` | `enqueue`、`rerun` | split で代替 | enqueue_workflow で一意の ID を指定。再生成は get_job_workflow → 編集 → 新しい ID で投入。 |
| `enqueue_workflow` | `run_url`、`template_schema`、`run_template` | 未提供 | 外部 URL や上流テンプレートの直接実行経路は未接続。保存グラフは投入できる。 |
| `generate_image` | `image`、`audio`、`video`、`3d`、`controlnet`、`ip_adapter`、`regenerate`、`upscale`、`remove_background` | split で代替 | 該当ノード・モデルを持つグラフを明示的に enqueue_workflow。上流のワンクリック自動構築や有料プロバイダーの選択は含まない。 |
| `get_defaults` | `get`、`set`、`get_ui`、`set_ui` | 未提供 | 上流のローカル設定／UI の個人設定はクライアント間で暗黙共有しない。 |
| `get_history` | `list`、`diagnose` | split で代替 | 参照用 CPU が保存済み台帳・GPU 完了記録から出力・エラー・時間を返す。ライブ状態ではない。 |
| `get_history` | `stats`、`suggest` | 未提供 | 上流の一時的なプロセス内の生成履歴 DB は split の永続ジョブ台帳とは異なる。 |
| `get_image` | `get` | split で代替 | 配信用 CPU が共有 output の画像を返す。8 MiB超は認証付きリンク。ComfyUI は起動しない。 |
| `get_image` | `list_outputs`、`list_assets`、`asset_metadata` | split で代替 | list_outputs は prompt_id 単位、list_inputs は入力置き場、inspect_media は寸法・音声など。上流の asset ID は使わない。 |
| `get_image` | `view`、`convert`、`analyze_color` | 未提供 | 任意パスの閲覧・形式変換・色解析は公開していない。取得は get_image。 |
| `get_system_stats` | `stats`、`health` | split で代替 | get_split_status は実際の GPU 台数と処理状態を返す。ハードウェア全項目を返すものではない。 |
| `get_system_stats` | `logs` | 未提供 | CPU/GPU の運用ログは Modal 側。ジョブの失敗は get_history(diagnose)。 |
| `get_workflow` | `list`、`get`、`analyze`、`query`、`slice`、`strip` | 上流を使用 | 保存ライブラリ内の相対名に限定。一覧・元 JSON・保存済み API グラフの取得は参照用 CPU で代替。UI→API 変換と解析には上流と現在のノード定義を使う。 |
| `get_workflow` | `from_image`、`prompt_director` | 未提供 | 画像内のグラフ抽出・プロンプト指示の抽出は未対応。既知の生成 ID は get_job_workflow。 |
| `install_comfyui` | `environment` | split で代替 | get_split_status で実際の環境を照会。 |
| `install_comfyui` | `install`、`update`、`update_all`、`panel`、`self_update`、`configure_manager` | 未提供 | 基盤イメージ・MCP・Manager の設定はデプロイで変更する。 |
| `install_custom_node` | `install`、`update` | split で代替 | change_node を候補コピーだけに実行。 |
| `install_custom_node` | `reinstall`、`fix`、`uninstall`、`enable`、`disable`、`list`、`sync_deps` | 未提供 | パック一覧・削除・有効化・依存同期はこの版では Manager GUI。list_nodes はパックではなくノードクラス。 |
| `kitchen` | `status`、`assess`、`apply` | 未提供 | 実行中のプロセスに最適化を適用する操作は split の宣言的なイメージと一致しない。 |
| `list_api_nodes` | `list`、`schema`、`generate` | 未提供 | 上流の個別ツールは未提供。対応ノードは list_nodes・create_workflow で照会し、enqueue_workflow から本人の承認後に実行。 |
| `list_local_models` | `list`、`embeddings`、`remove` | split で代替 | list_models は embeddings も対象。削除は inspect_model → remove_model。 |
| `list_local_models` | `list_paths`、`add_path`、`remove_path` | 未提供 | ローダーの検索パスはデプロイ設定で管理する。 |
| `list_packs` | `list`、`read_workflow`、`read_manifest`、`list_templates`、`check_runtime`、`extract_deps`、`install_deps`、`skill_list`、`skill_read`、`generate_skill` | 未提供 | 上流のローカルパック保管庫を共有 Volume としては構成していない。 |
| `list_tools` | 引数 action なし | split で代替 | MCP の tools/list が実際に公開する入力スキーマを返す。 |
| `model_metadata` | `read` | split で代替 | inspect_model は共有ファイルのサイズと変更検出トークン。埋め込みメタデータの解析は未対応。 |
| `model_metadata` | `propose`、`fetch_civitai` | 未提供 | 外部情報の取得・タグ提案は未対応。 |
| `node_pack` | `scaffold`、`verify`、`publish`、`list_files`、`read`、`search`、`write`、`patch`、`git` | 未提供 | ソースの編集・公開は開発用リポジトリで行う。 |
| `node_snapshot` | `save`、`restore` | split で代替 | prepare_environment、apply_environment、discard_environment は候補コピーの作成・検証・破棄。過去バージョン全般の復元ではない。 |
| `node_snapshot` | `list` | 未提供 | 上流スナップショット一覧は split の環境台帳ではない。 |
| `queue` | `list`、`status` | 上流を使用 | 同名ツールを使用。 |
| `queue` | `get_workflow`、`cancel`、`cancel_queued` | split で代替 | get_job_workflow、cancel_job で1件を指定。 |
| `queue` | `move`、`edit`、`clear` | 未提供 | 予約済みのグラフ・順番は変更しない。全消去は他ジョブに影響する。 |
| `report_issue` | 引数 action なし | 未提供 | 外部への課題投稿を制作接続には含めない。 |
| `restart_comfyui` | `restart`、`start`、`stop` | 未提供 | コンテナ起動は Modal が管理。環境切替は apply_environment。 |
| `runpod` | `create`、`start`、`stop`、`status`、`list`、`connect`、`use_local`、`deploy_link` | 未提供 | 別プロバイダー。Modal の管理と認証を共有しない。 |
| `runpod_watch` | `watch`、`unwatch`、`troubleshoot` | 未提供 | 別プロバイダーの監視。 |
| `save_workflow` | `save` | 上流を使用 | 新規名のみ。既存ワークフローは上書きしない。 |
| `save_workflow` | `lock`、`verify_lock` | 未提供 | 上流ロックファイルの永続化・依存解決をまだ扱わない。 |
| `search_custom_nodes` | `search`、`details` | 上流を使用 | 公開レジストリでパック ID とリリースを調べる。インストールは別操作。 |
| `train_doctor` | `doctor`、`bootstrap`、`build_image` | 未提供 | 学習環境の構築・診断はデプロイの対象外。 |
| `train_prepare_dataset` | `prepare`、`list`、`detail`、`update`、`delete`、`file`、`caption_image`、`caption_dataset` | 未提供 | 学習データ領域は生成用 input と別。学習基盤は未接続。 |
| `train_start` | `start`、`status`、`cancel`、`delete`、`list_flows`、`job_config`、`preview_config` | 未提供 | 学習ジョブの実行先は生成ジョブ台帳と別。 |
| `upload_image` | `image`、`video`、`audio`、`stage` | split で代替 | upload_media と reuse_output。後者は以前のジョブの結果を素材にする。 |
| `upload_image` | `output` | 未提供 | 生成履歴に属さないファイルを output に書き込まない。 |
| `visualize_workflow` | `render`、`render_hierarchical`、`mermaid`、`to_dsl`、`from_dsl` | 上流を使用 | 引数のグラフまたは DSL を使う。 |
| `workspace` | `get`、`set_default`、`list` | 未提供 | 転送先は認証済みの固定 split。接続先はツールで変更しない。 |
