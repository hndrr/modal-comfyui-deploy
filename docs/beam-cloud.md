# Beam CloudへのComfyUIデプロイ

`beamapp.py`は、既存のModal版をBeam Cloudのserverless `Pod`へ移植したデプロイ定義です。ComfyUIは8000番ポートで公開され、生成に使うデータは5つのBeam Volumeへ保存されます。

## 構成

- Python 3.12
- CUDA 12.8 toolkit
- PyTorch 2.10.0 + CUDA 12.8
- Comfy Kitchen 0.2.30（CUDA 12.8からGPU別にソースビルド）
- xFormers / FlashAttention / SageAttention（SageAttentionは既定off）
- `comfy-cli==1.7.3`
- Modal版と同じ4つのcustom nodes
- RTX 5090 / RTX 4090 / A10G（serverless）
- RTX PRO 6000 Blackwell / H100（on-demand pool）
- アイドル時のscale-to-zero

Comfy Kitchen 0.2.30の配布済みCUDA wheelはCUDA runtime 13.0以上を要求します。一方、ソースビルドはCUDA Toolkit 12.8以上をサポートします。BeamのA10Gを含めて動かせるように、この構成ではPyPI wheelを使わず、`v0.2.30`をCUDA 12.8でGPU architecture別にビルドします。ComfyUI本体も、`comfy-kitchen==0.2.30`を要求する確認済みcommit `024cbc5fc1c779ea7905356d3f3239b90dd0dae3`へ固定しています。

起動時には`torch.cuda`のGPU、compute capability、Comfy KitchenのCUDA backendを検証します。別GPUへの誤配置やCUDA拡張のロード失敗時は、ComfyUIを低速なfallback状態で起動せずエラー終了します。

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

`COMFYUI_BEAM_GPU`で次のプロファイルを選べます。`serverless`の可否は2026-08-12に、このworkspaceの`beam machine list --format json`で確認したスナップショットです。在庫は変動するため、デプロイ直前にも確認してください。

| 値 | Beam GPU | SM | Kitchen build | 容量 | Kitchenでの位置づけ |
| --- | --- | --- | --- | --- | --- |
| `rtx5090`（既定） | RTX5090 | 12.0 | `120f` | serverless `ready` | Blackwell。KitchenのFP8 / NVFP4 / MXFP8のハードウェア条件を満たす |
| `rtx4090` | RTX4090 | 8.9 | `89` | serverless `ready` | Ada。KitchenのFP8条件を満たす |
| `a10g` | A10G | 8.6 | `86` | serverless `available` | Kitchen CUDAの汎用kernel / INT8向けfallback |
| `rtx-pro-6000` | RTXPro6000 | 12.0 | `120f` | on-demand | Blackwell、96GB。Kitchenのハードウェア条件と大規模workflow向け |
| `h100` | H100 | 9.0 | `90a` | on-demand | Hopper、80GB。KitchenのFP8条件を満たす |

この5種類にした理由は、Beam SDK 0.2.207が受理するGPU名、実際の`beam machine list`の供給状況、NVIDIA公式compute capability、Comfy Kitchen 0.2.30のハードウェア要件を突き合わせたためです。単に「速そうなGPUを3つ」選んだものではありません。

Comfy KitchenのTensorCore FP8 layoutはSM 8.9以上、NVFP4/MXFP8 layoutはSM 10.0以上を要求します。そのためKitchenを主目的にする既定値は、serverlessで使えるBlackwellのRTX 5090です。A10GでもKitchen CUDA backend自体はビルド・ロードしますが、この3種類の量子化layout目的では選びません。

ここでいう「ハードウェア条件を満たす」は、すべての演算が必ずCUDA kernelへdispatchされるという意味ではありません。例えば`scaled_mm_nvfp4`はcuBLASLtの実行時可用性にも依存し、MXFP8を含む一部演算は入力条件に応じてTriton/eagerへfallbackします。起動ログの`kitchen_cuda_capabilities`が、そのコンテナで実際に登録されたCUDA演算です。

利用可能なGPUはアカウントやその時点の在庫で変わります。確認には次を使います。

```bash
uv run beam machine list
```

設定変更後は再デプロイしてください。

```bash
COMFYUI_BEAM_GPU=rtx4090 uv run beam deploy beamapp.py:comfyui
```

GPUを変えるとComfy KitchenとSageAttentionのCUDA architectureも変わるため、該当レイヤーは再ビルドされます。

### on-demand GPU

`rtx-pro-6000`と`h100`は、現在このworkspaceのserverless在庫にはありません。先に有料のon-demand machineをpoolへ予約し、そのpoolをPodへ指定します。例えばRTX PRO 6000を2時間予約する場合:

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

追加・削除する場合は`beamapp.py`の`CUSTOM_NODES`を編集して再デプロイしてください。

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
