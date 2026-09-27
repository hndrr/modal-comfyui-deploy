# Ambient Studioとsplit

生成ジョブ、動画の最終フレーム継承、保存、タグ付けはStudioのCoordinatorが担当します。
Modal側では`splitapp.py`が通常のComfyUIキューとGPU実行を管理し、任意のAmbient拡張がワークフロー編集・実行表示を追加します。
旧`ambient_app.py`、`python -m ambient.cli`、専用FastVideo workerは廃止しました。

## 接続

Modal側で`SPLIT_EXTENSIONS=ambient`を設定し、Studioの`ambient-targets.json`のModal targetにsplitのCPU UI URLを指定します。
追加ノードとMac Bridgeは独立した設定です。[任意拡張](split-integrations.md)を参照してください。
Ambientを使わないsplitには、パッケージ・Secret・専用ノードは不要です。

共有レシピとパネルの正本は、非公開の[ComfyUI-Ambient](https://github.com/hndrr/ComfyUI-Ambient) repoです。
Modalは固定コミットをデプロイ時に取得します。LocalとModalのモデル既定値は別プロファイルで、保存済みグラフ・revisionは維持します。

## モデルの明示的な準備

起動・デプロイ・設定編集ではモデルをダウンロードしません。必要な場合にだけ次を実行します。

```sh
./scripts/modal.sh run scripts/prepare_ambient_h3.py --mode h3
```

準備対象は`h3`、`h3-turbo-4step`、`h3-fused-4step`、`fasth3`、`fasth3-8step-t2v`、`fasth3-8step-i2v`です。
`model_manifests.py`が従来のモデルrevisionとチェックサムを保持します。
ComfyUIの対応ノードは生成先に導入してください。モデルの配置・ノード不足は投入前のワークフロー検証で確認します。

## 保存データと検証

保存済みの動画、タグ、Volume、Journalは削除しません。読み出し経路は[保存データ](ambient-library.md)を参照してください。
ローカル・モック検証の手順は[任意拡張の検証](split-integrations.md#ローカル検証)にあります。
廃止前のGPU測定などは[履歴](design/retired-ambient-backend.md)に保存しています。
