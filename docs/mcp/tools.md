# リモート MCP のツール一覧

このページは、**Split app（`splitapp.py`）の MCP を開発する人・ツールを直接呼ぶ人向け**のリファレンスです。チャットでの使い方を知りたい場合は[利用ガイド](usage.md)を参照してください。

ChatGPT Web・Claude Code・Codex が同じ HTTPS `/mcp` で使う **33ツール**と、音声・動画・3Dビューアー専用の内部ツール3件をまとめています。接続方法と依頼例は[利用ガイド](usage.md)、通信先と起動の分岐は[経路図](../design/split-mcp-routing.md)、artokun 版の全機能との比較は[上流との対応表](upstream.md)を参照してください。

この一覧はリポジトリの実装を説明します。接続先で実際に使える名前・入力項目は MCP の `tools/list` が返すスキーマで確認してください。外部送信の承認は、Worker と split の対応版・設定を両方適用して使う機能です。OAuth 接続が成功しただけでは有効化を確認できません。[管理手順](setup.md)に適用条件があります。

## 実行先の読み方

以下は配信用 CPU を設定した構成です。**「配信用 CPU」と「参照用 CPU」は同じコンテナ**で、出力の配信に加えて保存ファイルの情報も返します。

| 表の表記 | 実際に動くもの | GPU |
| --- | --- | --- |
| Worker のみ | 認証・MCP 初期化・ツール一覧・`ping`・プレイヤー HTML | 起動しない。Modal CPU も起動しない |
| 配信用 CPU | `mediaapp.py`。Volume の保存データを読む。ComfyUI・artokun MCP は起動しない | 起動しない |
| ComfyUI CPU | `splitapp.py` の CPU。現在のノード情報・ジョブ管理・素材保存・上流ツールを扱う | その操作だけでは起動しない |
| モデル取得 CPU | ComfyUI CPU が依頼するダウンロード用の別関数 | 起動しない |
| CPU実行ワーカー | CPU対応済みAPI・軽い素材入出力、または明示的なCPU試行 | 起動しない |
| GPU | ローカル生成・CPU対応未登録の処理、または `apply_environment` の環境検証 | 必要な処理の実行時に起動する |

「起動しない」はその操作が新しく起こさないという意味です。別の生成が進行中なら GPU は動いています。取得が終われば配信用 CPU はアイドル停止し、通常の split モードでは ComfyUI CPU と GPU もほかに仕事がなければ0台に戻ります。状態照会を続けると ComfyUI CPU は停止しません。

配信用 CPU の設定を外している場合は、保存データの取得も従来の ComfyUI CPU 経路になります。設定済みの配信先が失敗した場合は、読み取りエラーを返し、ComfyUI を自動で起こし直しません。

## ワークフローを選ぶ・調べる・編集する（6ツール）

| ツール | 主な引数・アクション | できること・返るもの | 実行先・保存への影響 |
| --- | --- | --- | --- |
| `get_workflow` | `action`。取得・解析には `filename` | ライブラリの一覧、JSON 取得、構成解析、ノード検索、部分抽出。下のアクション表を参照 | 一覧・元JSON・保存済みAPI形式は配信用 CPU。変換・解析は ComfyUI CPU。保存しない |
| `create_workflow` | `action: node_info / create / modify / validate`。`node_type`、`workflow`、`operations` など | ノードの入力仕様、実行用グラフの作成・編集・検証結果 | ComfyUI CPU → artokun。グラフを返すだけで保存・生成しない |
| `save_workflow` | `filename`、`workflow` | 新しい名前でライブラリに保存。可能なら画面で編集できる形式に変換 | ComfyUI CPU → artokun。既存名は上書きしない |
| `visualize_workflow` | `action: render / render_hierarchical / mermaid / to_dsl / from_dsl`。`workflow`、`mermaid`、`dsl` | グラフの Mermaid 図・DSL、またはその解析結果 | ComfyUI CPU → artokun。保存・生成しない |
| `calculate` | `spec`、任意で `variables`、`seed` | 解像度などの数式・複数式の計算結果 | 現在は ComfyUI CPU → artokun。Worker 内の計算ではない |
| `get_job_workflow` | `prompt_id` | 過去のジョブに実際に投入した API 形式の `workflow` と保存時点の状態 | 配信用 CPU。認証用の `extra_data` は返さない |

### get_workflow の使い分け

保存名はライブラリ内の相対名です。`動画/短いクリップ.json` のような日本語・サブフォルダに対応し、サーバーの絶対パスは指定しません。

| `action` | 追加引数 | 結果と実行先 |
| --- | --- | --- |
| `list` | なし | 保存名の一覧。配信用 CPU |
| `get` | `filename`、任意で `format: api / ui` | 既定は生成依頼に渡す API 形式。保存済みAPI形式なら配信用 CPU、画面用JSONからの変換が必要なら ComfyUI CPU。`ui` は保存された元JSONを配信用 CPUから返す |
| `analyze` | `filename`、任意で `view`・`section` | 構成・セクションを調べる。`view: detail` には `section` が必要。ComfyUI CPU |
| `query` | `filename`、任意で `types`・`title`・`where`・`ids`・`upstream_of`・`downstream_of` など | クラス・タイトル・接続関係でノードを絞る。ComfyUI CPU |
| `strip` | `filename`、任意で `format: api / raw` | 編集用情報を整理してグラフを返す。ComfyUI CPU |
| `slice` | `filename`、`groups` | 指定したグループを取り出す。ComfyUI CPU |

**画面用JSONとAPI形式の違い**：画面用JSONはノードの位置や線、ウィジェットの値を記録します。API形式は「どのノードに何という名前で値を渡すか」を記録します。変換には現在のノードの入力仕様が必要なので、ComfyUIに問い合わせます。変換時の警告も返します。`format: ui` は「API形式を画面用に変換する」という指定ではなく、保存されたJSONをそのまま読む指定です。

```json
{"action":"list"}
```

一覧の後、元の保存内容だけを見るなら次の引数を `get_workflow` に渡します。

```json
{"action":"get","filename":"動画/短いクリップ.json","format":"ui"}
```

生成依頼に渡す形式が必要なら `format` を `api` にするか省略します。取得だけでは生成されません。

## 入力素材を用意する（4ツール）

| ツール | 主な引数 | できること・返るもの | 実行先・保存への影響 |
| --- | --- | --- | --- |
| `upload_media` | ChatGPT は `file`。CLI は `upload_id` と分割データ | 画像・音源・動画を入力に保存し、ローダーに渡す `filename` を返す。最大256 MiB | ComfyUI CPU。全体の検証成功時だけ公開し、既存ファイルは上書きしない |
| `list_inputs` | 任意で `kind`・`query`・`offset`・`limit` | 入力フォルダ直下の素材名・種類・サイズを返す。未完了の転送は含まない | ComfyUI CPU。読み取りのみ |
| `inspect_media` | 入力は `filename`、出力は `prompt_id` と `index` | 画像の寸法、音声・動画の再生時間やストリーム情報。GLB/SPZはファイル名・種類・形式・サイズのみ（ffprobe不使用） | ComfyUI CPU。出力の検査は完了済みジョブが対象 |
| `reuse_output` | `prompt_id`、`index` | 対応する画像・音声・動画の完了済み出力1件を入力へコピーし、ローダー用 `filename` を返す。最大256 MiB。3Dは非対応 | ComfyUI CPU。再生成・クライアントへのダウンロードは不要 |

ChatGPT 添付はクライアントが `file` を渡します。CLI では[ファイル転送ヘルパー](usage.md#手元のファイルを送る)を使います。会話に大きな Base64 やローカルパスを貼る必要はありません。

CLI の転送は、`upload_id + filename` で開始・再開位置を確認し、`upload_id + offset + data` で1 MiB以内の部分を送り、`upload_id + total_bytes` で確定します。同じ転送の再試行では `upload_id` を維持します。対応する入力形式は[利用ガイド](usage.md#手元のファイルを送る)を参照してください。

GLB/SPZのアップロード・入力への再利用は追加していません。`list_inputs` も従来どおり画像・音声・動画だけを対象にします。

## 生成する・状況を見る・止める（5ツール）

| ツール | 主な引数・アクション | できること・返るもの | 実行先・保存への影響 |
| --- | --- | --- | --- |
| `enqueue_workflow` | `request_id`、API形式の `workflow`、任意の `gpu`（`auto`＝料金優先、`auto-speed`＝速度優先、または `L4`・`L40S`・`RTX-PRO-6000`、明示試行用の `CPU`） | 選択結果の `gpu` と優先設定を含む理由の `gpu_reason` を返す。`prompt_id`、または外部送信の `approval_required` と本人用確認リンク | ComfyUI CPUで受付。対応済みAPI・軽い入出力だけならCPU、それ以外はGPUを起動。明示的な `CPU` 指定ならCPU対応未登録でもCPUで試行する。CPU指定でもMCPの拒否・承認条件は維持。外部送信は対応版の承認機能を適用して利用する |
| `queue` | `action: list / status`。個別確認は `prompt_id` | ジョブ一覧、または指定したジョブの現在の状態 | ComfyUI CPU。新規ジョブを作らない |
| `get_job_progress` | `prompt_id` | 現在のノード、報告されたステップ、CPU/GPUと選択理由、エラー、各種時間。未報告の値は `null` | ComfyUI CPU。GPUを新規起動しない |
| `get_history` | `action: list / diagnose`、任意で `prompt_id`・`limit` | 保存済みの状態・出力・エラー・実行時間。`live: false`。IDなしの `diagnose` は直近の対象件数内の失敗を優先 | 配信用 CPU。ライブ進捗ではない |
| `cancel_job` | `prompt_id` | その1件だけを取消。待機中は `cancelled`、実行中は `cancel_requested`。不明なIDはエラー | ComfyUI CPU。実行中の対象に停止を要求。完了済みの出力は維持 |

`cancel_requested` は「停止要求を受け付けた」という意味で、停止完了ではありません。同じ `prompt_id` を `queue` または `get_job_progress` で確認します。キュー全消去や全GPUへの割り込みツールはありません。

`enqueue_workflow` の受付で通信が切れた場合や、本人が外部送信を承認した後の結果を取得する場合は、同じ `request_id`、同じ `workflow`、同じGPU指定で再送します。省略と `auto` は同じ料金優先の指定です。料金優先と速度優先を変更する場合も、新しい `request_id` を使います。承認待ちの段階ではジョブも GPU 呼び出しもありません。承認は1つのグラフと素材にだけ有効で、内容を変更した場合は新しい `request_id` を使います。[利用者が行う承認操作](usage.md#外部サービスを使うときの承認)も参照してください。

| 時間の項目 | 意味 |
| --- | --- |
| `execution_seconds` | ComfyUI の実行記録による時間 |
| `elapsed_seconds` | 待機を含めた、ジョブ受付からの経過時間 |
| `worker_seconds` | 準備・出力保存を含めた CPU/GPU 実行ワーカーの処理時間 |

## 出力を受け取る（5ツール）

| ツール | 主な引数 | できること・返るもの | 実行先 |
| --- | --- | --- | --- |
| `list_outputs` | `prompt_id`、任意で `offset`・`limit` | 通常保存された出力の `filename`・`subfolder`・種類・検査／対応素材の再利用用 `index`。GLB/SPZは `kind: "3d"`。一時出力と `.split-temp` は除外 | 配信用 CPU |
| `get_image` | `filename`、任意で `subfolder` | 8 MiB以下のPNG・JPEG・WebPをMCP認証でチャット内表示。サイトへの追加ログインは不要。上限超過・表示不可・保存時だけ認証付き閲覧リンクを案内 | 配信用 CPU。ビューアHTML自体はWorker |
| `get_audio` | `prompt_id`、任意で `index`・`byte_offset`・`chunk_bytes` | MP3・WAV・FLAC・Ogg・OpusをMCP認証でチャット内再生。サイトへの追加ログインは不要。上限超過・再生不可・保存時だけ認証付きリンクを案内。明示的な部分取得では音声データ | 配信用 CPU。プレイヤーHTML自体はWorker |
| `get_video` | `prompt_id`、任意で `index`・`byte_offset`・`chunk_bytes` | 最大128 MiBのMP4をMCP認証の非公開分割取得で再生。サイトへの追加ログインは不要。上限超過・Range非対応時はブラウザ認証付き配信を使う。バイナリ取得は `byte_offset: 0` から | 配信用 CPU。プレイヤーHTML自体はWorker |
| `get_3d` | `filename`、任意で `subfolder` | 通常保存されたGLBメッシュ／SPZ Gaussian splatのメタデータ（形式・MIME・サイズ・閲覧URL）と3Dビューアー。チャット内表示は最大128 MiB | 配信用 CPU。ビューアーHTML自体はWorker。生成しない |

`get_3d` には `list_outputs` のファイル名とサブフォルダを渡します。一時出力・`.split-temp` は対象外です。ビューアーはMCP認証で分割取得し、Accessのブラウザ用URLを直接fetchしません。ホストのCSP・WebGL制約などで表示できない場合や128 MiBを超える場合は、ブラウザ認証付きの閲覧・保存リンクを案内します。ツールの成功だけで表示成功とは判断しません。

`get_audio` の `index` は音声だけ、`get_video` の `index` はMP4だけを数えた0始まりの番号です。`list_outputs` の `index` は全種類を通した番号で、`reuse_output` と `inspect_media` に渡します。複数種類の出力があるときは混同しないでください。

CLIで音声・動画の全バイトを取る場合は `byte_offset: 0` から開始し、返った `next_offset` を次の `byte_offset` に指定して、`null` まで続けます。`chunk_bytes` は最大4 MiBです。ダウンロード・再生失敗時は同じ出力の取得を再試行し、生成をやり直しません。ブラウザ用リンクにはログインが必要で、公開URLではありません。

`prompt_id` による取得はジョブ記録の保持期間に従います。記録が期限切れでも通常保存された原本が残っていれば、ファイル名を指定した `get_image`・`get_3d` やブラウザの `/view` では取得できます。

プレイヤーを表示できず再生リンクも見えない場合は、同じ `prompt_id` の `list_outputs` からファイル名を取得し、`https://<SPLIT_PUBLIC_HOST>/view?type=output&filename=…&subfolder=…` を案内します。`filename` と `subfolder` はそれぞれ URL エンコードします。ブラウザでログインすると再生・保存できます。動画から音声だけを抽出する機能はありません。

GLB/SPZも、ブラウザからの `/view` への通常のページ遷移では認証付きビューアーを返し、`/api/view` は原本バイトを返します。重みの取得や推論環境の追加設定は不要です。

## モデルを探す・追加する・退避する（5ツール）

| ツール | 主な引数 | できること・返るもの | 実行先・保存への影響 |
| --- | --- | --- | --- |
| `list_models` | 任意で `source: storage / runtime`・`category`・`query`・`offset`・`limit` | カテゴリ、またはその中の一覧。既定の `storage` はファイル名・サイズ・更新日時。`runtime` はComfyUIの選択肢 | `storage` は配信用 CPU、`runtime` は ComfyUI CPU。重みをロードして動作試験はしない |
| `inspect_model` | `path`、任意で `request_id` | サイズと全体のSHA-256による `version`。大きいファイルは追跡用 `request_id` | ComfyUI CPU。内容を読み出してハッシュ計算するが変更しない |
| `add_model` | `repo`・`file`・`category`・`request_id`、任意で `revision`・`sha256` | Hugging Faceの1ファイルを追加。結果は `get_admin_operation` で確認 | ComfyUI CPU → モデル取得 CPU。既存ファイルは上書きしない |
| `remove_model` | `path`・`version`・`request_id` | 確認した1ファイルをローダーの対象から退避。再帰削除ではない | ComfyUI CPU。ファイル変更を検知したら拒否。実行中ジョブがある間は退避しない |
| `restore_model` | `removal_id`・`request_id` | 退避済みの1ファイルを復元。`removal_id` は削除時の `request_id`、`request_id` は新しい復元用ID | ComfyUI CPU。新しく同名ファイルがある場合は上書きしない |

**保存一覧と選択肢の違い**：`storage` は「Volumeに何があるか」、`runtime` は「ComfyUIのモデルを選ぶ欄に何が表示されるか」です。保存されている全ファイルをComfyUIが使えるとは限りません。どちらも生成はせず、GPUを起動しません。

`list_models` でLoRAの保存ファイルを見る例：

```json
{"source":"storage","category":"loras","query":"portrait","limit":20}
```

ComfyUIのLoRA選択肢を確認する例：

```json
{"source":"runtime","category":"loras","query":"portrait","limit":20}
```

モデルを追加するカテゴリは `list_models` の結果から選び、追加形式や認証の条件は[管理手順](setup.md#モデルの取得元と上流版の更新)を参照してください。

## ノード・実行環境を管理する（8ツール）

| ツール | 主な引数 | できること・返るもの | 実行先・保存への影響 |
| --- | --- | --- | --- |
| `list_nodes` | 任意で `query`・`offset`・`limit` | 現在の環境で登録されているノードクラス名・表示名・カテゴリ | ComfyUI CPU。詳しい入力仕様は `create_workflow(node_info)` |
| `search_custom_nodes` | `action: search / details`。検索は `query`、詳細は `id` | 公開Comfy Registryのパック候補・リリース情報 | ComfyUI CPU → artokun → Registry。インストールしない |
| `get_split_status` | なし | 稼働版 `active`、候補 `candidate`、GPU台数、処理・Managerキューの状態 | ComfyUI CPU。GPUは起こさないが、この照会自身がCPUを起こす |
| `prepare_environment` | `active`・`request_id` | 指定した稼働版の検証用コピーを非同期で準備。`get_admin_operation` の完了結果から `candidate` を取得 | ComfyUI CPU。準備中・候補がある間は通常の生成受付を止める |
| `change_node` | `candidate`・`action: install / update`・`node`・`request_id`。`install` は任意で `version` | 指定したRegistryパックを候補環境にインストール・更新 | ComfyUI CPU。候補のコード・依存を変更。稼働版はまだ切り替えない |
| `apply_environment` | `candidate`・`request_id` | 候補をCPU/GPUで検証し、成功時だけ稼働版に切り替える | **ComfyUI CPUとGPU**。画像生成を依頼しなくても検証でGPUを起こす |
| `discard_environment` | `candidate`・`request_id` | 指定した非稼働の候補を破棄 | ComfyUI CPU。稼働版は維持 |
| `get_admin_operation` | `request_id` | 指定した管理操作の進行状態・結果・エラー | ComfyUI CPU。対象の操作を新たに作らない |

管理操作は `running` を完了と見なさず、同じ `request_id` で `completed / failed / unknown` を確認します。`unknown` は結果未確認なので、対象の状態を調べてから次の操作を決めます。`get_split_status` の定期呼び出しではCPUが止まらないため、0台になったか調べる場合はModalの管理画面・管理APIを使います。

## IDとページ送り

| 項目 | 何に使うか | 再試行時の扱い |
| --- | --- | --- |
| `request_id` | 生成受付、または管理操作1件の識別キー | 同じ内容の再試行は同じキー。別の生成・別の内容は新しいキー。同じキーで内容を変えると競合 |
| `prompt_id` | 受付済みの生成ジョブを追跡 | 状態・取消・履歴・出力の取得に同じIDを使う |
| `upload_id` | 素材1ファイルの分割転送 | 中断・部分再送・確定まで同じIDを使う |
| `candidate` | ノード変更の検証用環境 | 返された正確な版を指定。古い版なら状態を再取得 |
| `version` | 退避前に確認したモデルのSHA-256 | `inspect_model` の値をそのまま `remove_model` に渡す |

一覧で `next_offset` が返った場合、その値を次の `offset` に指定します。`null` なら末尾です。`get_workflow(list)` は保存名の一覧、`get_history` は `limit` による直近件数の指定で、`offset` は受け付けません。

## プレイヤー・ビューアー専用の内部ツール（3件）

| ツール | 引数 | 用途・実行先 |
| --- | --- | --- |
| `read_audio_part` | `prompt_id`・`byte_offset`、任意で `index` | 音声プレイヤーが検証付き128 KiB分割データを読む。配信用 CPU。エージェント向けではなくアプリ向けとして登録 |
| `read_video_part` | `prompt_id`・`byte_offset`、任意で `index` | 動画プレイヤーが検証付き512 KiB分割データを読む。配信用 CPU。アプリ専用で、本体はモデル向け本文に含めない |
| `read_3d_part` | `filename`・`byte_offset`、任意で `subfolder` | 3Dビューアーが最大512 KiBずつ取得。配信用 CPU。アプリ専用で、バイト列のBase64は `_meta["split/3dPart"]` のみ。本文には含めず、`next_offset` で継続 |

プレイヤー・ビューアーのHTML取得はMCPリソースで、ツール数には含めません。HTMLだけならWorkerで応答し、データの要求時に配信用CPUを使います。クライアントの表示機能によってプレイヤーが使えない場合も、`get_audio`・`get_video` のリンクまたは部分取得、`get_3d` のブラウザ用リンクを利用できます。
