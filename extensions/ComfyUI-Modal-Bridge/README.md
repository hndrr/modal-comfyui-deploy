# ComfyUI-Modal-Bridge

標準のcustom-nodeローダーで読み込む、Modal分離実行用のバックエンド拡張。
推論ノードやフロントエンドを追加しない。`SPLIT_INTEGRATION=1` のときだけ有効で、
通常のComfyUIへ置いただけではルート登録・差し替えを行わない。

- `/_split/catalog`: ノードと拡張の読み込み状況を取得する標準拡張ルート。
- `/_split/jobs`: 外部キューのスナップショットをComfyUIのジョブ形式に変換する。
  ComfyUIのキューは参照・変更しない。`comfy_execution.jobs` の読み取り用整形関数に依存する。
  上流がこの関数を変更した場合、対応箇所は `jobs.py` に限定される。
- `cpu_guard.py`: `SPLIT_CPU=1` のプロセスだけ、`PromptQueue.put` を拒否する。
  カスタムノードがHTTPを経由せず直接投入するケースを防ぐために残した唯一の実行時上書き。
  キューの読み取り・履歴・起動スレッド・一時ファイル削除は変更しない。

ルートはComfyUIの `PromptServer.instance.routes` で登録する。
公開ゲートウェイは `/_split/*` を外部へ公開しない。
CPU監視側は起動時に拡張とガードの有効性を検査し、ガードなしの状態を正常起動としない。

標準起動方法は `python main.py`。この拡張を外せば通常のComfyUIとして動作する。
Modal分離構成のCPUとして運用する場合は、この拡張を必須とする。
