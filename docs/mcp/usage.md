# チャットから ComfyUI を使う

ChatGPT Web、Claude Code、Codex から、Modal 上の ComfyUI に同じ HTTPS URL で接続できます。チャットで保存済みのワークフローを選び、画像・音源・動画を渡して生成し、結果を受け取れます。以下の `<SPLIT_PUBLIC_HOST>` は、管理者から案内された split の公開ホスト名に置き換えてください。ホスト名には `https://` や `/mcp` を含めません。

このページは操作の流れを説明します。個々の引数・戻り値は[32ツールの一覧](tools.md)、どのCPU・GPUを使うかは[経路図](../design/split-mcp-routing.md)、サーバー側の設定は[管理手順](setup.md)を参照してください。

## 接続する

**ChatGPT Web**：設定からカスタム MCP 接続を追加し、URL に `https://<SPLIT_PUBLIC_HOST>/mcp`、認証方式に OAuth を指定します。ブラウザで Access にログインし、チャットでこの接続を選んでください。生成や添付には書き込み操作を使えるワークスペース権限が必要です。[ChatGPT の接続条件](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt)

**Claude Code**：使いたいプロジェクトのディレクトリで登録します。

```bash
claude mcp add --transport http --scope local comfyui-remote "https://<SPLIT_PUBLIC_HOST>/mcp"
claude mcp login comfyui-remote
```

**Codex**：次のコマンドで登録してログインし、新しいセッションを開きます。

```bash
codex mcp add comfyui-remote --url "https://<SPLIT_PUBLIC_HOST>/mcp"
codex mcp login comfyui-remote
```

最初は「保存済みのワークフローを一覧にして。まだ生成しないで」と頼んでみてください。接続やツール一覧の取得だけでは Modal は起動しません。保存ファイル・モデル一覧・履歴の照会では小さな参照用 CPU だけを使います。変換・解析・素材保存には ComfyUI CPU を使い、生成を投入すると GPU が起動します。

## 操作ごとの実行先

「配信用CPU」と「参照用CPU」は同じものです。保存された出力・ワークフロー・履歴・モデル情報を読みます。ComfyUIを動かすCPUとは別なので、曲を取り直すためにComfyUIを起こす必要はありません。

```mermaid
flowchart TD
  Client["ChatGPT Web・Claude Code・Codex"] --> Edge["同じHTTPS /mcp：Access認証 → Worker"]
  Edge -->|"接続・ツール一覧・プレイヤーHTML"| Static["Workerだけで応答"]
  Edge -->|"保存済みファイル・履歴・一覧"| Read["配信用CPU → Modal Volumeを読む"]
  Edge -->|"ノード照会・変換・編集・素材保存・状態確認"| CPU["ComfyUI CPUで処理"]
  CPU -->|"生成・候補環境の検証を実行するとき"| GPU["GPUで処理 → 結果をVolumeに保存"]
```

配信用CPUは読み取りが終わるとアイドル停止します。ComfyUI CPUとGPUも、通常のsplitモードでほかに利用がなければ0台に戻ります。上流のartokun MCPは必要な操作でサーバー内から使い、クライアント側で起動する必要はありません。管理操作を含む[詳しい経路図](../design/split-mcp-routing.md)も用意しています。

## 添付を使って1件生成する

たとえば、画像を入力にするワークフローがある場合は、次のように依頼できます。`動画/短いクリップ.json` は例なので、一覧に出た名前に置き換えてください。

1. **ワークフローを選ぶ**：「保存済みの `動画/短いクリップ.json` を開いて、どの入力ノードに画像を渡すか教えて」。`get_workflow` は既定で実行用のグラフを返します。ComfyUI のエディタで開く JSON が欲しい場合は `format: "ui"` を指定します。
2. **素材を渡す**：ChatGPT なら画像をチャットに添付して「この画像を `upload_media` で保存して、返ったファイル名を LoadImage に設定して」と頼みます。音源なら LoadAudio、動画なら LoadVideo に渡します。CLI から送る方法は次節にあります。
3. **生成する**：「そのワークフローを1回だけ実行して、ジョブ ID を返して」。`enqueue_workflow` が受け付けると `prompt_id` が返ります。チャットを閉じてもジョブは続くので、この ID を控えてください。
4. **結果を受け取る**：「そのジョブの進捗を教えて。終わったら動画を返して」。画像は `list_outputs` で出力名を確認して `get_image` で取得します。動画と音声は `get_video`・`get_audio` にジョブ ID を渡せます。

生成の受付で通信が切れたときは、**同じ `request_id` と同じワークフロー**で再試行します。`request_id` は二重投入を防ぐためのキー、`prompt_id` は受け付けたジョブを追跡する ID です。

## 外部サービスを使うときの承認

「MiniMax Music 3 で曲を作って」と頼んだ場合、split 内でモデルを読み込んで生成するワークフローなら、そのままジョブ ID が返ります。Modal の通常の実行料金はかかります。**同じモデル名でも**、`ComfyCloudMiniMaxMusic3TextToAudioNode` を使うと Comfy Cloud に歌詞やプロンプトを送るので、先に本人の承認が必要です。

1. MCP が `approval_required` と確認用リンクを返します。この時点ではジョブも GPU 呼び出しもありません。リンクの有効期限は15分です。
2. 自分のブラウザでリンクを開いてログインします。ノードごとに、送信先、課金先、プロンプト・歌詞・モデルなどの入力を確認してください。画像・音声・動画を渡す場合は、送信に使う元の素材もその場で確認できます。最終料金は事前には確定しません。
3. **「この1件を承認して実行」**を押すと、その内容で1件だけキューに入り、ページに `prompt_id` が出ます。「送信しない」を押すか期限が切れれば実行しません。HTTPS MCP は元の `request_id` とグラフを再送すれば同じ結果を取得できます。

例：Comfy の Partner Node 経由で Gemini・OpenAI・OpenRouter を呼ぶ場合、引き落とし先は **Comfy のクレジット**です。対応する直接接続ノードで Gemini や OpenRouter を使う場合は、それぞれの API アカウントに課金されます。MCP の OAuth ログインでは外部送信を承認したことになりません。

送信先や送信内容を判断できないノードは、実行前に理由を返します。たとえば、先行ノードで作った画像をそのまま外部に送ろうとした場合は、まずローカル生成を終え、`reuse_output` で入力素材にしてから外部ノードに渡してください。承認は1つのグラフと素材にだけ有効です。内容を変更したら、新しい `request_id` で依頼します。

## 手元のファイルを送る

Claude Code や Codex では、このリポジトリのルートで次のヘルパーを使います。プロジェクト指定の Node.js で依存パッケージを準備し、送りたいファイルだけを引数に渡してください。

```bash
npm ci --prefix mcp --ignore-scripts
node mcp/upload.mjs "https://<SPLIT_PUBLIC_HOST>/mcp" photo.png sound.wav clip.mp4
```

端末に表示された URL でログインすると、ファイルごとに ComfyUI のローダーで使える名前が返ります。チャットに Base64 を貼る必要はありません。中断した場合は同じファイルでコマンドを再実行してください。途中まで送った素材はローダーの候補に現れません。

1ファイルの上限は **256 MiB** です。画像は PNG・JPEG・WebP、音源は WAV・FLAC・MP3・M4A・Ogg/Opus、動画は MP4・MOV・WebM・MKV を入力に使えます。

## 生成中・生成後にできること

保存済み画像・音声・動画と `list_outputs` は専用の配信用 CPU が Volume を読みます。ComfyUI CPU・GPU は起動しません。取得中は配信用 CPU の料金が発生し、利用が終わると0台に戻ります。原本は Modal に残り、取得で削除しません。`get_image` は8 MiBまでインラインで返し、大きい画像は認証付きリンクを返します。

ワークフロー一覧・元 JSON（`get_workflow` の `format: "ui"`）・保存済み API グラフの取得と、`get_history`・`get_job_workflow` も配信用 CPU だけで応答します。画面で保存したJSONを「どのノードに何という名前で値を渡すか」という生成依頼用の形式に直すときだけ、ComfyUI CPUで現在の入力仕様を確認します。すでに生成依頼用の形式で保存されていれば、その変換は不要です。[取得時の分岐図](../design/split-mcp-routing.md#ワークフロー取得でcomfyuiを起こす場合)で確認できます。履歴は保存時点の記録なので、実行中の最新状態には `get_job_progress` を使ってください。

`list_models` は既定で保存ファイルの一覧・サイズ・更新日時を返します。保存されていることと、ComfyUI が利用できることは別です。「ローダーで実際に選べるモデルを確認して」と頼む場合は `source: "runtime"` を指定し、ComfyUI CPU で認識一覧を取得します。

| 聞きたいこと | 使うツールと答え |
| --- | --- |
| いまどのノード？ 何ステップ目？ | `get_job_progress(prompt_id)`。実行中のノードと、そのノードが通知した `value/max` を返します。全体の完了率ではありません。 |
| 何秒かかった？ | 同じツールの `execution_seconds` は ComfyUI の実行時間、`elapsed_seconds` は待機を含む受付からの経過時間、`worker_seconds` は準備・出力保存を含む GPU ワーカーの処理時間です。記録のない値は `null` です。 |
| なぜ失敗した？ | `get_job_progress` で失敗ノードと例外の要約、`get_history(action: "diagnose", prompt_id: …)` で履歴の詳細を取得します。 |
| この1件だけ止めたい | `cancel_job(prompt_id)`。待機中は `cancelled`、実行中は `cancel_requested` が返ります。後者は受付なので、同じ ID でもう一度状態を確認してください。完了済みの出力は残ります。 |
| 保存されているモデルは？ | `list_models()` でカテゴリ、`category: "checkpoints"` などで保存ファイル名・サイズを取得します。`query` は部分一致検索、次ページは返された `next_offset` を `offset` に指定します。 |
| ComfyUIのモデル選択欄に何が出る？ | `list_models(source: "runtime", category: "checkpoints")`。ComfyUI CPUに問い合わせます。重みをロードして生成できるかを試す操作ではなく、GPUは起こしません。 |
| ノードの入力を調べたい、グラフを編集したい | `create_workflow` の `node_info`・`modify`・`validate`。編集や検証だけで生成は始まりません。 |
| 待機中のジョブを一覧にしたい | `queue(action: "list")`。別のチャットやクライアントからも、同じ `prompt_id` で個々のジョブを追跡できます。 |

曲を聴くには「このジョブの曲を `get_audio` で再生して」と頼みます。MP3・WAV・FLAC・Ogg・Opus を元の形式のまま取得します。ChatGPT では、曲名が表示されたら「再生」を押します。一時停止と再生位置の変更もできます。4 MiB を超える曲は、先に「音声を読み込む」を押してください。チャット内再生の上限は元ファイル64 MiB・10分・展開後の音声128 MiBです。圧縮ファイルでも展開量を再生前に確認します。上限を超える曲、展開量を確認できない形式、元ファイルを保存したい場合は、返った再生リンクを使います。

「アプリを表示できませんでした」と出たら、曲を生成し直す必要はありません。再生リンクが見えないときは「同じ `prompt_id` の `list_outputs` から曲のブラウザ用リンクを出して」と頼んでください。リンクは split の `/view?type=output&filename=…&subfolder=…` で、`filename` と `subfolder` はそれぞれ URL エンコードします。split にログインして開くと、曲を再生・保存できます。ブラウザが再生に対応しない形式でも保存できます。

音声も MP4 動画も、複数出力があれば `index`（0始まり）で選べます。通常の `get_audio` は再生用の情報を返します。音声そのものを CLI で受け取るには、小さい曲でも `byte_offset: 0` を指定し、`next_offset` が `null` になるまで取得してください。動画から音声だけを抽出する機能はありません。

## 保存したグラフを調べて、もう一度使う

「このワークフローで動画の長さを決めているノードを探して」と頼むと、`get_workflow(action: "analyze")` で構成を読み、`query` でタイトル・クラス・接続を絞れます。`strip` は編集用 JSON を実行グラフに整理し、`slice` は指定グループを取り出します。`visualize_workflow` は渡したグラフの図や DSL を返します。変更を残すなら `save_workflow` に新しい保存名を渡してください。

過去の生成をやり直すには `get_job_workflow(prompt_id)` で実際に投入したグラフを取得し、変更してから **新しい** `request_id` で投入します。元の出力を次の生成に渡したい場合は、`list_outputs(prompt_id)` の `index` を `reuse_output` に指定します。返ったファイル名はそのままローダーに渡せます。`list_inputs` は入力素材の検索、`inspect_media` は寸法・再生時間・音声情報の確認用です。

## カスタムノードとモデルを管理する

**カスタムノード**：「このクラスはインストール済み？」なら `list_nodes`、追加するパックを探すなら `search_custom_nodes` です。追加・更新は次の順番です。候補は稼働中の環境を維持する仕組みです。インストール時はパックのコードが共有 Volume を持つコンテナで動くので、使うパックを選んでください。

1. `get_split_status` で稼働中の版（`active`）を取得し、`prepare_environment(active, request_id)` で検証用コピー（`candidate`）を作ります。候補がある間、通常の生成は受け付けません。
2. `change_node(candidate, action: "install" または "update", node, request_id)` にレジストリのパック ID を渡します。`get_admin_operation(request_id)` で処理結果を確認します。`running` はインストール完了ではありません。
3. `apply_environment(candidate, request_id)` は候補を CPU と GPU で検証し、成功時だけ稼働版を切り替えます。適用しない場合は `discard_environment` で候補を破棄できます。

**モデル**：`list_models` でカテゴリを確認し、`add_model(repo, file, category, request_id)` で Hugging Face の safetensors・GGUF・ONNX ファイルを追加します。専用の CPU で取得するので GPU は使いません。必要なら `revision` と `sha256` を指定してください。`get_admin_operation` は完了後に取得したコミットと SHA-256 を返します。

削除する場合は `inspect_model(path)` が返した `version` を `remove_model(path, version, request_id)` に渡します。版はファイル全体の SHA-256 です。大きなファイルでは照会・退避の応答に `request_id` が返るので、`get_admin_operation` で完了を確認してください。そのファイルだけを退避します。元に戻すには `restore_model` の `removal_id` に**削除時の** `request_id`、`request_id` には**新しい復元操作用のID**を指定します。削除時のIDを両方に使うと競合します。追加・復元は既存ファイルを上書きしません。

管理操作の通信が途切れたら、同じ `request_id` で結果を確認してください。`failed` は失敗、`unknown` は結果未確認です。後者では対象の状態を確認してから次の操作を決めてください。

各ツールの主な引数と影響は[ツール一覧](tools.md)にまとめています。上流にある機能が見つからない場合は、[上流との対応表](upstream.md)で、split での呼び方と未提供の理由を確認できます。

## 困ったとき

- **CPU が起動中**：少し待って同じ操作を再試行してください。「初期化失敗」や実行モードの競合なら、再投入せず管理者に設定を確認してください。
- **ツールが見当たらない**：ChatGPT の接続設定で「ツールを更新」、CLI では新しいセッションでツール一覧を取得してください。
- **Web 添付が保存できない**：応答にローダー用のファイル名があるか確認してください。配信元が拒否された場合は、管理者が該当する配信ホストを確認します。署名付き URL をチャットやログに貼る必要はありません。

サーバーの準備・配信用 CPU の設定・旧 stdio 接続の廃止は、[管理の手順](setup.md)を参照してください。
