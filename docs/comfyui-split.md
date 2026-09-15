# GPUを生成時だけ使うComfyUI

`splitapp.py` は既存の `comfyapp.py` と別の `comfyui-split` App を作る。
CPUサーバー・GPUワーカーとも最小0台・最大1台、停止待ちは30秒。
画面を閉じ、生成キュー・環境更新がなくなるとCPUも自動停止する。
画面のWebSocket接続や定期リクエストが続く間はCPUが稼働する。
次に開くときはCPUとComfyUIの起動待ちが発生する。
バックグラウンド生成・環境更新中はModalのautoscalerでCPUを一時的に1台維持し、
結果の保存完了後に最小0台へ戻す。従来モードは明示的な常駐設定のため維持する。
コンテナ停止後も保存済みVolumeのストレージ料金は発生する。
UI閲覧、WebSocket、ワークフロー保存、ファイル閲覧はGPUを呼ばない。
GPUは生成、環境検証、明示的な従来モードでだけ使用する。

```bash
./scripts/modal.sh deploy splitapp.py
```

検証用URLはModal Proxy Authを必須とする。Cloudflare Workerの既存
`MODAL_ORIGINS` で新しいホスト名をこのURLへ向ければ、既存のAccess認証を使える。
本番ホスト名の接続先は受け入れ検証後に切り替える。

## 保存先

- モデル・入力・出力は既存のVolumeを利用。
- ユーザーデータは初回に `comfy-user-data` から `comfy-split-user-data` へ複製。
  以後は独立して保存する。検証中の設定変更で従来環境を変更しない。
- 既存custom nodeを初回に環境Volumeへコピーし、依存を復元する。
- `comfy-split-environments`: ノード、仮想環境、検証したノード定義。
- `comfy-split-state`: CPUが書くジョブ受付・状態・環境選択。
- `comfy-split-results`: GPUが書くジョブごとの実行結果。

GPUは入力のreload後に実行し、出力のcommit後に結果を保存する。
CPUは出力をreloadしてから完了を通知する。稼働中のSQLiteを共有しない。
一時出力は通常のローカル作業領域で生成し、外部側が出力Volumeの `.split-temp/<起動ID>/` へコピーする。
履歴と完了イベントの参照を永続出力へ変換してから公開する。
従来の `.split-temp/temp` に保存済みの一時出力も引き続き閲覧できる。

## 操作

標準サイドバーの「GPU」タブで稼働状態を確認できる。モード変更の操作は折りたたまず表示する。
パネルにはGPUの状態と稼働コンテナ数を表示する。CPU側がModalの管理API
`gpu_worker.get_current_stats()` を取得し、5秒間キャッシュする。
画面も5秒ごとに `/modal-control/v1/status` を読み、生成キュー・モードと組み合わせて
停止中、起動待ち、使用中、停止待ち、常駐中、環境検証中を表示する。
取得失敗時は停止と推測せず「確認できません」にする。
表示のためにGPUワーカーを実行することはない。パネルに最終確認時刻と説明を表示する。キャンバス上の固定ボタン・独自ダイアログは設置しない。
UIは独立した [ComfyUI-Modal-Control](../extensions/ComfyUI-Modal-Control/README.md) に置く。
標準の `WEB_DIRECTORY` によって配信し、ゲートウェイによる `/split.js` の追加配信は行わない。
ComfyUI本体を変更せず、追加のcustom_nodes検索パスから読み込む。

- **分離モード**: CPUにキューを保存し、1件ずつGPUで実行する。
  GPU呼び出しは生成・保存の完了まで継続し、終了後30秒で停止対象となる。
- **従来モード**: 同じGPUワーカーでComfyUI全体を実行する。
  GPU待機料金が発生する。切替前にワークフローのバックアップを保存する。
  GPUトンネルはセッション固有のBearer認証を持ち、ブラウザには鍵を渡さない。
  CPUからのheartbeatが90秒途絶えた場合は新規投入を停止し、キュー完了後に終了する。
- **Manager**: 追加・更新は候補環境に対して行う。再起動、または
  「ノード更新を検証・反映」で依存確認、CPU起動、GPU検証を行い、有効環境を切り替える。
  失敗時は旧環境を維持し、「未反映の更新を破棄」で戻せる。

生成・待機ジョブがある場合はモード切替とノード更新を拒否する。
ManagerからのComfyUI本体更新は提供せず、固定バージョンを再デプロイで更新する。
主要CUDAパッケージ・フロントエンド・Managerの制約を依存インストールにも適用する。

## APIと復旧

ComfyUIの `/prompt`, `/queue`, `/history`, `/ws` と `/api/` プレフィックスを扱う。
ジョブ一覧APIは外部キューのスナップショットを標準拡張へ渡して整形する。
ComfyUI内部のキュー・履歴メソッドは差し替えない。

追加API:

- `GET /split/status`: モード、環境更新、結果不明ジョブ。
- `GET /modal-control/v1/status`: 同じ状態を返すバージョン付きAPI。`api_version: 1`。
- `POST /split/mode`: `{"mode":"split"}` または `{"mode":"legacy"}`。
- `POST /split/environment/apply`: 候補環境を作成・検証して反映。
- `POST /split/environment/discard`: 未反映の候補を破棄。
- `/prompt` の `Idempotency-Key` ヘッダー: 同一キー・同一内容の再送を重複受付しない。
- `POST /jobs/<id>/cancel`: 指定ジョブの待機キャンセル、またはそのGPU workerへの中断指示。

[Ambient](ambient.md)はCPUの `ui` URLを接続先にする。モデル確認、WebSocket接続、
結果取得はCPU側で処理し、H3とComfyUI版FastH3の生成をこのキューへ投入する。
`X-Modal-Execution-Mode: split` を付けたリクエストは、従来モードでは409を返す。
事前の状態確認後にモードが変わっても、Ambientのリクエストを従来モードのGPUへ転送しない。
通常のComfyUI画面はこのヘッダーを送らず、従来どおりモードを切り替えて使える。

`/modal-control/v1/status` の `dependencies` は実行中のCPU ComfyUI環境の
comfy-kitchen版、固定版、必要APIの不足を返す。GPUカーネルの動作検証とは区別する。
comfy-kitchenは上流ComfyUIの指定版を固定依存に含め、イメージ構築時と仮想環境の
適用時に検査する。古いVolume上の仮想環境が別版を優先している場合はCPU側の
レポートに反映し、AmbientのFastH3生成前に検出する。修復は既存の環境更新手順で行う。

GPU呼び出し前にdispatch intentを保存し、呼び出しIDを取得後に保存する。
CPUが間で停止してIDを記録できなかった場合は `unknown` とし、結果記録を待つ。
結果不明のジョブは自動再実行せず、後続投入の実行も停止する。
管理者はModalのGPU呼び出しとresults Volumeを確認してから復旧する。
ネットワークエラーだけを根拠に再投入しない。
GPU関数の実行タイムアウトは失敗として確定し、結果待ちのポーリングタイムアウトと区別する。

GPU側も実行前に開始記録をcommitする。[Modalのプリエンプション](https://modal.com/docs/guide/preemption)
では同じ入力が再開されるため、開始記録のみ残っている場合は `unknown` とし、再実行しない。

CPUの再デプロイでは旧CPUの停止を確認してから起動する。
状態Volumeへの複数デプロイからの同時書き込みはサポートしない。
検証時も従来Appを別途起動するとGPUを別枠で確保するため、同時に使わない。

## 互換性と計測

CPUでimportできるカスタムAPIはCPU側で実行する。GPU専用ノードは検証時の
ノード定義とJSを配信し、モデルリストと一致する選択肢はCPU側で更新する。
独自の動的APIなど、分離で再現できない機能には従来モードを使う。
任意のカスタムノードの動作を保証するものではない。

構造化ログの `comfy_ready`, `gpu_ready`, `gpu_finished` で起動と総処理時間を確認できる。
モデルロードの詳細時間はComfyUIログ、停止はModal Containers画面で確認する。
GPU Memory Snapshotsは未使用。

検証コマンド:

```bash
uv run python -m unittest discover -s tests -q
uv run ruff check comfy_split splitapp.py tests/test_comfy_split.py
```

実機ではGPUゼロのまま10分間の編集・保存・閲覧、生成とプレビュー、
30秒停止、連続生成、再接続、Manager更新と失敗時復帰、モード往復を確認する。
ローカルのプロトコルテストだけで実機の受け入れ完了とはしない。

`scripts/verify_split_execution.py` は検証用CPUコンテナ内で実行するAPIスモークテスト。
2件の投入と待機キャンセル、画像保存・表示、WebSocketイベント、モード往復、GPU停止を確認する。
EmptyImageを使うため、モデル推論やサンプリング中のプレビューの検証には含めない。

`scripts/verify_split_idle.py` は同じCPUコンテナ内で10分間実行する。
WebSocketを保持し、画面配信・メタデータ取得・画像アップロード・表示・
ワークフロー保存と読み取りを繰り返し、各回でGPU台数が0であることを確認する。
検証ファイルは `split-verification` 名で入力・ユーザーデータへ保存する。
このAPIテストとは別に、Access経由で標準フロントエンドを操作する受け入れ確認が必要。

### 保存済み環境のImage Browsing修復

イメージ更新だけではVolume上の既存ノードは更新されない。
認証済みの検証用エンドポイントへ `POST /split/environment/repair-image-browsing` を送ると、
アクティブ環境を複製し、Image Browsingだけをイメージ同梱版に戻した候補を作成する。
通常の依存・CPU・GPU検証を通過後に有効化する。旧環境と他の追加済みノードは保持する。
生成中・キュー待ち・別の候補環境がある場合は409で拒否する。
GPU検証には一時的なGPU起動を伴う。結果は `/split/status` の `candidate` と
`environment` で確認でき、失敗時は旧環境を使い続ける。

`scripts/verify_image_browsing.py` は修復後のCPUコンテナ内で実行するAPI検証。
GPUが0台に戻った後、Web拡張配信、一覧、アップロード、プレビュー、名前変更、削除、
既存MiniMax生成結果の参照を確認する。作成した検証ファイルだけを削除する。
実ブラウザでの画面操作はこのAPI検証とは別に確認が必要。

GPU状態は標準ツールバー内の独立したボタンにも常時表示し、クリックするとGPUサイドバーを切り替える。
位置の固定やキャンバスへの重ね描きは行わない。

台数表示は `GPU(0)` の形式とし、稼働中は赤、取得失敗時は `GPU(?)` とする。


## 本体への介入の削減

| 処理 | 変更前 | 現在 |
|---|---|---|
| 起動 | 独自bootからmainをimportして起動 | 通常の `python main.py` |
| CPU生成スレッド | `main.prompt_worker` を無効化 | 標準のスレッドを使用、投入されないため待機 |
| キュー読み取り | 3メソッドを上書き | 上書きなし。外部ゲートウェイと拡張ルートで提供 |
| 履歴 | `PromptQueue.get_history` を上書き | 上書きなし |
| 一時ファイル | `main.cleanup_temp` を無効化 | 通常の削除処理。外部で結果を永続化 |
| ノード情報 | 独自bootがルート追加 | 標準カスタム拡張のルート登録 |
| CPUへの直接投入 | `PromptQueue.put` を拒否 | CPU専用ガードとして維持 |

実行時の関数上書きは7箇所から1箇所へ削減した。
残る上書きは、独自ノードがCPUのキューへ直接投入しても実行させないためのもの。
本体の生成無効化APIがないため、対応シグネチャを確認して適用する。
ジョブ整形関数・ノードカタログには読み取り用の内部API依存が残るが、
[ComfyUI-Modal-Bridge](../extensions/ComfyUI-Modal-Bridge/README.md) 内へ限定する。
Managerのインストール処理自体は再実装せず、既存の排他と候補環境への中継を維持する。
split前のWebSocket圧縮・user_managerソースパッチは、この分離構成では適用していない。

### 外部拡張化の検証方法

- 単体・回帰テストを実行する。
- `scripts/verify_comfy_bridge.py` で、ブリッジなしの標準起動とCPU生成、
  分離側のCPUガード・ジョブAPIを確認する。
- 実モデルで生成し、進捗・バイナリプレビュー・通常出力・一時出力の保存と閲覧を確認する。
- 複数ジョブの投入、待機キャンセル、実行中断、従来モードとの往復を確認する。
- WebSocketを接続したままGPUが0台に戻ることを確認する。
  停止待ち設定は30秒だが、実際の停止時刻はModalの制御に依存する。
- CPU再起動後の履歴・ジョブ詳細・画像の復元と、実行中ジョブが再投入されないことを確認する。
- 実ブラウザで標準画面・GPU表示・生成履歴・画像表示を確認する。
- Managerの応答、追加済みノードの保持、Image Browsingのファイル操作を確認する。
