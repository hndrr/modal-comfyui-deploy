# Volume の操作補助スクリプト

## Volume を別名へコピーする

`rename_volume.py` は Modal Volume 間でデータをコピーするユーティリティです。実質的に Volume 名を移行したい時に使います。

```bash
uv run python rename_volume.py <コピー元ボリューム名> <コピー先ボリューム名>
```

確認を省略する場合:

```bash
uv run python rename_volume.py <コピー元> <コピー先> --yes
```

仕様:

- コピー先 Volume は存在しなければ作成
- データコピーは `modal.App(name="volume-copier")` 経由で実行
- コピー後、元 Volume の削除は自動では行わない

## Volume 内のファイルを移動する

`move_volume_file.py` は Modal Volume 内の単一ファイルまたはディレクトリを移動するユーティリティです。同じ Volume 内でのリネームにも、別 Volume への移動にも使えます。

```bash
uv run python move_volume_file.py \
  comfy-model \
  diffusion_models/old-model.safetensors \
  comfy-model \
  diffusion_models/archive/old-model.safetensors
```

別 Volume へ移動する例:

```bash
uv run python move_volume_file.py \
  comfy-inputs \
  uploads/example.png \
  comfy-outputs \
  archived/example.png
```

主なオプション:

- `--yes`: 確認プロンプトをスキップ
- `--overwrite`: 移動先が存在する場合に上書き
- `--create-destination-volume`: 移動先 Volume が存在しない場合に作成

注意点:

- パスは Volume 内の相対パスで指定する
- `..` や絶対パスは受け付けない
- 移動先に既存ファイルがある場合は `--overwrite` が必要
- 移動先パスが既存ディレクトリなら、その配下へ元ファイル名のまま移動する
