# PyTorch 2.13.0 + cu130 へのアップグレード検討

## 目的

「ComfyUI が PyTorch 2.13.0 で速くなった」という話題を受けて、本リポジトリの現状を確認し、2.13.0 へ上げる場合の手順と制約を整理する。

この文書は調査結果と手順のメモであり、実装は行っていない。

## 結論: 速度差の実体は cu130 であり、本リポジトリは既に満たしている

ComfyUI 本体 `comfy/quant_ops.py` が comfy-kitchen の CUDA 最適化パスを有効にするかどうかの判定は、PyTorch のバージョンではなく **CUDA のバージョン** で行われている。

```python
cuda_version = tuple(map(int, str(torch.version.cuda).split('.')))
if cuda_version < (13,):
    logging.warning("WARNING: You need pytorch with cu130 or higher to use optimized CUDA operations.")
```

一般に「2.13.0 が速い」と言われるのは、2.11 リリース系列から PyPI の既定 CUDA が 13.0 になり、素直に `pip install torch` すると cu130 が入るようになったため。バージョン番号そのものが速度を決めているわけではない。

本リポジトリは `comfyapp.py` でホイール URL を直接指定して cu130 ビルドを入れており、さらに `cuda-toolkit-13-0` も導入している。したがって **PyTorch 2.10.0 のままで comfy-kitchen の高速パスは既に有効** であり、上記の警告は出ていないはずである。

なお `comfy-kitchen` 自体は PyPI 上で torch への依存ピンを持たない abi3 wheel なので、2.10 系でも問題なく動作する。

このため 2.13.0 へのアップグレードは **速度目的ではなく、バグ修正・新カーネル・上流追従が目的** になる。

## 現状のピン

`comfyapp.py` の以下の 5 定数でホイールを固定している。これらは base_image 側の pip install と、`comfy install` が依存を触った後の上書き install の両方から参照されるため、1 箇所直せば両方に効く。

| 定数 | 現在の値 |
| --- | --- |
| `TORCH_WHEEL_URL` | `torch-2.10.0+cu130-cp312-cp312-manylinux_2_28_x86_64.whl` |
| `TORCHVISION_WHEEL_URL` | `torchvision-0.25.0+cu130-cp312-cp312-manylinux_2_28_x86_64.whl` |
| `TORCHAUDIO_WHEEL_URL` | `torchaudio-2.10.0+cu130-cp312-cp312-manylinux_2_28_x86_64.whl` |
| `XFORMERS_WHEEL_URL` | `xformers-0.0.34-cp39-abi3-manylinux_2_28_x86_64.whl` |
| `FLASH_ATTN_WHEEL_URL` | `v0.9.0` / `flash_attn-2.8.3+cu130torch2.10-cp312-cp312-linux_x86_64.whl` |

GPU プロファイルの既定は `rtx-pro-6000`（`TORCH_CUDA_ARCH_LIST=12.0+PTX`）で、NVFP4 / FP8 の恩恵が出る Blackwell 世代を想定している。

## 変更案

### 1. ホイール URL の差し替え

配布状況は download.pytorch.org / PyPI / GitHub Releases に実際に問い合わせて確認済み（いずれも到達可能）。

| 定数 | 変更後の値 |
| --- | --- |
| `TORCH_WHEEL_URL` | `torch-2.13.0+cu130-cp312-cp312-manylinux_2_28_x86_64.whl` |
| `TORCHVISION_WHEEL_URL` | `torchvision-0.28.0+cu130-cp312-cp312-manylinux_2_28_x86_64.whl` |
| `TORCHAUDIO_WHEEL_URL` | `torchaudio-2.11.0+cu130-cp312-cp312-manylinux_2_28_x86_64.whl`（据え置き） |
| `XFORMERS_WHEEL_URL` | `xformers-0.0.35-py39-none-manylinux_2_28_x86_64.whl` |
| `FLASH_ATTN_WHEEL_URL` | `v0.9.47` / `flash_attn-2.8.3+cu130torch2.13-cp312-cp312-linux_x86_64.whl` |

ベース URL は既存と同じ `https://download.pytorch.org/whl/cu130/<filename>`（`+` は `%2B` エスケープ）。flash-attn は GitHub Release の直リンクで、既存同様 `+` はエスケープ不要。

flash-attn のリリースタグは最新（v0.9.52 系）ではなく **v0.9.47** である点に注意。`cu130torch2.13` のアセットを持つのがこのタグ。

### 2. torchaudio はマイナー不一致を許容する

**cu130 index も PyPI も torchaudio は 2.11.0 が最新で打ち止め**であり、2.12 / 2.13 に対応する版は存在しない。ComfyUI の `requirements.txt` には依然 `torchaudio` が含まれている。

対応方針は 2.11.0+cu130 の据え置き。torchaudio の wheel は `requires_dist` を持たないため pip は依存衝突を報告しないが、libtorch にリンクした拡張を含むので **ビルド後に `import torchaudio` が通るかの確認が必須**。この意図はコード上にコメントで残すこと。

### 3. xformers は 0.0.35 へ上げる必要がある

0.0.34 は abi3 wheel だが、これは CPython の ABI を指すもので libtorch の ABI ではない。torch 2.10 に対してリンクされているため、torch 2.13 では import 時に undefined symbol で落ちる可能性が高い。

ただし 0.0.35 は性質が変わっている点に注意。

- 0.0.34: 108 MB（CUDA カーネルを同梱、`cp39-abi3`）
- 0.0.35: 5.5 MB（`py39-none`、依存は `torch>=2.10` と `numpy` のみ）

つまり自前の CUDA カーネルを同梱しなくなっている。本リポジトリは既定で `--use-sage-attention` で動くため実害は小さい見込みだが、影響は意識しておく。既存の「cu130 index は abi3 wheel を 0.0.34 で公開」というコメントは実態と合わなくなるため更新する。

### 4. SageAttention は自動で追従する

SageAttention は torch インストール層の**後**にソースからビルドされる。`TORCH_WHEEL_URL` を変えるとその層以降のビルドキャッシュが無効化されるため、2.13 に対して自動で再ビルドされる。`SAGEATTENTION_REF = "abi3_stable"` の変更も `COMFYUI_FORCE_BUILD=on` の指定も不要。

その代わり **base_image のほぼ全体が再ビルドされ、SageAttention のコンパイルに相応の時間がかかる**。

### 5. ドキュメント更新

`README.md` の「PyTorch 2.10.0 + CUDA 13.0 系の wheel を利用」を 2.13.0 に更新し、torchaudio のみ 2.11.0 据え置きである旨を添える。

### 触らないもの

- `cuda-toolkit-13-0` の導入層
- `comfy-cli==1.7.3` のピン（最新は 1.13.0 だが、上げると `comfy install` の挙動が変わるため別件）
- GPU プロファイル / `TORCH_CUDA_ARCH_LIST`

## 検証手順

### 1. ビルドを通す

```bash
uv run modal deploy comfyapp.py
```

pip の依存解決エラー（特に torchvision が `torch==2.13.0` を要求する箇所と、torchaudio 2.11 との組み合わせ）が出ないことを確認する。

### 2. コンテナ内で実バージョンとロード可否を確認（最重要）

```bash
uv run modal shell comfyapp.py::ui
```

```python
import torch, torchvision, torchaudio, xformers
print(torch.__version__, torch.version.cuda)   # 期待: 2.13.0+cu130 13.0
print(torchvision.__version__, torchaudio.__version__)
import flash_attn, sageattention
import comfy_kitchen as ck
print("ok")
```

torchaudio のマイナー不一致はここで表面化する。`import torchaudio` が undefined symbol などで落ちる場合、2.11.0 据え置きは成立しないため、torchaudio のピン除去か torch 2.12 系への後退を検討し直す。

### 3. 起動ログで comfy-kitchen の高速パスを確認

```bash
uv run modal app logs comfyui
```

- `WARNING: You need pytorch with cu130 or higher to use optimized CUDA operations.` が **出ていない** こと
- `Found comfy_kitchen backend ...` の info ログが出ていること

この 2 点は 2.10.0+cu130 時点でも既に満たされているはずなので、アップグレードによるデグレが無いことの確認という位置づけ。

### 4. 実ワークフローのスモークテスト

ComfyUI の UI で既存の代表的なワークフローを 1 本流し、SageAttention / flash-attn 経路でエラーが出ないこと、生成結果が壊れていないことを確認する。

### 5. ロールバック

問題が出た場合は 5 定数を元の値に戻して再デプロイするだけで戻せる。cu130 による高速パスは 2.10.0 でも有効なので、ロールバックしても速度面の損失は無い。

## 参考

- [ComfyUI — `comfy/quant_ops.py`](https://github.com/comfyanonymous/ComfyUI/blob/master/comfy/quant_ops.py)
- [ComfyUI — `requirements.txt`](https://github.com/comfyanonymous/ComfyUI/blob/master/requirements.txt)（`comfy-kitchen==0.2.26`）
- [PyTorch cu130 wheel index](https://download.pytorch.org/whl/cu130/)
- [mjun0812/flash-attention-prebuild-wheels](https://github.com/mjun0812/flash-attention-prebuild-wheels/releases/tag/v0.9.47)
