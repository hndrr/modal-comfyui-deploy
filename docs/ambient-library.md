# Ambientの共有ライブラリとワークフロー編集

Ambient Studioは、サーバーに保存した動画を再生しながら新作を生成できます。
20本程度を準備の目安としますが、1本から再生できます。再生速度は常に等速です。
新作は読み込みが終わった後の切り替えで優先し、映像と音を約1秒でクロスフェードします。
既存動画は現在の雰囲気と直前の動画への近さを同じ比重で評価し、重み付きランダムで選びます。
直近最大5本を避けますが、候補が最低1本残るよう履歴を縮めます。
再生候補は動画全体の取得後に使い、通信が途切れたときは取得済みの動画を続けます。
再生中・次の候補・新作待ちの動画だけをブラウザに保持し、ライブラリ全体は読み込みません。

## ライブラリAPI

| API | 動作 |
| --- | --- |
| `GET /library` | 動画・タグ処理状態・本数・合計バイト数 |
| `POST /library?name=...` | 動画本体をraw bodyで登録。上限128 MiB |
| `GET /library/:id/video` | Range対応の動画配信 |
| `DELETE /library/:id` | 動画削除 |
| `GET /workflows` | 共有Splitゲートウェイのワークフロー一覧と現在の版 |

登録時はCPUのffmpegでH.264/AAC MP4へ変換します。無音の持ち込み動画も受け付けます。
動画は出力Volumeの `ambient/library/`、メタデータは `comfyui-ambient-library` Dictに保存します。
生成ジョブの期限切れ清掃では消えません。動画は手動で削除します。

`POST /jobs` に任意の `saveToLibrary`、`sessionId`、`workflowRevision` を追加しました。
既存CLIの要求はそのまま使えます。Studioは保存を有効にし、生成サイクルの開始時に
ワークフローの版を固定します。自動生成OFFは次の投入を止め、受付済みの処理は完了させます。
再生停止やブラウザ終了では受付済みジョブを取り消しません。取消は別操作です。

## Jevタグ付け

動画の保存後、独立したComfyUIジョブでJevを実行します。入力は生成に実際に渡した設定です。
ComfyUIで固定した値も反映します。シーン・音のタグ、動き・暖かさ・夢幻的な度合い、
スキーマのハッシュを保存します。Jevの失敗や遅延で動画の完了・割り込みは取り消しません。
手持ち動画などタグがないものは中立の候補です。

初期接続先はTypeSafeで、ComfyUIからOpenRouterへ変更できます。通常のAmbientノード導入と
対応するModal Secretが必要です。キーをワークフローに直接保存する操作は拒否します。
タグジョブは既存のSplit実行経路を使い、GPUワーカーのキューを共有します。
この変更では実際の所要時間・費用を測定していません。

## ComfyUIでの表示と編集

ComfyUIの **Ambient** サイドバーでセッションと処理段階を選びます。
追従表示では実際に送信されたグラフ・入力値・進捗・結果を表示します。
保存済みの配置を重ねますが、古いウィジェット値を実行値として表示しません。

「編集用に開く」でドラフトを作り、「Ambientに適用」で次回用の版を保存します。
各入力は「Ambientから入力／ワークフローの値」を選べます。固定した入力はStudioで無効表示になります。
ノードを置き換えた場合、詳細欄で入出力の接続先を変更してください。
出力の種類、必須入力の接続、Bridgeのsandbox・CLI制約は適用前に検査します。
固定した参照素材は `ambient/workflow-assets/` にコピーし、一時ファイル清掃から保護します。

別端末の更新と競合すると409を返し、ドラフトを保持します。
保存版を編集用に復元して適用すると新しい版になります。
H3・FastH3・Jev・Bridgeの初期テンプレートは必要なノードがあれば生成前に表示できます。
Bridgeの初期スナップショットは `ambient/bridge_templates.json` にあります。
Studioの `server/agent-workflows.mjs` の固定グラフを変更する場合は、このスナップショットも更新します。
表示・編集・適用だけでは生成を投入しません。

Split側のAPIは `/ambient/workflows`、`/ambient/workflows/:revision`、
POST `/ambient/workflows/validate`、POST `/ambient/workflows/apply`、
`/ambient/executions[/:prompt_id]` です。
適用には `stage`、`template`、`expectedRevision` を渡します。
ワークフローの版は既存の単一書き込み元のSplit journalに保存します。
`ambient_execution` WebSocketイベントにセッションと処理段階を含めます。
履歴の保持期間中は、実行スナップショットから途中参加・再接続できます。

## 導入と検証

Ambient API、Splitゲートウェイ、Ambient Studioを組み合わせて更新してください。
Studioの `COMFY_URL` とバックエンドの `AMBIENT_COMFYUI_URL` は、同じSplitゲートウェイを指定します。
これによりBridgeと動画生成で同じワークフロー版を使用します。
Studioは停止中のワークフロー定期取得を行わず、画面へ戻ったときに再取得します。
「Connect Mac」で作るBridge接続はStudioサーバーが保持するため、タブを閉じても残ります。
使い終わったらCodexパネルの「Disconnect」で切断してください。ComfyUIのタブも閉じ、
生成・環境更新・ほかの接続がなければ、SplitのCPU UIは30秒のアイドル待機後に停止対象になります。
GPUの停止条件はCPU UIと別です。UIが稼働していても、生成がなければGPUは停止できます。
テストはローカル素材と模擬APIを使います。実GPU生成・外部Jev呼び出し・デプロイ・
既存動画の一括移行はテストで実行しません。

バックエンドの検証は `.venv/bin/python -m unittest discover -s tests`、
ComfyUIパネルの模擬検証は `node --test tests/test_ambient_panel.mjs` で実行します。
Studio側では `npm run test:ambient`、`npm run typecheck`、`npm run lint`、
`npm run test:ambient:browser` を使います。パネルの実ComfyUI上での表示は別途確認してください。
