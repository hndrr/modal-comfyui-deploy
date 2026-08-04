# Modal ComfyUI のアイドル停止計画

## 目的

ComfyUI で画像や動画を生成していない間は GPU コンテナをゼロ台まで縮退させ、Modal の GPU・CPU・メモリに対するコンピュート消費を抑える。

デプロイ済みの URL と Modal Volume は維持し、次回アクセス時にはコールドスタートで ComfyUI を再起動できる構成とする。

## 現状

`comfyapp.py` の `ui` Function には、次の値が直接指定されている。

```python
@app.function(
    max_containers=1,
    scaledown_window=30,
    timeout=1800,
    # ...
)
```

- `scaledown_window=30`: アクティブな入力がなくなってから、コンテナを縮退させるまでの最大アイドル時間は30秒
- `timeout=1800`: 1つのFunction入力または接続を実行できる最大時間は1,800秒
- `min_containers` は未指定: Modalの既定動作により、入力がなければゼロ台まで縮退できる
- `max_containers=1`: ComfyUI用コンテナは最大1台

Modal Function は、処理する入力がない場合に既定でゼロ台まで縮退する。そのため、デプロイ済みAppが存在するだけでGPUコンテナが常時起動する構成ではない。

参考: [Modal — Scaling out](https://modal.com/docs/guide/scale)

## timeoutとアイドル停止の違い

### `scaledown_window`

コンテナにアクティブな入力がない状態が続いたとき、Modalのオートスケーラーがコンテナを停止するまでの最大待機時間を指定する。

値を短くするとアイドル中のコンピュート消費を減らせる一方、短時間で再アクセスした場合にもコールドスタートが発生しやすくなる。

今回の既定値は30秒を維持する。

### `timeout`

Functionの入力や接続を実行できる最大時間を指定する。アイドル状態を検出する設定ではない。

ComfyUIの生成処理やWebSocket接続がこの時間を超える可能性がある場合は、十分に長い値を設定する必要がある。今回の既定値は1,800秒を維持する。

## ComfyUIとWebSocketの注意点

ComfyUIはブラウザとの状態同期にWebSocketを使用する。ModalではWebSocket接続もFunctionへのアクティブな入力として扱われるため、生成処理が終わっていてもブラウザのタブを開いたままにすると、コンテナがアイドルと判定されない可能性がある。

GPUコンテナを確実にscale-to-zeroさせる運用は次のとおりとする。

1. ComfyUIのキューと生成処理が完了したことを確認する。
2. ComfyUIを開いているすべてのブラウザタブやクライアントを閉じる。
3. アクティブなHTTP・WebSocket接続がなくなった後、`scaledown_window` の範囲内でコンテナが縮退するのを待つ。
4. 次回は同じデプロイ済みURLへアクセスし、コールドスタートで再起動する。

参考: [Modal — Web Functions / WebSockets](https://modal.com/docs/guide/webhooks#websockets)

タブを開いたまま「生成中かどうか」だけを監視してGPUを停止する仕組みや、手動Sleepボタンは今回の対象外とする。

## 追加予定のデプロイ時設定

ハードコードされている値を、`.env` またはシェル環境変数からデプロイ時に変更できるようにする。

```env
COMFYUI_SCALEDOWN_WINDOW=30
COMFYUI_FUNCTION_TIMEOUT=1800
```

### `COMFYUI_SCALEDOWN_WINDOW`

- `@app.function(scaledown_window=...)` に渡す
- 単位は秒
- 既定値は `30`
- 許容範囲は `2` から `1200`
- 短い値ほどコンピュート消費を抑えやすいが、再アクセス時のコールドスタートが増える

### `COMFYUI_FUNCTION_TIMEOUT`

- `@app.function(timeout=...)` に渡す
- 単位は秒
- 既定値は `1800`
- 許容範囲は `1` から `86400`
- 想定する最長の生成処理や接続時間より長く設定する

いずれも整数として検証し、空文字、非数値、範囲外の値が指定された場合は、Modalのイメージビルドを開始する前に対象の環境変数名と許容範囲を含むエラーを出す。

これらは `comfyapp.py` の読み込み時に決定されるデプロイ設定である。値を変更した場合は、次のように再デプロイする必要がある。

```bash
COMFYUI_SCALEDOWN_WINDOW=10 \
COMFYUI_FUNCTION_TIMEOUT=3600 \
uv run modal deploy comfyapp.py
```

## 実装手順

1. `comfyapp.py` に環境変数名、既定値、解決後の設定値を追加する。
2. 整数値と許容範囲を検証する共通ヘルパーを追加する。
3. `@app.function` に `min_containers=0` を明示する。
4. ハードコードされた `scaledown_window=30` と `timeout=1800` を、解決済みの設定値へ置き換える。
5. コンテナ起動ログへ、適用された `min_containers`、`scaledown_window`、`timeout` を出力する。
6. `.env.example` に2つの環境変数と既定値を追加する。
7. READMEに設定方法、再デプロイの必要性、WebSocketとコールドスタートに関する注意事項を追加する。

## 検証計画

### 自動検証

- 環境変数が未指定の場合に、`30` と `1800` が選択されること
- 許容範囲内の整数で設定値を上書きできること
- 最小値と最大値を受け入れること
- `0`、負数、上限超過、空文字、非数値を拒否すること
- エラーに対象の環境変数名と許容範囲が含まれること
- PythonのコンパイルチェックとRuffの静的チェックが成功すること

### Modal上での手動検証

1. 短い `COMFYUI_SCALEDOWN_WINDOW` を指定してデプロイする。
2. ComfyUIを開き、生成処理が正常に完了することを確認する。
3. すべてのComfyUIタブを閉じる。
4. Modal Dashboardで、アクティブな接続がなくなった後にコンテナ数がゼロになることを確認する。
5. 同じURLを再度開き、コールドスタート後にComfyUIへ接続できることを確認する。
6. モデル、入力、出力、ユーザーデータ、workflowが各Modal Volumeから復元されることを確認する。

## 課金上の注意

scale-to-zero後はGPU・CPU・メモリの実行コンテナに対するコンピュート消費を抑えられる。ただし、次の利用分は別に扱われる。

- `scaledown_window` の間にコンテナが起動している時間
- コールドスタートとComfyUI起動に必要なコンピュート
- モデル、生成物、入力、ユーザーデータを保持するModal Volumeのストレージ

Volumeはコンテナがゼロ台になっても削除せず、次回起動のために永続化する。

参考:

- [Modal — Cold start performance](https://modal.com/docs/guide/cold-start)
- [Modal — Volumes](https://modal.com/docs/guide/volumes)
- [Modal — Pricing](https://modal.com/pricing)

## 完了条件

- デプロイ時にアイドル停止時間とFunction timeoutを環境変数で指定できる
- `min_containers=0` がコード上で明示されている
- 接続がなくなるとGPUコンテナがゼロ台まで縮退する
- 設定値が不正な場合はデプロイ前に明確なエラーになる
- 既定値を使う場合は現在の `scaledown_window=30`、`timeout=1800` と同じ動作になる
- 永続化済みのVolumeデータはscale-to-zeroと再起動をまたいで維持される
