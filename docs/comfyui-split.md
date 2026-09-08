# GPUを生成時だけ使うComfyUI

`splitapp.py` は既存の `comfyapp.py` と別の `comfyui-split` App を作る。
CPUサーバーは最小1台、GPUワーカーは最小0台・最大1台。
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
一時出力は出力Volumeの `.split-temp/temp` に保持される。

## 操作

画面右下の「実行環境」からモードとノード更新状態を操作できる。

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
新しいジョブ一覧APIはCPU ComfyUIのキュー読み取りを永続状態へ差し替えて使用する。

追加API:

- `GET /split/status`: モード、環境更新、結果不明ジョブ。
- `POST /split/mode`: `{"mode":"split"}` または `{"mode":"legacy"}`。
- `POST /split/environment/apply`: 候補環境を作成・検証して反映。
- `POST /split/environment/discard`: 未反映の候補を破棄。
- `/prompt` の `Idempotency-Key` ヘッダー: 同一キー・同一内容の再送を重複受付しない。

GPU呼び出し前にdispatch intentを保存し、呼び出しIDを取得後に保存する。
CPUが間で停止してIDを記録できなかった場合は `unknown` とし、結果記録を待つ。
結果不明のジョブは自動再実行せず、後続投入の実行も停止する。
管理者はModalのGPU呼び出しとresults Volumeを確認してから復旧する。
ネットワークエラーだけを根拠に再投入しない。

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
