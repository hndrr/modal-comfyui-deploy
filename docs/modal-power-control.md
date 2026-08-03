# Modal ComfyUI Sleep/Wake 構成案

## 目的

ComfyUI のタブを開いたままでも、生成していない時間は GPU コンテナを scale-to-zero できるようにする。

この文書では、現在の ComfyUI と Modal の構成を大きく変更せずに導入できる Sleep/Wake 機能と、より厳密に UI と GPU 実行環境を分離する将来案を整理する。

## 現行構成の課題

現在の `comfyapp.py` は、ComfyUI の Web UI、API、WebSocket、生成エンジンを1つの GPU コンテナで実行している。

```text
Browser
  ├─ HTTP ────────┐
  └─ WebSocket ───┼─> Modal GPU container
                  │     └─ ComfyUI UI / API / generation
                  └─ scaledown_window=30
```

Modal Function は、処理中の入力がなくなるとゼロ台まで縮退できる。現在も `scaledown_window=30` が設定されているため、すべてのHTTP・WebSocket接続が終了すればGPUコンテナは停止できる。

一方、ComfyUIは次の用途でブラウザから `/ws` へWebSocket接続を維持する。

- キュー状態の更新
- 生成開始・完了・失敗の通知
- ノードごとの進捗通知
- プレビュー画像の転送
- 実行ログやステータスの同期

タブを開いたままにすると、生成していない状態でもWebSocketがModal Functionへのアクティブな入力として残り、コンテナがアイドルと判定されない可能性がある。

さらに、ComfyUIフロントエンドはWebSocketが閉じると短い待機時間後に自動再接続する。単純に `socket.close()` を呼ぶだけでは接続が復元されるため、Sleep機能では通常の再接続処理も抑止する必要がある。

参考:

- [Modal — Scaling out](https://modal.com/docs/guide/scale)
- [Modal — Web Functions / WebSockets](https://modal.com/docs/guide/webhooks#websockets)
- [ComfyUI — Server Routes and WebSocket Communication](https://docs.comfy.org/development/comfyui-server/comms_routes)
- [ComfyUI Frontend — WebSocket implementation](https://github.com/Comfy-Org/ComfyUI_frontend/blob/main/src/scripts/api.ts)

## 推奨する段階導入

最初の実装ではCPUフロントとGPUワーカーを分離せず、ComfyUIのカスタムJavaScript拡張としてPower Controlを追加する。

拡張はブラウザ内のworkflowと画面を残したまま、ComfyUIとのWebSocket接続だけを明示的に停止する。接続がなくなった後のコンテナ停止はModalのオートスケーラーに任せる。

```text
Awake
  Browser ── WebSocket ──> GPU container

Sleep
  Browser keeps workflow UI
  Browser     no connection  GPU container ──> scale-to-zero

Wake / Queue
  Browser ── reconnect ───> cold start ───> GPU container
```

### v1で提供する機能

- 手動の `Sleep GPU` ボタン
- 手動の `Wake GPU` ボタン
- キューが空になってから60秒後の自動Sleep
- 生成中・待機中キューがある場合のSleep拒否
- 同一オリジンで開かれた複数タブへの一括Sleep通知
- Sleep中にQueueを実行した場合の自動Wake
- Wake失敗時のエラー表示とRetry
- ComfyUI設定画面からの自動Sleep有効化・待機時間変更

### 既定設定

| 設定 | 既定値 | 制約 |
| --- | ---: | --- |
| 自動Sleep | 有効 | UIから無効化可能 |
| 自動Sleep待機時間 | 60秒 | 10〜3,600秒 |
| Modal `scaledown_window` | 30秒 | 現在のコード値を利用 |
| Wake接続待ち | 最大120秒 | 超過時はRetry可能なエラー |
| 生成中の手動Sleep | 拒否 | 強制中断は提供しない |

既定設定の場合、最後のキュー完了から最大でおおむね90秒後にGPUコンテナが停止対象になる。内訳は、ブラウザ側の自動Sleep待ち60秒とModal側の `scaledown_window` 最大30秒である。

## UIと公開インターフェース

ComfyUIが提供するJavaScript Extension APIを利用する。

- サイドバー `Modal Power`
  - 現在の状態
  - 自動Sleepまでの残り時間
  - running/pendingキュー数
  - Sleep/Wakeボタン
  - 最後のエラーとRetry
- トップバーメニュー `Extensions > Modal Power`
  - `Sleep GPU`
  - `Wake GPU`
- コマンド
  - `modal.power.sleep`
  - `modal.power.wake`
- 設定
  - `Modal.PowerControl.AutoSleepEnabled`: boolean、既定 `true`
  - `Modal.PowerControl.AutoSleepDelaySeconds`: number、既定 `60`、範囲 `10–3600`
- タブ間通信
  - BroadcastChannel名: `modal-comfyui-power-v1`

参考:

- [ComfyUI — JavaScript Extensions](https://docs.comfy.org/custom-nodes/js/javascript_overview)
- [ComfyUI — Settings](https://docs.comfy.org/custom-nodes/js/javascript_settings)
- [ComfyUI — Topbar Menu](https://docs.comfy.org/custom-nodes/js/javascript_topbar_menu)
- [ComfyUI — Sidebar Tabs](https://docs.comfy.org/custom-nodes/js/javascript_sidebar_tabs)

## 状態モデル

Power Controlはタブごとに次の状態を保持する。

| 状態 | 意味 | 主な遷移条件 |
| --- | --- | --- |
| `awake-busy` | WebSocket接続中で、runningまたはpendingキューがある | キューが空になると `awake-idle` |
| `awake-idle` | WebSocket接続中で、キューが空 | タイマー満了または手動操作で `sleeping` |
| `sleeping` | WebSocketを意図的に切断し、再接続を停止 | WakeまたはQueue操作で `waking` |
| `waking` | WebSocket再接続とModalのコールドスタートを待機 | 接続成功で `awake-idle`、失敗で `error` |
| `error` | Sleep/Wake処理または互換性確認に失敗 | Retry成功で対応する状態へ復帰 |

### 状態遷移

```text
                  queue starts
     ┌────────────────────────────────┐
     │                                v
awake-idle <── queue becomes empty ── awake-busy
     │                                │
     │ timer / manual Sleep           │ manual Sleep
     v                                └─ rejected
  sleeping
     │
     │ Wake / Queue
     v
   waking ── connected ──> awake-idle
     │
     └─ timeout / incompatible API ──> error ── Retry
```

## 処理フロー

### 自動Sleep

1. WebSocketの `status`、実行完了、失敗、中断イベントからキュー状態を追跡する。
2. runningとpendingが両方ゼロになったら60秒タイマーを開始する。
3. タイマー中に新しい実行が始まった場合はタイマーをキャンセルする。
4. タイマー満了時に `GET /queue` を呼び、サーバー全体のrunningとpendingが空であることを再確認する。
5. キューが存在する場合はSleepせず、次のステータス更新を待つ。
6. キューが空なら、同一オリジンの他タブへSleepを通知する。
7. 各タブが自身のWebSocketを再接続対象から外してからcloseする。
8. Modalがアクティブな入力なしと判定すると、`scaledown_window` に従ってGPUコンテナを停止する。

### 手動Sleep

1. `Sleep GPU` 操作時に `GET /queue` で最新状態を取得する。
2. runningまたはpendingが存在する場合はSleepを拒否する。
3. 拒否理由としてrunning/pending件数を表示し、生成やキューは変更しない。
4. キューが空なら全タブへSleepを通知し、WebSocketを閉じる。
5. `/interrupt`、キュー削除、コンテナ強制終了は行わない。

### Wake

1. `Wake GPU` 操作を行ったタブを `waking` にする。
2. ComfyUI APIの接続初期化処理を呼び、WebSocketを再作成する。
3. WebSocketリクエストによってModalのGPUコンテナがコールドスタートする。
4. 最大120秒、WebSocketがopenになるのを待つ。
5. 接続成功後にキュー状態を取得し、`awake-busy` または `awake-idle` へ移る。
6. timeoutや接続エラーの場合は `error` に移り、Retryを表示する。

Wakeは操作したタブだけで行う。他のタブはSleep状態を維持し、不要なWebSocket接続の再作成を避ける。

### Sleep中のQueue実行

1. ComfyUIのQueue処理を互換ラッパーで受ける。
2. 現在が `sleeping` ならWakeを開始する。
3. WebSocket接続が成功するまで元のQueue送信を保留する。
4. 接続成功後、元の引数と戻り値を維持してQueueを1回だけ送信する。
5. Wakeに失敗した場合はQueueを送信せず、ユーザーへエラーを表示する。

## 実装案

### カスタム拡張

リポジトリ管理の `modal_power_control` custom nodeを追加する想定とする。

```text
modal_power_control/
  __init__.py
  js/
    power_control.js
    power_state.js
```

- `__init__.py` は `WEB_DIRECTORY = "./js"` を公開する。
- `power_state.js` は状態機械とタイマーをDOMやComfyUI APIから分離して実装する。
- `power_control.js` はComfyUI API、UI、WebSocket、BroadcastChannelとの接続を担当する。
- ComfyUIの計算ノードは追加しない。
- 新しい公開HTTPエンドポイントは追加せず、キュー確認には既存の `GET /queue` を使う。

### Modalイメージへの組み込み

- ローカルのcustom nodeをModal Imageに含める。
- ComfyUI起動前に、専用ディレクトリだけを `comfy-custom-nodes` Volumeへ同期する。
- 同期時は `modal_power_control` の管理ファイルのみを更新し、他のcustom nodeやユーザー管理ファイルを削除しない。
- ComfyUIを起動すると、通常のcustom node読み込み経路でJavaScript拡張が配信される。

### WebSocket切断

現行フロントエンドでは、アクティブsocketを保持したままcloseすると自動再接続される。Sleep時は次の順序で処理する想定とする。

1. 現在のsocket参照をローカルへ退避する。
2. ComfyUI APIが保持するアクティブsocket参照を空にする。
3. 退避したsocketをcloseする。
4. closeイベントが通常の再接続を開始しないことを確認する。

この処理はComfyUIの公開ドキュメントで保証されたSleep APIではなく、現行フロントエンド実装に依存する。必要なプロパティや初期化関数が存在しない場合は、Power Control全体を無効化して通常の接続を維持する。部分的な書き換えや推測によるfallbackは行わない。

## 安全条件

- runningまたはpendingキューが1件でもあればSleepしない。
- Sleep機能から `/interrupt` を呼ばない。
- Sleep機能からキューや履歴を削除しない。
- Sleep機能からModal Appをstopまたはdeleteしない。
- Wake完了前にpromptを送信しない。
- Wake失敗時にpromptを自動再送しない。
- Queueラッパーは元の引数、`this`、Promise、例外を維持する。
- 同じQueue操作を重複送信しない。
- 未対応のComfyUIバージョンでは通常動作を優先し、Sleep機能だけを停止する。

## テスト計画

### JavaScript自動テスト

Node標準のtest runnerとfake timerを使い、外部JavaScriptテスト依存を追加せずに状態機械を検証する。

- 初期接続とキュー状態から正しい状態を選択する
- キューが空になると自動Sleepタイマーが始まる
- タイマー中に実行が始まるとSleepがキャンセルされる
- 設定変更時にタイマーが再計算される
- Sleep直前の再確認でキューが見つかった場合は接続を閉じない
- 生成中の手動Sleepを拒否する
- 複数タブへのSleep通知で各タブのsocketが閉じる
- Wakeは操作タブだけで実行される
- Sleep中のQueue操作がWake後に1回だけ送信される
- Wake timeout時にQueueを送信しない
- 必要なComfyUI APIが存在しない場合に通常接続を壊さない
- timer、event listener、BroadcastChannelを重複登録しない

### Python自動テスト

- bundled custom nodeが所定のVolumeディレクトリへ同期される
- 再同期で管理ファイルが更新される
- 他のcustom nodeやユーザーファイルが維持される
- custom nodeの `WEB_DIRECTORY` が正しく公開される
- PythonコンパイルチェックとRuffが成功する

### Modal上での手動検証

1. ComfyUIを開き、60秒を超えるworkflowを実行する。
2. 生成中に自動Sleepせず、手動Sleepが拒否されることを確認する。
3. 生成完了後もタブを開いたままにする。
4. 60秒後にブラウザのWebSocketが閉じることを確認する。
5. Modal Dashboardで、さらに `scaledown_window` 経過後にGPUコンテナがゼロ台になることを確認する。
6. タブ内のworkflowが表示されたままであることを確認する。
7. Wakeボタンでコールドスタートし、キューと履歴が取得できることを確認する。
8. 再度Sleepし、Sleep中にQueueを実行して自動Wakeと生成完了を確認する。
9. 複数タブを開き、1タブからのSleepですべてのWebSocketが閉じることを確認する。
10. 導入済みcustom nodeが定期HTTP通信を行い、scale-to-zeroを妨げていないことを確認する。
11. モデル、workflow、入力、出力、ユーザーデータが再起動後もVolumeから復元されることを確認する。

## 互換性リスク

### ComfyUIフロントエンド更新

ComfyUIのWebSocket生成関数、socketプロパティ、自動再接続処理は変更される可能性がある。現在のComfyUIインストールは特定のフロントエンドバージョンへ固定されていないため、`COMFYUI_FORCE_BUILD=on` で更新した際に互換性確認が必要になる。

対策:

- 起動時に必要なAPIをfeature detectionする
- 未対応時はSleep/Wakeボタンを無効化する
- 通常のComfyUI接続は変更しない
- ComfyUI更新時の手動確認項目にSleep、Wake、Queueを含める
- 対応確認済みのComfyUI frontendバージョンをドキュメントへ記録する

### custom nodeのバックグラウンド通信

custom nodeがWebSocket以外のHTTPリクエストを定期送信すると、Sleep後もGPUコンテナが起動または維持される可能性がある。

初回導入時はブラウザのNetworkパネルとModal Dashboardで、現在導入されているcustom nodeの通信を実測する。v1ではグローバルな `fetch` の上書きや未知のAPI遮断は行わない。これはcustom nodeの動作を壊す危険が高いためである。

### 複数クライアント

BroadcastChannelは同じブラウザ・同じオリジンのタブ間でのみ機能する。別ブラウザ、別端末、APIクライアントがWebSocketを維持している場合、その接続は別途閉じる必要がある。

## 厳密なCPU/GPU分離案

「生成処理の間だけGPUを起動する」ことを厳密に保証するには、UIとGPU実行環境を分離する必要がある。

```text
Browser
  └─ HTTP / WebSocket
         │
         v
CPU gateway / control plane
  ├─ UI assets
  ├─ workflow and user data
  ├─ queue state
  └─ progress relay
         │ generation request only
         v
GPU worker
  └─ ComfyUI execution engine
```

### CPU/GPU分離で必要になる機能

- ComfyUIフロントエンドの静的配信
- `/ws` の終端とComfyUI形式のイベント生成
- `/object_info`、モデル一覧、extensions、userdata、履歴の提供
- workflowの検証とGPUワーカーへの送信
- GPUからの進捗、プレビュー、成功、失敗イベントの中継
- custom node独自HTTP/WebSocket APIの互換レイヤー
- GPUワーカーの起動、timeout、失敗、再試行、キャンセル管理
- Volume上の入力・出力・ユーザーデータの同期

### 比較

| 観点 | JavaScript Sleep/Wake | CPU/GPU分離 |
| --- | --- | --- |
| 変更規模 | 小〜中 | 大 |
| 既存ComfyUI UI | そのまま利用 | API互換レイヤーが必要 |
| custom node互換性 | 比較的高い | 個別検証が必要 |
| GPU利用の厳密性 | ベストエフォート | 生成処理へ限定可能 |
| タブを開いたままのGPU停止 | 可能 | 可能 |
| 常時コスト | 原則なし。ただし背景通信次第 | CPUゲートウェイ分が継続し得る |
| 実装・保守リスク | ComfyUI socket内部への依存 | 分散実行とAPI互換性 |

v1ではJavaScript Sleep/Wake方式を採用する。Modal上の実測でWebSocketを切ってもGPUがscale-to-zeroしない場合、またはGPU利用を生成処理だけへ厳密に限定する必要が生じた場合に、CPU/GPU分離を別プロジェクトとして設計する。

## 課金上の注意

SleepによってGPUコンテナがゼロ台になると、GPU・CPU・メモリの実行コンテナに対するコンピュート消費を抑えられる。ただし、次の利用は残る。

- 自動Sleep待機時間中のGPUコンテナ
- `scaledown_window` 中のGPUコンテナ
- Wake時のコールドスタートとComfyUI起動
- モデルや生成物を保持するModal Volumeのストレージ
- 将来CPUゲートウェイを導入した場合のCPU・メモリ

Volumeはコンテナが停止しても永続化され、モデル、workflow、入力、出力、ユーザーデータを次回起動時に再利用する。

参考:

- [Modal — Cold start performance](https://modal.com/docs/guide/cold-start)
- [Modal — Volumes](https://modal.com/docs/guide/volumes)
- [Modal — Pricing](https://modal.com/pricing)

## v1完了条件

- タブを開いたまま手動Sleepできる
- キュー完了から60秒後に自動Sleepできる
- 生成中・待機中キューがある場合はSleepしない
- Sleep後にブラウザのWebSocketが再接続しない
- 複数タブのWebSocketを一括で閉じられる
- Modal DashboardでGPUコンテナがゼロ台になる
- WakeまたはSleep中のQueue操作でコールドスタートできる
- Wake後の生成進捗と結果がComfyUIへ表示される
- 未対応のComfyUI frontendでは通常接続を壊さず機能を無効化する
- 既存custom nodeとVolume上のデータが維持される
