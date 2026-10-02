# CPU/GPU分離構成のComfyUI（Experimental・実験的）

この構成は開発・検証中です。`splitapp.py` は、UIをCPU、生成をオンデマンドGPUで動かすModal App `comfyui-split` を作ります。通常構成の `comfyapp.py` が作る `comfyui` Appとは別です。
ComfyUI標準のワークフロー作成・編集・保存・読み込みに、任意拡張は必要ありません。

## デプロイと接続

```bash
./scripts/modal.sh deploy splitapp.py
```

このコマンドは、[Modalプロファイルの設定](modal-profiles.md)に従ってデプロイします。
拡張・追加ノード・中継の有効化設定は既定で無効です。設定方法と取得・更新手順は[任意拡張](split-integrations.md)を参照してください。

接続先はデプロイ出力の `ui` URLです。Modal Proxy Authが必須で、`COMFYUI_REQUIRES_PROXY_AUTH`による無効化はできません。
ブラウザで使う場合は、[Cloudflare Accessの設定手順](cloudflare-access.md)に従い、Workerの転送先設定、Custom Domain、Accessアプリの宛先と許可ポリシーを設定します。`MODAL_ORIGINS`にURLを追加するだけでは公開・認証の設定は完了しません。

## 起動と停止

CPUサーバー・GPUワーカーとも最小0台・最大1台、停止待ちは30秒。
30秒は停止待ちの設定値であり、実際に0台へ戻るまでの時間はModalの制御に依存する。
画面を閉じ、生成キュー・環境更新がなくなるとCPUも自動停止する。
画面のWebSocket接続や定期リクエストが続く間はCPUが稼働する。
次に開くときはCPUコンテナの起動・復元を待つ。CPU Memory Snapshotが利用可能で、保存時と有効な環境版が一致していれば、初期化済みのComfyUIを再利用する。再利用できない場合は通常起動する。復元後はジョブ状態とユーザーデータを読み直す。[CPU復元の詳細](cpu-memory-snapshot.md#本体への組み込み)。
バックグラウンド生成・環境更新中はModalのautoscalerでCPUを一時的に1台維持し、
処理と保存の完了後に最小0台へ戻す。GPUを常時起動するモードでは、画面を閉じてもCPUとGPUを維持する。
コンテナ停止後も保存済みVolumeのストレージ料金は発生する。
分離モードでは、UI閲覧、WebSocket、ワークフロー保存、ファイル閲覧はGPUを呼ばない。
GPUは生成、明示的な環境検証、常時起動モードで使用する。

`SPLIT_NODE_PACKS`で選択したノードは、デプロイ後の最初のアイドル状態でのCPU起動時に更新を確認します。カタログに`revision`があれば固定コミット、なければ配布元の既定ブランチのHEADを使います。
変更があれば候補環境で依存とCPUでの読み込みを検査してから採用します。この自動更新ではGPUを起動しません。失敗時は更新前の有効環境を使い、再試行条件は[取得と更新](split-integrations.md#取得と更新)に従います。CPU/GPUはジョブに固定した同じ環境版を使います。
HTTPの入口は更新処理より先に起動し、`GET /split/startup` で起動段階を確認できます。
準備中はトップページに起動画面を表示します。`/split/startup`以外のAPIは最大20秒待ち、準備が終わらなければ `503` と `Retry-After: 2` を返します。
表示・更新確認だけでGPUは起動しません。保存領域の整理は起動完了後のアイドル処理で行います。

## 保存先

- モデルは`comfy-model`、入力は`comfy-inputs`、出力は`comfy-outputs`を利用。通常構成と同じVolumeを共有する。
- ユーザーデータは初回に `comfy-user-data` から `comfy-split-data/user/` へ複製。
  SQLiteファイルは複製せず、以後の設定・ワークフローはsplit専用の保存先を使う。
- 初期環境の作成時に、イメージ同梱ノードと`comfy-custom-nodes`のノードを環境Volumeへコピーし、依存を復元する。コピー元は変更しない。
- `comfy-split-environments`: ノード、仮想環境、検証したノード定義。
- `comfy-split-data/state/`: CPUが書くジョブ受付・状態・環境選択。
- `comfy-split-data/jobs/`: GPUが書くジョブごとの実行結果。

Split専用Volumeは環境用とデータ用の2つ。初期環境、有効環境、候補環境、未完了処理が参照する環境、CPU Memory Snapshotが参照する環境を残す。それ以外の環境は清掃対象であり、更新前の環境を無期限には保管しない。
CPU Snapshotの参照は`comfy-split-environments/.cpu-snapshots/`に記録する。対応するデプロイへ戻す可能性がなく、そのSnapshotを復元しないことを確認するまで保護を解除しない。

完了時刻を記録した終了済みジョブの詳細履歴は7日後に清掃対象となる。結果不明のジョブは期限では削除しない。再実行を防ぐ最小限の記録は残す。
`.split-temp`内のファイルは更新から24時間以上経過し、履歴や処理から参照されていなければ清掃対象となる。通常の生成物・入力・保存済みワークフローは自動削除しない。
清掃は分離モードのアイドル時に、GPUコンテナと待機呼び出しがともに0であることを確認して行う。停止中に清掃のためのCPU/GPU起動は行わない。[保存構成の設計・変更記録](design/split-storage.md)。

GPUは入力のreload後に実行し、出力のcommit後に結果を保存する。
CPUは出力をreloadしてから完了を通知する。稼働中のSQLiteを共有しない。
`execution_success`・エラー通知も、出力の公開と履歴の保存が済むまで保留する。
固定版フロントエンドのTotal表示は `executed` で完了数を数えるため、
`progress_state` で完了した出力なしのノードにも空のUI結果を1回通知する。
実際の画像・動画を含むUI結果は出力の公開後に通知する。
待機・実行中のジョブと履歴には受付日時を付け、保存済みの失敗履歴に不足する項目も補う。
これにより、失敗ジョブ1件によってMedia Assetsの一覧全体が読めなくなるのを防ぐ。
一時出力はコンテナのローカル作業領域で生成し、GPU workerが出力Volumeの `.split-temp/<起動ID>/` へコピーする。
履歴と完了イベントの参照を永続出力へ変換してから公開する。
`.split-temp/temp`に保存された一時出力も`type=temp`の参照で閲覧できる。

## 操作

標準サイドバーの「GPU」タブで稼働状態を確認できる。モード変更の操作は折りたたまず表示する。
標準ツールバーの`GPU(0)`ボタンからも、このサイドバーを開閉できる。稼働中は赤、取得失敗時は`GPU(?)`と表示する。
パネルにはGPUの状態と稼働コンテナ数を表示する。CPU側がModalの管理API
`gpu_worker.get_current_stats()` を取得し、5秒間キャッシュする。
画面も5秒ごとに `/modal-control/v1/status` を読み、生成キュー・モードと組み合わせて
停止中、起動待ち、使用中、停止待ち、常駐中、環境検証中を表示する。
取得失敗時は停止と推測せず「確認できません」にする。
表示のためにGPUワーカーを実行することはない。パネルに最終確認時刻と説明を表示する。キャンバス上の固定ボタン・独自ダイアログは設置しない。
UIは独立した [ComfyUI-Modal-Control](../extensions/ComfyUI-Modal-Control/README.md) に置く。
標準の `WEB_DIRECTORY` によって配信し、ゲートウェイによる `/split.js` の追加配信は行わない。
ComfyUI本体を変更せず、追加のcustom_nodes検索パスから読み込む。

- **分離モード（`split`）**: CPUにキューを保存し、1件ずつGPUで実行する。
  GPU呼び出しは生成・保存の完了まで継続し、終了後30秒で停止対象となる。
- **常時起動モード（`legacy`）**: 「GPUを常時起動にする」で切り替え、同じGPUワーカーでComfyUI全体を実行する。
  GPU待機料金が発生する。「生成時だけ起動に戻す」で分離モードへ戻る。画面からの切替時はワークフローを自動保存し、切替後に復元する。保存できなければ切替を中止する。
  GPUトンネルはセッション固有のBearer認証を持ち、ブラウザには鍵を渡さない。
  CPUからのheartbeatが90秒途絶えた場合は新規投入を停止し、キュー完了後に終了する。
- **Manager**: ノードの追加・更新は分離モードで候補環境に対して行う。インストール完了後にManagerの再起動、または「ノード更新を反映」を実行すると、依存確認、CPU起動、GPUでのノード読み込み検証を行い、有効環境を切り替える。
  GPU検証はモデル生成を行わない。失敗時は更新前の有効環境を維持する。「未反映の変更を取り消す」で候補を破棄すると生成を再開できる。

生成・待機・結果不明のジョブがある場合はモード切替とノード更新を拒否する。候補環境が残っている間は生成を受け付けない。
ManagerからのComfyUI本体更新は提供せず、固定バージョンを再デプロイで更新する。
主要CUDAパッケージ・フロントエンド・Managerの制約を依存インストールにも適用する。

## APIと復旧

ComfyUIの `/prompt`, `/queue`, `/history`, `/ws` と `/api/` プレフィックスを扱う。
この節のキュー・履歴・キャンセル・重複受付防止は分離モードの動作。常時起動モードでは、これらのリクエストをGPU上のComfyUIへ中継する。
ジョブ一覧APIは外部キューのスナップショットを標準拡張へ渡して整形する。
ComfyUI内部のキュー・履歴メソッドは差し替えない。

Modal 1.1.4の標準Webサーバー中継は、ASGIのデコード済みパスを転送するため、
`/userdata/workflows%2Fname.json` の保存・読み込みに失敗する。
CPU入口は認証付きASGIアプリとし、`comfy_split/modal_proxy.py` でファイルパスを再エンコードして
固定版ModalのHTTP/WebSocket中継へ渡す。`raw_path` が提供される環境ではそれを優先する。
SDK更新時は `tests/split/test_proxy.py` と
公開URL経由のワークフロー保存・読み込みを確認する。

追加API:

- `GET /split/startup`: 起動段階、準備完了・失敗、CPU Snapshotの復元情報。
- `GET /split/status`: モード、環境更新、結果不明ジョブ。
- `GET /modal-control/v1/status`: 同じ状態を返すバージョン付きAPI。`api_version: 1`。
- `POST /split/mode`: `{"mode":"split"}` または `{"mode":"legacy"}`。
- `POST /split/environment/apply`: 候補環境を作成・検証して反映。
- `POST /split/environment/discard`: 未反映の候補を破棄。
- `POST /split/environment/repair-image-browsing`: Image Browsingをイメージ同梱版へ戻す候補を作成・検証して反映。
- `/prompt` の `Idempotency-Key` ヘッダー: 同一キー・同一内容の再送には同じジョブIDを返す。同じキーで内容が違う場合は409で拒否する。
- `POST /jobs/<id>/cancel`: 指定ジョブの待機キャンセル、またはそのGPU workerへの中断指示。
- `POST /jobs/cancel`: `{"job_ids":["<id>"]}`で複数ジョブをキャンセル。

外部クライアントはCPUの `ui` URLを接続先にする。分離モードではモデル確認、WebSocket接続、
結果取得はCPU側で処理し、生成ワークフローをこのキューへ投入する。
通常のComfyUI APIへのリクエストに`X-Modal-Execution-Mode: split`を付けると、常時起動モードでは409を返し、GPUへ転送しない。状態取得やモード切替などの管理APIは、このヘッダーによる拒否の対象外。
通常のComfyUI画面はこのヘッダーを送らず、両モードを切り替えて使える。

`/modal-control/v1/status` の `dependencies` は実行中のCPU ComfyUI環境の
comfy-kitchen版、固定版、必要APIの不足を返す。GPUカーネルの動作検証とは区別する。
comfy-kitchenは上流ComfyUIの指定版を固定依存に含め、イメージ構築時と仮想環境の
適用時に検査する。保存済み仮想環境が固定版と異なるパッケージを優先している場合はCPU側のレポートに反映する。Managerで候補環境の依存を修正し、「ノード更新を反映」で検証・採用する。環境検証だけで依存を自動修復する機能ではない。

GPU呼び出し前にdispatch intentを保存し、呼び出しIDを取得後に保存する。
CPUが間で停止してIDを記録できなかった場合は `unknown` とし、結果記録を待つ。
結果不明のジョブは自動再実行せず、後続投入の実行も停止する。
管理者はModalのGPU呼び出しと`comfy-split-data/jobs/`を確認してから復旧する。
ネットワークエラーだけを根拠に再投入しない。
GPU関数の実行タイムアウトは失敗として確定し、結果待ちのポーリングタイムアウトと区別する。

GPU側も実行前に開始記録をcommitする。同じジョブIDでGPU関数が再度呼ばれた場合、保存済みの結果があればそれを返す。開始記録しか残っていない場合は `unknown` とし、生成を再実行しない。

CPUを再デプロイする際は、デプロイ前のCPUが停止してから新しいCPUを利用する。
状態Volumeへの複数デプロイからの同時書き込みはサポートしない。
`comfyapp.py`の通常Appを別途起動した場合、そのGPUはsplitの最大1台という制限や台数表示に含まれない。

## 互換性と計測

CPUでimportできるカスタムAPIはCPU側で実行する。GPU専用ノードは検証時の
ノード定義とJSを配信し、モデルリストと一致する選択肢はCPU側で更新する。
独自の動的APIなど、分離で再現できない機能には常時起動モードを使う。
任意のカスタムノードの動作を保証するものではない。

構造化ログの `comfy_ready`, `gpu_ready`, `gpu_finished` で起動と総処理時間を確認できる。
モデルロードの詳細時間はComfyUIログ、停止はModal Containers画面で確認する。
GPU Memory Snapshotsは未使用。

ローカルの回帰検証:

```bash
uv sync --locked --extra split-test
uv run --locked --extra split-test python scripts/test_standalone.py --suite split
```

全体テストと任意拡張との連携検証は[テストの説明](../tests/README.md)を参照。任意拡張の実パッケージとの連携テストは各拡張repoで実行する。

実機ではGPUゼロのまま10分間の編集・保存・閲覧、生成とプレビュー、
停止待ち設定30秒でのGPU停止、連続生成、再接続、Manager更新と失敗時復帰、モード往復を確認する。
ローカルのプロトコルテストだけで実機の受け入れ完了とはしない。

`scripts/verify_split_execution.py` は検証用CPUコンテナ内で実行するAPIスモークテスト。
2件の投入と待機キャンセル、画像保存・表示、WebSocketイベント、モード往復、GPU停止を確認する。
EmptyImageを使うため、モデル推論やサンプリング中のプレビューの検証には含めない。

`scripts/verify_split_execution.py`、`scripts/verify_split_idle.py`、`scripts/verify_image_browsing.py`の接続先はコンテナ内の`http://127.0.0.1:8000`で、`SPLIT_URL`は使わない。

`scripts/verify_split_idle.py` は同じCPUコンテナ内で10分間実行する。
WebSocketを保持し、画面配信・メタデータ取得・画像アップロード・表示・
ワークフロー保存と読み取りを繰り返し、各回でGPU台数が0であることを確認する。
検証ファイルは `split-verification` 名で入力・ユーザーデータへ保存する。
このAPIテストとは別に、Access経由で標準フロントエンドを操作する受け入れ確認が必要。

### 保存済み環境のImage Browsing修復

イメージ更新だけではVolume上の保存済みノードは更新されない。
認証済みのsplit CPUエンドポイントへ `POST /split/environment/repair-image-browsing` を送ると、
アクティブ環境を複製し、Image Browsingだけをイメージ同梱版に戻した候補を作成する。
他の追加済みノードも候補へ複製し、依存・CPU・GPUでの読み込み検証を通過後に有効化する。
生成中・キュー待ち・別の候補環境がある場合は409で拒否する。
GPU検証には一時的なGPU起動を伴う。結果は `/split/status` の `candidate` と
`environment` で確認できる。失敗時は更新前の有効環境を維持し、候補を取り消してから生成を再開する。成功後の更新前環境は、上記の清掃ルールに従う。

`scripts/verify_image_browsing.py` は修復後のCPUコンテナ内で実行するAPI検証。
GPUが0台に戻った後、Web拡張配信、一覧、アップロード、プレビュー、名前変更、削除、
MiniMax生成結果の参照を確認する。このスクリプトは出力Volumeの`split-verification/`に`minimax_real_`で始まる画像があることを前提としている。該当画像がない環境では、この確認は失敗する。削除するのはスクリプト自身が作成した検証ファイルだけ。
実ブラウザでの画面操作はこのAPI検証とは別に確認が必要。

## ComfyUI本体との接続

ComfyUIは`python main.py`で起動する。キュー読み取り・履歴・生成スレッド・一時ファイル削除の処理は上書きしない。分離モードの生成受付とGPUへの投入はGatewayが担当する。

[ComfyUI-Modal-Bridge](../extensions/ComfyUI-Modal-Bridge/README.md)が、CPUへの直接投入を拒否する`PromptQueue.put`のガードと、ノード定義・ジョブ整形・CPU復元用の内部APIを提供する。これらの内部APIは公開Gatewayからアクセスできない。この拡張はsplitに必須で、任意のAgentBridge中継とは別物。

Managerのインストール処理は候補環境へ中継する。`comfyapp.py`のWebSocket圧縮・user_managerソースパッチはsplitには適用しない。
この境界を変更する際は、`scripts/verify_comfy_bridge.py`によるCPUガード・ジョブAPIの確認に加え、実モデルでの生成とプレビュー、通常出力・一時出力の保存、CPU再起動後の復元、重複実行の防止を確認する。
