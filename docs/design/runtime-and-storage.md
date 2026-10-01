# Splitの実行環境とStorageの検討

> 2026-09-15〜16の設計・実施記録です。現在の構成・運用は[splitの説明](../comfyui-split.md)と[任意拡張](../split-integrations.md)を参照してください。

確認日: 2026-09-16

現在の実装と、実行環境をイメージにまとめる場合の違いを整理する。
構成変更を決定・実施したものではない。

## 現在の保存先

SplitはModalのコンテナで動いている。
Dockerfileの代わりに、Pythonの`modal.Image`で依存関係やイメージを定義している。
検討対象は、Splitのカスタムノードと追加Python環境をVolumeに保持する必要があるか、という点。

| 対象 | 現在の配置・実行方法 |
| --- | --- |
| ComfyUI本体・基本ライブラリ | Splitのイメージ内。本体の更新は再ビルド・再デプロイで反映する |
| SplitのCPU UIとGPU worker | 同じイメージを使い、別々のコンテナで実行する |
| Splitの通常のカスタムノード | `comfy-split-environments`内の`/environments/<環境ID>/comfy/custom_nodes`から実行する |
| Splitの追加Python環境 | 同Volumeの`/environments/<環境ID>/venv`。イメージ内の基本ライブラリも参照する |
| SplitのModal連携用拡張 | イメージ内の`/opt/comfy-extensions`に配置する |
| モデル・入力・生成物・設定・ワークフロー・必要なジョブ状態 | VolumeやModal Dictなどの永続ストレージに保持する |

## なぜSplitの環境をVolumeに置いているか

Managerで追加・更新したノードと依存パッケージを、再デプロイせずに保持し、CPUとGPUで同じ環境を使うため。
更新時は環境をコピーして候補を作り、検証後に使用する環境を切り替える。
Manager自体が問題というより、実行中に変更できる環境を永続化する設計が、環境の同期・切替・清掃を必要にしている。

**Volumeにあるのは、Managerで後から追加したノードだけではない。**
初期化時にイメージの`/opt/comfy-template/custom_nodes`もVolumeへコピーし、以後はそのコピーを使う。
既存の`/data/custom_nodes`があれば、そこからも取り込む。
Python環境は`venv --system-site-packages`で作るため、基本ライブラリすべてをVolumeに複製する構成ではない。

初期化済みの環境は、イメージを更新しても自動では作り直されない。
そのため、新しいComfyUI本体・基本ライブラリと、Volumeに残った既存ノード・追加依存関係の組み合わせになり得る。
本体更新時には、この組み合わせの互換性を確認する必要がある。

## 実行環境をイメージにまとめる場合

ノード追加・更新のたびに再ビルド・再デプロイする運用を受け入れるなら、次の構成にまとめる方が管理は単純になる。

| 対象 | 保存先 |
| --- | --- |
| ComfyUI本体・カスタムノード・Python依存関係 | コンテナイメージ |
| モデル | Volume |
| 入力・生成物・設定・保存済みワークフロー・必要なジョブ状態 | 永続ストレージ |

この方式なら、Split専用の環境Volumeと、ノード・Python環境のコピー・同期・清掃をなくせる。
本体とノード・依存関係を同じイメージ定義で管理でき、実行環境を再現しやすくなる。
ただし、Managerでインストールした内容がそのまま次回起動にも残る使い方は維持できない。
追加するノード・依存関係をイメージ定義に反映し、再デプロイする運用に変わる。

Dockerfileへの書き換えは必須ではなく、現在の`modal.Image`のままで実現できる。
CPUとGPUの分離や未使用時の停止も、引き続きModal側で設定できる。
モデルやユーザーデータの永続化は別の役割なので、環境Volumeをなくしても必要な保存先は残す。
起動時間や費用の改善幅は、この整理だけでは断定できず、変更後の計測が必要。

## 確認した実装・関連資料

- `splitapp.py`: イメージ定義、同梱ノード、CPU/GPUへの割り当て。
- `comfy_split/runtime.py`: `initialize_environment`、`create_environment`、`ComfyProcess.start`。
- `comfy_split/gateway.py`: Managerの環境候補作成・検証・適用。
- `comfy_split/storage.py`: Volume名・マウント先・環境の保持判定。
- [Split Storage](split-storage.md): 現行のStorage構成と清掃・移行の記録。
- [Modal Images公式ドキュメント](https://modal.com/docs/guide/images): イメージ定義、ローカルコードの配布と`copy=True`の違い。
