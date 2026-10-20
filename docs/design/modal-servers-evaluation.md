# Modal Serversの別アプリ検証

2026-10-20 / 作業ブランチ `codex/modal-servers-evaluation` / 比較元 `9e1d03801a119aee18344d316606acd9590575ac`

## 実モデル推論の追加結果

**MiniMax H3でFunctions・Serversとも3本ずつ動画生成に成功。今回の実装ではServers化による推論高速化・費用削減は確認できず、送信から取得までの時間と費用はFunctionsのほうが小さかった。** UIの起動時間には先行試験で利点があったが、本番移行を判断する材料とは分けて扱う。

両方ともRTX PRO 6000を1台使用し、同じ重み・プロンプト・seed 42/43/44で、576×1024・124フレーム・24 fps・4 stepsの動画と音声を生成した。使用グラフは `benchmarks/minimax_h3_servers.json` に固定した。モデルは本番 `comfy-model` を**読み取り専用で共有**し、入力・生成物・実行状態・環境だけを検証用に分けた。モデルの複製は不要だった。

| 実測項目 | Functions | Servers |
| --- | ---: | ---: |
| 完了した実モデル推論 | 3/3 | 3/3 |
| 空の検証環境からUI準備完了 | 68.020秒 | 86.989秒 |
| seed 42：送信→MP4取得 | 76.964秒 | 99.567秒 |
| seed 43：送信→MP4取得 | 67.041秒 | 91.419秒 |
| seed 44：送信→MP4取得 | 70.664秒 | 96.801秒 |
| GPUワーカー内の処理（各seed順） | 55.679 / 46.025 / 52.215秒 | 53.696 / 44.099 / 45.923秒 |
| ComfyUIグラフ実行（各seed順） | 38.518 / 29.785 / 36.157秒 | 36.261 / 28.129 / 31.530秒 |
| GPUコンテナ起動数 | 1 | 2 |
| 接続終了→全コンテナ0 | 88.636秒 | 56.914秒 |
| 課金額と単価から逆算したGPU割当時間 | 約280秒 | 約308秒 |

GPUワーカー内の処理にはComfyUI起動・モデル読込・生成・結果保存を含む。送信→取得の時間には制御処理、GPU準備待ち、結果転送、日本から米東部への通信も含まれる。各3本の小標本なので、差をServers全般の性能とみなさない。

Functionsは同じGPUコンテナを3本で使用し、Serversは3本目で別コンテナに替わった。ログ上は同じコンテナ内でもComfyUIが再起動し、全件で `execution_cached.nodes=[]` だった。**モデルをロード済みのまま維持した定常推論の比較ではなく、現在の実装での連続生成の比較**である。サンプラーを含む実グラフが6回とも実行された。GPU利用率の収集値は両方ともp90=53%、p99=100%。GPUホストRAMのp90はFunctions 40.410 GiB、Servers 40.593 GiBで、これはVRAMの値ではない。

全6本でH.264・576×1024・124フレーム・24 fps・AAC音声・5.167秒をffprobeで確認した。同seedのMP4はバイト一致せず、デコードした全フレームのSSIMは42/43/44の順に0.983396 / 0.984852 / 0.987254だった。これは画質優劣の評価ではない。seed 42の代表フレームは両方目視確認し、庭・水面・ランタンという同じ構図を確認した。音声トラックの存在は確認したが、聴感の比較は実施していない。

再現データと生成MP4は `docs/validation/2026-10-20/servers-model-inference/` に保存した。このディレクトリは既存の `.gitignore` 対象。参照したモデル4ファイルのサイズ・更新時刻は試験前後で一致し、APIが返す実マウント設定も読み取り専用だった。

### GPU込みの実モデル試験費用

2026-10-20 14:38 JST取得のBilling APIに反映されたアプリ別利用料。CPU・メモリはUIとGPUホストの両方を含み、空の検証環境からの準備、3本の生成、停止待ちを含む。Functionsの初回取得時は0.267324 USDだったが、再取得で下記の額に更新された。

| リソース（USD） | Functions | Servers |
| --- | ---: | ---: |
| RTX PRO 6000 | 0.235866 | 0.259334 |
| CPU | 0.011638 | 0.013108 |
| メモリ | 0.023963 | 0.027763 |
| 外向き通信 | 0.000051 | 0.000201 |
| 3本分の総額 | **0.271519** | **0.300406** |
| 総額÷3本 | **0.090506 / 本** | **0.100135 / 本** |

今回のServers版は約10.6%高かった。両アプリのGPU処理自体は近く、外側の待ち時間とGPU割当時間を含めた結果である。小標本・起動込みの実測であり、定常運転の単価や一般的な削減率ではない。Billingの時間集計には遅延があり、共有イメージ作成・Volumeなど別オブジェクトの料金が含まれない可能性もあるため、確定請求額とは区別する。実モデル試験のアプリ利用料は合計約0.572 USD。

不要だったモデルコピーは中止し、途中のコピーとコピー用スクリプトを削除した。その準備アプリに反映された約0.001767 USDは、上の推論比較に含めていない。

追加試験後のPython全テストは177件成功。`comfyui-servers-eval-20261020-h3-{function,server}` は停止し、専用Volume・Queueを削除した。共有モデルVolumeは削除対象に含めず、本番アプリのデプロイも行っていない。

## 起動・APIのスモーク試験（先行検証）

SDK 1.6.0で既存テストは通り、Functions版・Servers版ともにAPIとGPU起動のスモーク試験を完了した。**ServersはUIの移行候補として有望。ただしGPU費用の削減は確認できておらず、本番移行は今回の対象外**とする。

| 実測項目 | Functions | Servers |
| --- | ---: | ---: |
| 完了した起動・API試験 | 4回 | 4回 |
| 空の環境からの初回起動 | 73.936秒 | 69.713秒 |
| 初期化済み環境での起動（3回） | 55.871 / 68.159 / 69.364秒 | 55.030 / 11.768 / 7.256秒 |
| 上記3回の各 `/queue` 中央値（各10標本） | 213.69 / 275.82 / 285.54 ms | 192.72 / 182.35 / 188.10 ms |
| GPUジョブ送信から画像取得まで（各1回） | 40.821秒 | 34.070秒 |
| GPUワーカー内部の処理時間（起動・保存込み） | 25.717秒 | 25.072秒 |
| 課金額と実単価から逆算したGPU割当時間 | 約110秒 | 約110秒 |
| GPU試験の接続終了から全コンテナ0まで | 90.614秒 | 85.663秒 |

Serversでは初期化ID `a22970325c5a438286ea927dc0a8c41a` が3回共通で、復元IDとコンテナIDは毎回変わった。2・3回目の7〜12秒は既存スナップショットからの復元である。Functionsでは3回とも初期化IDが変わり、ログにもスナップショット作成が毎回出た。ComfyUIプロセスの引き継ぎ自体は成功したが、既存スナップショットの継続的な再利用はこの検証では確認できなかった。スケジューリング等の差を切り分けていないため、数値を一般的な速度保証にはしない。

GPUは双方とも成功1件、失敗0件、起動コンテナ1台。2件目の待機ジョブはキャンセルされ、生成PNGのSHA-256は双方 `44e55242930daa56bfaa80b7dfa376c015766729676cc4859c8b25ae7cfdcf82` で一致した。この先行試験はGPU計算負荷をかける試験ではなく、GPU利用率の収集値も0%である。GPU料金は実際にRTX PRO 6000を割り当てて発生した額を使っている。実モデル推論は追加試験として別に計測した。

### 反映済みの検証費用

2026-10-20 14:11 JST取得。アプリに帰属する、初期化・再起動試験・GPUスモークを合わせた利用料。CPU・メモリ欄はUIとGPUホストの両方を含む。

| リソース（USD） | Functions | Servers |
| --- | ---: | ---: |
| RTX PRO 6000 | 0.092584 | 0.092584 |
| CPU | 0.019743 | 0.011280 |
| メモリ | 0.012906 | 0.007552 |
| 外向き通信 | 0.000164 | 0.000816 |
| APIが返した総額 | **0.125396** | **0.112232** |

合計は約0.238 USD。これは**反映済みのアプリ利用料であり、検証全体の確定請求額ではない**。時間集計の遅延、共有イメージ作成費用、Volume等の別オブジェクトの費用は含まれていない可能性がある。Functions側には最初のソース同梱漏れによる中断分が含まれ、同じ時間枠の初期化・修正費用を厳密には分けられない。総額差をそのまま本番の削減率として扱わない。

この先行試験のGPU費用は同額だった。Serversの利点として観測できたのは、スナップショット再利用とHTTP応答の短縮である。

### 検証と後片付け

- Python全テスト175件、Worker 7件、Worker型検査、Web 36件とビルドが成功。
- 最初のFunctions試験は `splitapp.py` のイメージ同梱漏れで中断。検証アプリを停止して同梱を修正し、再試験を完了。未完了の試行記録はJSONに残した。
- Serversの503起動ページはWorkerのローカル統合テストで確認。Cloudflare経由の公開環境にはデプロイしていない。
- 本番 `comfyui-split` のApp ID・状態・タスク数・作成時刻は検証前後で一致し、デプロイを実行していない。
- 検証App `comfyui-servers-eval-20261020-a-{function,server}` は停止、コンテナ0を最終確認。各アプリの専用Volume 7個・Queue 2個、計18リソースを削除済み。再現用のソース・集計JSON・ログはローカルに保存した。

## 対象と実装

既存の `comfyui-split` と同じ構成を、Functions版とServers版の独立したModal Appへ配置する。UIは2物理CPUコア・8 GiB、生成ワーカーはRTX PRO 6000を1台（CPU・メモリは現行と同じ既定の最小予約と使用量に応じた課金）、各役割の最大コンテナ数は1、アイドル時は0とする。先行試験では `EmptyImage → SaveImage`、追加試験ではMiniMax H3の動画・音声生成を実行する。

本番アプリ、Cloudflare Worker、DNS、Access設定はデプロイしない。実モデル試験では本番のモデルVolumeを読み取り専用で共有する。入力・出力・実行状態・ユーザーデータ・ノード・環境の6種類のVolumeと2種類のQueueは検証アプリごとの専用名を使う。先行スモーク試験のみモデルVolumeも空の専用品を使用した。オプションのノードパックと外部サービス連携は無効にする。

- SDKをローカルとsplitコンテナ内で `modal==1.6.0` に固定。
- `scripts/probe_servers.py` が同じイメージ・GPUワーカー・ゲートウェイをFunctions/Serversの両方に配置する。
- UIのスナップショットは既存と同じ境界で作成。ComfyUIを事前起動し、復元後に認証情報・Volume・永続キュー・Controllerを読み直す。
- `SPLIT_UI_BACKEND=server` の場合に公開SDKの `modal.Server` を使用。生成中のCPU保持には、1.6.0で双方に存在する `update_autoscaler` を使う。
- 単一の永続状態を複数コンテナが書かないように `max_containers=1` と `--strategy recreate` を指定。
- Workerには `MODAL_STARTUP_ORIGINS` に列挙したoriginだけに適用する起動ページを追加。Access認証後のHTML GETが503になった場合だけ表示し、安全なGETで起動状態を確認する。POST・API・WebSocketは自動再送しない。今回はローカルテストのみで、本番Workerの設定は変更しない。

## スモーク試験の計測方法

実測結果は `docs/validation/2026-10-20/servers-evaluation/{function,server}.json` に保存する。このディレクトリはリポジトリ既定の `.gitignore` 対象。認証情報は保存しない。

1. 空の専用ストレージから起動し、ComfyUIの環境を作成する。
2. 同じ検証アプリを再デプロイし、初期化済みComfyUIのスナップショットを作成する。
3. コンテナが0になったことをModal管理APIで確認してから再起動する。初期化IDが同じで復元ID・コンテナIDが変わることを確認する。
4. 未認証401、ノード定義、拡張JS一覧、ワークフロー保存・取得・移動・削除、日本語と記号を含むパス、3,149,631バイトの画像アップロードと同一ハッシュでの取得、WebSocket再接続、内部ルート遮断を確認する。
5. GPU起動を伴うワークフローを1件完了させ、2件目を待機中にキャンセルする。起動中にWebSocketを切断し10秒間リクエストを止め、その後に接続を回復する。
6. HTTPを送らず管理APIでコンテナを観測し、GPUとCPUが停止するまで記録する。設定した30秒のscaledown値を実際の課金時間と同一視しない。
7. Billing APIの実単価・リソース別費用・実行統計を保存し、専用アプリを停止、専用VolumeとQueueを削除する。

APIの応答時間は日本の実行端末から米東部のModal endpointへの往復を含む。小標本なので、一般的な性能差や本番推論の速度差として扱わない。GPU試験はモデル推論のベンチマークではない。

## GPUを含む費用の計算

アカウントの `Workspace.billing.rates()` から2026-10-20に取得した単価を使用する。

| 項目 | 単価（USD） |
| --- | ---: |
| RTX PRO 6000 | 3.03000 / GPU時間 |
| 物理CPUコア | 0.04730 / コア時間 |
| メモリ | 0.00800 / GiB時間 |
| Volume | 0.09000 / GiB月 |
| 外向き通信 | 0.04000 / GiB |

同じ処理の総額は次のように計算する。すべて秒単位の利用量から時間へ換算し、UIとGPU側のCPU・メモリを両方含める。

```text
総額 = 3.03 × GPU割当時間
     + 0.0473 × (UIのCPUコア時間 + GPUホストのCPUコア時間)
     + 0.008 × (UIのGiB時間 + GPUホストのGiB時間)
     + ストレージ + 外向き通信
```

UIの予約分2コア・8 GiBは0.15860 USD/時。GPUを1日8時間・30日、合計240時間割り当てる場合、GPU単体が **727.20 USD/月**、同じ時間だけUIを稼働させる予約分が38.064 USD/月になる。さらにGPUホストのCPU・メモリが加算される。

今回の実モデルでGPUホストのRAMは約40 GiBまで使用したため、以前のGPUホスト8 GiBという仮定は使わない。GPUホストを平均2コア・40 GiB課金と仮定すると、その分が99.504 USD/月、合計は **864.768 USD/月**。これは240時間の割当を前提にした参考値であり、短い試験の使用量の分位点を実運用の平均に換算した値ではない。Functions版とServers版で利用時間・利用量が同じなら同額になる。ストレージ・通信は別途、割引・クレジット・税を引く前の額。

Serversへの変更だけでGPU単価や同じ推論の実行時間が減るとは仮定しない。起動、モデル読み込み、実行、停止待ちの合計GPU割当時間が短くなったときに、その差額を評価する。

Billing APIは時間枠で集計され反映が遅れる。行がまだ返らない場合は「未反映」とし、0円と解釈しない。試作中の失敗やイメージ作成を含む総検証費用と、正常試験の実行時間に基づく比較は区別する。同じ時間枠に含まれる修正費用を請求額から厳密に分離できるとは限らない。

## 再実行

`--run-id` は新しい小文字英数字・ハイフンのIDを使う。`--profile` は利用するModalプロファイル。Proxy Tokenは既存の `.env` または環境変数から読み、CLI引数には渡さない。

```sh
uv sync --extra split-test --inexact
.venv/bin/python scripts/evaluate_servers.py deploy --run-id <id> --backend function --profile <profile> --output /tmp/function.json
.venv/bin/python scripts/evaluate_servers.py check --run-id <id> --backend function --profile <profile> --output /tmp/function.json
# 初期化済みの環境でスナップショットを作成
.venv/bin/python scripts/evaluate_servers.py deploy --run-id <id> --backend function --profile <profile> --output /tmp/function.json
.venv/bin/python scripts/evaluate_servers.py check --run-id <id> --backend function --profile <profile> --output /tmp/function.json --boots 2
.venv/bin/python scripts/evaluate_servers.py check --run-id <id> --backend function --profile <profile> --output /tmp/function.json --gpu
.venv/bin/python scripts/evaluate_servers.py collect --run-id <id> --backend function --profile <profile> --output /tmp/function.json
.venv/bin/python scripts/evaluate_servers.py cleanup --run-id <id> --backend function --profile <profile> --output /tmp/function.json --delete-storage
```

Servers版は `--backend server` と専用の出力ファイルを使い、同じ手順を順番に実行する。同一出力ファイルに対するコマンドを並列実行しない。検証失敗時はランナーが当該アプリを停止するが、診断用ストレージは残す。確認後に `cleanup --delete-storage` を実行する。

実モデル推論では、新しいrun IDを使い、deployに `--shared-models`、checkに `--workflow benchmarks/minimax_h3_servers.json` を付ける。既存モデルのダウンロードやコピーは不要。両backendで同じベンチマークを使い、check後にcollect、cleanupを実行する。共有モデルVolumeは削除対象に入らない。

## 参考

- [Runtime Roundup](https://modal.com/blog/runtime-product-update-sandbox-endpoints)
- [Serversの動作・認証・ゼロからの起動](https://modal.com/docs/guide/servers)
- [公開料金表](https://modal.com/pricing)

VM Sandboxes、Sidecars、Clusters、Sticky Sessions、Endpoint Candidatesは今回導入しない。今回の単一UI・単一GPUワーカー構成に必要な変更をSDKとServers比較に絞った。Endpointsへの本番トラフィックのミラーリングも行っていない。
