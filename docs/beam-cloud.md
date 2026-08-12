# Beam CloudへのComfyUIデプロイ

`beamapp.py`は、既存のModal版をBeam Cloudのserverless `Pod`へ移植したデプロイ定義です。ComfyUIは8000番ポートで公開され、生成に使うデータは5つのBeam Volumeへ保存されます。

## 構成

- Python 3.12
- CUDA 13.0 toolkit
- PyTorch 2.10.0 + CUDA 13.0
- Comfy Kitchen 0.2.30公式CUDA wheel + cuBLAS 13
- xFormers / FlashAttention / SageAttention（SageAttentionは既定off）
- `comfy-cli==1.7.3`
- Modal版と同じ4つのcustom nodes
- RTX 5090 / RTX 4090（serverless）
- RTX PRO 6000 Blackwell / H100 / A100 80GB（on-demand pool）
- アイドル時のscale-to-zero

Comfy Kitchen 0.2.30の公式CUDA wheelとcuBLAS拡張はCUDA 13およびNVIDIA driver 580以上を要求します。さらに、固定しているComfyUI commit `024cbc5fc1c779ea7905356d3f3239b90dd0dae3`は、PyTorchがcu130未満の場合にKitchen CUDA backendを明示的に無効化します。そのためBeam版もModal版と同じCUDA 13へ統一しています。

起動時にはdriver 580以上、PyTorch cu130、GPU compute capability、Kitchen 0.2.30、ComfyUIのquantization policy適用後のCUDA backendを検証します。Blackwellでは`scaled_mm_nvfp4`も必須です。別GPUへの誤配置やfallback状態を検出した場合はComfyUIを起動せずエラー終了します。

## 1. セットアップ

依存関係をインストールします。

```bash
uv sync
cp .env.example .env
```

BeamのダッシュボードでAPI tokenを取得し、CLI contextを作成します。

```bash
uv run beam config create production
uv run beam config select production
```

`beam config create`で聞かれるGateway HostとGateway Portは、Beam Cloudを使う場合は空欄のままで構いません。

## 2. デプロイ

```bash
uv run beam deploy beamapp.py:comfyui
```

Beamがイメージをビルドし、ソースを同期してPodをデプロイします。完了時に表示されるHTTPS URLをブラウザで開いてください。最初のデプロイではPyTorchやCUDA拡張を組み込むため、ビルド完了まで時間がかかります。以降はBeamのイメージキャッシュが利用されます。

デプロイとコンテナの確認:

```bash
uv run beam deployment list
uv run beam container list
```

ログの確認:

```bash
uv run beam logs --deployment-id <DEPLOYMENT_ID>
```

## 3. 永続Volume

既定では次のVolumeがデプロイ時に自動作成されます。

| Beam Volume | コンテナ内 | ComfyUI |
| --- | --- | --- |
| `comfyui-models` | `/models` | `models` |
| `comfyui-custom-nodes` | `/data/custom_nodes` | `custom_nodes` |
| `comfyui-outputs` | `/data/output` | `output` |
| `comfyui-inputs` | `/data/input` | `input` |
| `comfyui-user-data` | `/data/user` | `user` / workflows |

起動時に`beam_runtime.py`がComfyUI側の各ディレクトリをVolumeへ接続します。イメージに含まれるcustom nodesは初回起動時にVolumeへ移され、以後の変更も保持されます。

Volume名の`comfyui`部分は`COMFYUI_BEAM_VOLUME_PREFIX`で変更できます。

## 4. モデルとファイルの転送

ローカルのcheckpointをアップロードする例:

```bash
uv run beam cp ./model.safetensors beam://comfyui-models/checkpoints/model.safetensors
```

入力ファイルのアップロード:

```bash
uv run beam cp ./input.png beam://comfyui-inputs/input.png
```

生成結果の確認とダウンロード:

```bash
uv run beam ls comfyui-outputs
uv run beam cp beam://comfyui-outputs/example.png ./example.png
```

Beam Volumeに書いたファイルが別コンテナから見えるまで、最大60秒程度かかる場合があります。アップロード直後にComfyUIへ表示されない場合は少し待ってから再読込してください。

## 5. 環境変数

`beamapp.py`はリポジトリ直下の`.env`を読み込みます。シェルですでに設定された値が優先されます。

```env
COMFYUI_BEAM_APP_NAME=comfyui
COMFYUI_BEAM_GPU=rtx5090
COMFYUI_BEAM_CPU=12
COMFYUI_BEAM_MEMORY=32Gi
COMFYUI_BEAM_KEEP_WARM_SECONDS=30
COMFYUI_BEAM_AUTHORIZED=off
COMFYUI_BEAM_VOLUME_PREFIX=comfyui
COMFYUI_BEAM_POOL=
COMFYUI_BEAM_SAGE_ATTENTION=off
COMFYUI_CLI_ARGS=
```

### GPU

`COMFYUI_BEAM_GPU`で次のプロファイルを選べます。`serverless`の可否は2026-08-12に、このworkspaceの`beam machine list --format json`で確認したスナップショットです。在庫とon-demandホストのdriverは変動するため、デプロイ直前にも確認してください。

| 値 | Beam GPU | SM | Kitchen wheel target | 容量 | Kitchenでの位置づけ |
| --- | --- | --- | --- | --- | --- |
| `rtx5090`（既定） | RTX5090 | 12.0 | `120f` | serverless `ready` | Blackwell。KitchenのFP8 / NVFP4 / MXFP8のハードウェア条件を満たす |
| `rtx4090` | RTX4090 | 8.9 | `89` | serverless `ready` | Ada。KitchenのFP8条件を満たす |
| `rtx-pro-6000` | RTXPro6000 | 12.0 | `120f` | on-demand | Blackwell、96GB。Kitchenのハードウェア条件と大規模workflow向け |
| `h100` | H100 | 9.0 | `90a` | on-demand | Hopper、80GB。KitchenのFP8条件を満たす |
| `a100-80gb` | A100-80 | 8.0 | `80` | on-demand | Ampere、80GB。汎用CUDA / INT8向け。FP8 / NVFP4 / MXFP8対象外 |

この5種類にした理由は、Beam SDK 0.2.207が受理するGPU名、実際の`beam machine list`の供給状況、NVIDIA公式compute capability、Comfy Kitchen 0.2.30のハードウェア要件を突き合わせたためです。

Comfy KitchenのTensorCore FP8 layoutはSM 8.9以上、NVFP4/MXFP8 layoutはSM 10.0以上を要求します。そのためKitchenを主目的にする既定値は、serverlessで使えるBlackwellのRTX 5090です。A100はVRAM 80GBが必要なworkflowには有効ですが、KitchenのFP8/NVFP4/MXFP8目的では選びません。

BeamのA10Gはserverless在庫がありますが、公式driverスナップショットが575.57.08（最大CUDA 12.9）です。現在のComfyUIはcu130未満でKitchen CUDA backendを無効化するため、CUDA 13版の選択肢から除外しています。RTX4090は公式にdriver 580.126.18 / CUDA 13.0対応です。RTX5090およびon-demand GPUは実コンテナの`nvidia-smi`を起動時に検証します。

ここでいう「ハードウェア条件を満たす」は、すべての演算が必ずCUDA kernelへdispatchされるという意味ではありません。例えば`scaled_mm_nvfp4`はcuBLASLtの実行時可用性にも依存し、MXFP8を含む一部演算は入力条件に応じてTriton/eagerへfallbackします。起動ログの`kitchen_cuda_capabilities`が、そのコンテナで実際に登録されたCUDA演算です。

利用可能なGPUはアカウントやその時点の在庫で変わります。確認には次を使います。

```bash
uv run beam machine list
```

設定変更後は再デプロイしてください。

```bash
COMFYUI_BEAM_GPU=rtx4090 uv run beam deploy beamapp.py:comfyui
```

Comfy Kitchen公式wheelは対応architectureをまとめて含みます。`COMFYUI_BEAM_SAGE_ATTENTION=on`の場合だけ、GPUを変えるとSageAttentionの該当architectureが再ビルドされます。

### on-demand GPU

`rtx-pro-6000`、`h100`、`a100-80gb`は、現在このworkspaceのserverless在庫にはありません。先に有料のon-demand machineをpoolへ予約し、そのpoolをPodへ指定します。例えばRTX PRO 6000を2時間予約する場合:

```bash
uv run beam machine reserve --gpu RTXPro6000 --ttl 2h --name comfyui-blackwell
COMFYUI_BEAM_GPU=rtx-pro-6000 \
COMFYUI_BEAM_POOL=comfyui-blackwell \
uv run beam deploy beamapp.py:comfyui
```

予約はPodがscale-to-zeroしてもTTLまで課金対象になり得ます。不要になったpoolは明示的に解放してください。

```bash
uv run beam machine release --pool comfyui-blackwell
```

A100 80GBの場合:

```bash
uv run beam machine reserve --gpu A100-80 --ttl 2h --name comfyui-a100
COMFYUI_BEAM_GPU=a100-80gb \
COMFYUI_BEAM_POOL=comfyui-a100 \
uv run beam deploy beamapp.py:comfyui
```

on-demandホストのdriverが580未満ならCUDA 13を実行できないため、Podは起動時検証で停止します。その場合は予約を解放し、別のofferまたはGPUを選んでください。

### scale-to-zero

`COMFYUI_BEAM_KEEP_WARM_SECONDS`は最後のリクエスト後にPodを保持する秒数です。既定値は30秒です。

- `0`: アイドルになり次第停止
- 正の整数: 指定秒数だけ保持
- `-1`: scale-to-zeroせず起動し続ける

ComfyUIはWebSocketを使うため、ブラウザのタブが接続中はPodがアクティブと判定される場合があります。確実に停止させるには生成後にComfyUIのタブを閉じてください。

### 認証

`COMFYUI_BEAM_AUTHORIZED=off`が既定で、この状態ではURLを知っている利用者がPodへアクセスできます。公開したくない場合は`on`へ変更し、Beam API tokenをAuthorization Bearer headerへ付与できるクライアントまたは認証プロキシからアクセスしてください。

```bash
COMFYUI_BEAM_AUTHORIZED=on uv run beam deploy beamapp.py:comfyui
```

通常のブラウザは任意のAuthorization headerを付けられないため、ComfyUIをブラウザから直接使う構成では`off`が必要です。その場合は公開URLの共有範囲に注意してください。

### ComfyUI起動引数

`COMFYUI_CLI_ARGS`は`comfy launch -- ...`の末尾へ追加されます。shell文字列として解釈されるため、空白を含む値は引用符で囲めます。

```bash
COMFYUI_CLI_ARGS="--cpu-vae" uv run beam deploy beamapp.py:comfyui
```

Beam版ではComfy Kitchenを優先し、SageAttentionは既定でoffです。有効化する場合は、Beam専用の`COMFYUI_BEAM_SAGE_ATTENTION`を使います。Modal版の`COMFYUI_SAGE_ATTENTION`設定には影響しません。

```bash
COMFYUI_BEAM_SAGE_ATTENTION=on uv run beam deploy beamapp.py:comfyui
```

onのときだけSageAttentionをイメージへビルドし、`COMFYUI_CLI_ARGS`に同じ指定がない限り`--use-sage-attention`を自動追加します。切り替え後は再デプロイが必要です。

## 6. custom nodes

イメージには次のcustom nodesが含まれます。

- `https://github.com/crystian/ComfyUI-Crystools`
- `https://github.com/Firetheft/ComfyUI_Local_Media_Manager`
- `https://github.com/hayden-fr/ComfyUI-Image-Browsing`
- `https://github.com/rgthree/rgthree-comfy`

各custom nodeは、レビュー済みcommitへ固定してインストールします。追加・更新・削除する場合は
`beamapp.py`の`CUSTOM_NODES`にあるURLとcommit SHAを編集して再デプロイしてください。
SageAttentionもcommit SHAへ固定し、FlashAttention wheelはダウンロード時にSHA-256を検証します。
Comfy Kitchenは公式PyPI版`0.2.30`を指定しています。一般のAPT/PyPI依存を含む完全なlockは行いません。

## 7. トラブルシュート

Podへ入ってGPU、Volume、ComfyUIインストールを確認できます。

```bash
uv run beam shell beamapp.py:comfyui
nvidia-smi
python3 -c 'import torch, comfy_kitchen as ck; print(torch.cuda.get_device_capability()); print(ck.list_backends())'
ls -la /models /data/custom_nodes /data/output /data/input /data/user
comfy --help
```

GPUの在庫がない場合は`beam machine list`で確認し、別の`COMFYUI_BEAM_GPU`へ切り替えて再デプロイしてください。

## 参考

- [Beam公式: Serverless ComfyUI](https://docs.beam.cloud/v2/examples/comfy-ui)
- [Beam公式: PodでWeb serviceを公開する](https://docs.beam.cloud/v2/pod/web-service)
- [Beam公式: Volume](https://docs.beam.cloud/v2/data/volume)
- [Beam公式: GPU](https://docs.beam.cloud/v2/environment/gpu)
- [Beam公式: Container Images / CUDA driver互換性](https://docs.beam.cloud/v2/environment/custom-images)
- [Comfy Kitchen 0.2.30 README](https://github.com/Comfy-Org/comfy-kitchen/blob/v0.2.30/README.md)
- [NVIDIA公式: CUDA GPU Compute Capability](https://developer.nvidia.com/cuda/gpus)
- [NVIDIA公式: CUDA 13はdriver 580以上](https://docs.nvidia.com/deploy/cuda-compatibility/minor-version-compatibility.html)
- [固定ComfyUI: cu130未満ではKitchen CUDAを無効化](https://github.com/Comfy-Org/ComfyUI/blob/024cbc5fc1c779ea7905356d3f3239b90dd0dae3/comfy/quant_ops.py)
