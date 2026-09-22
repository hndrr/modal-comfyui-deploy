# ComfyUI CPU Memory Snapshotsの検証

2026-09-20、運用サーバーとは別のModalアプリで検証した。**GeminiToolsを含むComfyUIの初期化済み状態を保存し、完全停止後の別コンテナへ復元できた。** 下記は独立した検証の記録。後続の本体実装は次節に記載する。

## 本体への組み込み

`splitapp.py`のCPU専用イメージで`enable_memory_snapshot=True`を指定する。モジュールの読み込み時に`comfy_split.cpu_snapshot.prepare()`がComfyUIだけを起動し、従来の`ui()` ASGIファクトリーから復元後の処理を開始する。関数名・公開URL・Proxy認証・自動停止の設定を維持し、GPUワーカーのイメージでは事前起動しない。

- GatewayのController、Journal、Bridge、WebSocket、ジョブdispatcherはSnapshotに入れない。復元後に保存データを読み直して作る。Modalが更新した接続情報も親から子へ渡し、古い接続情報を再利用しない。一時ファイルは所有者のみ読める状態で渡し、読んだ直後に削除する。状態APIやログには含めない。
- ComfyUIが起動時に開くuserログ等は一時領域へ置く。復元後にuserディレクトリの参照先を共有Volumeへ切り替え、設定・ワークフローへのアクセスを再開する。
- 読み込み済みの拡張ライブラリが環境Volumeを開いた状態では`reload`が拒否されるため、同じ不変の環境は再読込しない。有効な環境版が変わっている場合やManagerの候補環境がある場合はComfyUIを停止し、環境Volumeを再読込して最新環境で起動する。
- 復元後にCPUガードと空のキューを確認し、モデル一覧のキャッシュ・乱数・一時出力の識別子を更新する。健康確認に失敗したComfyUIは通常起動へ切り替える。
- `/.cpu-snapshots/<deployment>-<environment>.json`を環境Volumeに保存し、Snapshotが参照する環境を期限切れ清掃から保護する。ジョブ履歴とは別管理。
- `/split/startup`の`snapshot`には保存時の初期化IDと時間、今回の復元IDとコンテナID、ComfyUIを再利用できたかを返す。`initialization_seconds`は今回の待ち時間ではない。

Managerで環境を変更した後も正しい環境で実行するが、古いSnapshotしかない実行先では通常起動になる。新しい環境の起動時間も短縮するには`splitapp.py`を再デプロイし、Snapshotを更新する。環境Volumeの変更だけでModalのSnapshotを作り直すことはできない。古い環境の保護は、そのデプロイへのロールバックも不要になり、対応するSnapshotを復元しないことを確認してから解除する。

読み取り専用の実機確認は次のコマンドで行う。`<profile>`は検証対象のModalプロファイル名に置き換える。起動間にはCPUが0台になるまで待ち、生成を投入せずにAPI・WebSocket・ノード定義・内部ルートの遮断を確認する。

```sh
MODAL_PROFILE="<profile>" .venv/bin/python scripts/verify_cpu_snapshot.py \
  --output /private/tmp/cpu-snapshot-boots.json --boots 2
```

Studioからの実GPU生成を含む反映記録は`ambient/docs/validation/2026-09-20/cpu-memory-snapshot-deploy/`に保存する。

### 本体での確認結果

2026-09-20の実機確認では、Snapshotの新規作成を伴う起動は54.16〜104.30秒、保存済みSnapshotから準備完了までは**12.24秒**だった。共有Volumeの再読込・Gatewayの復旧まで含むため、前段の隔離試験の4〜5秒とは計測条件が異なる。全API確認が終わるまでの時間とは区別する。

StudioからCodex OFF、H3 Fused + Mystic 4step、9:16で実生成した。生成・保存・Studioへの配信・Jevタグ付けが完了し、共有ライブラリは13本から14本になった。動画は576×1024、124フレーム、24fps、約5.2秒、AAC音声付き。モデル処理を含むGPUワーカーの所要時間は64.36秒だった。

生成前に作成したSnapshotを、CPUが0台になった後の別コンテナへ復元した。初期化IDは同一、復元IDとコンテナIDは異なる。生成後の成功履歴を取得でき、新しく増えた素材の選択肢も反映されていた。ノード1352件・拡張161件、WebSocket、内部復元ルートが公開されないことも確認した。

20:12:09 JST、最終反映の確認後にCPU・GPU・Ambient API・生成・タグ付け・清掃がすべて0台、待機ジョブ0であることを管理APIから確認した。

## 結果

リクエスト送信から、ComfyUIのAPI・WebSocketの確認結果が返るまでの実測値。

| 条件 | 所要時間 |
| --- | ---: |
| Snapshotなし・1回目 | 187.48秒 |
| Snapshotなし・2回目 | 47.32秒 |
| Snapshotの新規作成を伴う起動 | 76.24秒、47.60秒、61.03秒 |
| 保存済みSnapshotからの復元・1回目 | **5.39秒** |
| 保存済みSnapshotからの復元・2回目 | **3.89秒** |

通常起動のComfyUI初期化部分は181.78秒／40.80秒だった。Volumeのファイル読み込み等によるばらつきがあるため、この少数の測定から平均や保証値は示さない。

各起動の間にrunners・backlogが0になるまで待ち、全7回が異なるコンテナであることを確認した。Snapshotの復元2回では、以前の初期化IDが新しいコンテナIDへ引き継がれ、復元後のIDだけが新しくなった。Modalのログにも新規作成を伴わない`Restoring Function from memory snapshot.`が記録された。稼働中コンテナの再利用による高速化ではない。

Modalは実行先のワーカーの種類ごとにSnapshotを作成するため、初回の数回は新規作成が発生し得る。この試験でも3回の新規作成を観測した。コード・設定の再デプロイ時にも再作成が必要になる。[Modalの仕様](https://modal.com/docs/guide/memory-snapshots)

## 動作確認

全7回で、以下の確認が成功した。

- `/object_info`の1352ノードが揃い、すべての入出力定義のSHA-256が一致した。
- GeminiToolsの21ノード、H3／FastH3、Jev、AgentRuntime Bridgeを含む現行Ambientワークフローの使用クラスが存在した。
- カスタムノードのimport失敗がなく、拡張JSは161件だった。
- ComfyUIのHTMLとメタデータAPIが応答した。
- 復元後に作った新しいクライアントIDでWebSocketへ接続し、そのIDに対応するstatusイベントを受信した。
- CPUでの生成禁止ガードが有効で、実行キュー・待機キューは空だった。

ブラウザの画面操作、H3の実GPU生成、外部プロバイダー呼び出し、Codex Bridgeの実行は対象外。これはCPU版ComfyUIの起動と復元の検証であり、GPUモデルの読み込み時間を測ったものではない。

## 構成

検証コードは[scripts/probe_cpu_snapshot.py](../scripts/probe_cpu_snapshot.py)。

- 専用アプリ：`comfyui-cpu-snapshot-probe`
- 元イメージ：`im-BUQdzUJzuhhjHZzFg6a8yl`
- 固定環境：`env-cd487277be974529ac2a0969e52cedac`
- CPU 2、メモリ8192 MiB、GPUなし、min_containers 0、max_containers 1。
- 既存の環境Volumeを読み取り専用でマウント。モデル・入力・出力・ジョブ状態の共有Volumeはマウントしていない。
- user・DB・出力は一時コンテナ内。外向き通信は遮断、APIキー等のSecretは未指定、公開HTTPエンドポイントなし。
- `ComfyProcess.start(..., cpu=True)`で、運用時と同じComfyUI子プロセス・venv・ノード群を起動した。
- `@modal.enter(snap=True)`で起動完了を待ち、ComfyUIを稼働させたまま保存。`snap=False`で復元後の識別子を作成した。
- Gateway／Controller／ジョブdispatcherは起動していないため、保存された古いジョブを再実行する経路がない。

通信遮断に伴うManager/OpenRouterのカタログ取得失敗は想定内。ノードの読み込みとメタデータ応答は成功した。ネットワーク接続・共有モデル一覧等の条件が運用と異なるため、運用画面が必ず同じ秒数で開くことを保証する結果ではない。

## 再現方法

```sh
scripts/modal.sh deploy scripts/probe_cpu_snapshot.py
MODAL_PROFILE="<profile>" .venv/bin/python scripts/probe_cpu_snapshot.py \
  --output /private/tmp/comfy-cpu-snapshot-results
scripts/modal.sh app stop comfyui-cpu-snapshot-probe
```

Snapshotはデプロイしたアプリで作成されるため、`modal run`による一時アプリで代用しない。環境IDとイメージIDは今回の測定対象に固定してある。再利用時は実在する組み合わせに更新して再デプロイする。試験途中で失敗・中断した場合も、最後の専用アプリ停止を行う。

`boots.json`の`comfyInitSeconds`・`totalInitSeconds`は**保存時の初期化時間**であり、復元時にその初期化を再実行した時間ではない。実際の待ち時間は`clientSeconds`。

## 独立検証時点で挙げた組み込み要件

1. ComfyUIの初期化とGatewayの状態読み込み・処理開始を分ける。復元後に最新のジョブ・ワークフロー・接続状態を読み直す。
2. Snapshotの環境版と有効なノード・venvの版を一致させる。Volumeの変更だけではSnapshotが更新されないため、Managerからの環境適用でも作り直す仕組みが必要。
3. Snapshotが参照する環境を期限切れ清掃から保護し、旧Snapshotの停止後に保護を解除する。
4. 既存の起動ページ、認証、CPUの自動停止、受付済みジョブの回復を含めて検証する。

## 記録と終了状態

測定記録は`ambient/docs/validation/2026-09-20/cpu-memory-snapshot/`に保存した。

- `boots.json`：全起動の時間、コンテナID、初期化ID、定義のハッシュ。
- `app.log`：Snapshot作成・復元のログ。
- `run.log`：各起動前後のCPU停止確認。
- `all-idle.json`：専用アプリ停止後の最終確認。

19:35:05 JSTに検証用アプリの停止が完了した。19:37:42 JST、管理APIから運用側の全6関数でrunners・backlogが0であることを確認した。検証アプリもstopped・Tasks 0で、コンテナ一覧は空だった。既存のStudio／Split／Ambientのデプロイ、GeminiToolsの有効状態は変更していない。
