# Ambient StudioのMiniMax H3高速化候補

調査日: 2026-09-20。ユーザー指定により対象はMiniMax H3系のみ。公開資料と保存済みの実GPU記録を調査した。今回は設定変更、モデル取得、GPU生成、デプロイは行っていない。

## 推奨する検証順

1. 現在のH3 Turboを4stepで比較する。
2. 指定されたFused Turbo + Mysticの4stepを比較する。
3. H3 TurboにComfyUI標準のComfy Kitchen Attention／Sol-Attnを追加し、それぞれ単独の効果を測る。Fused配布元のSLA構成も別条件として検証する。
4. H3 PDD Accの4step／8stepを比較する。

この順番は、現在の音声付きT2V／I2Vを維持しながら、導入変更を小さくできる順序。現環境での速さの順位を表すものではない。

## 現在の構成と実測

- GPU: RTX PRO 6000 Blackwell Server Edition、1台。
- 検証済みのComfyUI: 0.36.0、commit `7a0b5eede3f9721c8faab290689893f36edc6d66`、comfy-kitchen 0.2.34。
- `h3`: pruned INT8 ConvRot H3 + FL2VA Turbo 8step v1.0。現在のレシピは8step、`res_multistep`。GPU起動設定は既定でSageAttentionを有効にする。
- `fasth3`: 4step VSA。今回追加したFastH3 V2は8step、T2VはVSA、I2Vはsol-attn。
- 24fps・124フレーム。現在の9:16 Previewは576×1024。

| 記録 | 解像度 | ComfyUI実行時間 | 計測範囲 |
| --- | --- | ---: | --- |
| H3 Turbo 8step、9月16日 | 832×480 | 42.59秒 | 読み込み・生成・保存を含む |
| FastH3 4step、9月16日 | 832×480 | 39.27秒 | 同上 |
| FastH3 V2 8step T2V、9月20日 | 576×1024 | 46.08秒 | 同上 |
| FastH3 V2 8step I2V、9月20日 | 576×1024 | 43.19秒 | 同上 |

各1回の機能検証であり、解像度・ComfyUI・読み込み条件も異なる。モデル間の速度ランキングには使えない。9月20日の受付要求から保存完了検知まではT2V 119.59秒、I2V 105.92秒だった。

記録: [9月16日](../ambient/docs/validation/2026-09-16/fastvae/report.md)、[9月20日](../ambient/docs/validation/2026-09-20/fasth3-eight/README.md)。

## 1. H3 Turbo 4step

**最初の比較候補。** 現在使っているFL2VA Turbo 8step v1.0は、提供元が4step実行も案内している。ベースモデルとLoRAを再利用でき、T2V・I2V・音声出力の構成を保ちやすい。

比較用設定は4step、Euler、CFGなし／1、video/audio shift 12/3、LoRA強度1を出発点にする。現行の8step・res_multistepとの比較に加え、8step・Eulerも測り、ステップ数とサンプラーの効果を混同しない。4stepにしても読み込み・デコード・保存時間は半分にならない。

高解像度向けには別のFL2VA Turbo 4step v1.0 768pもある。こちらはshift 6/3で、現行LoRAの12/3とは異なる。モデル名だけを差し替えず、対応する設定を一緒に適用する。

4step化では映像の動きと音楽・打楽器の品質を確認する。速度だけで既定値を決めない。

出典: [提供元のモデル仕様](https://github.com/ModelTC/Minimax-H3-Turbo#1-model-specs)、[ComfyUI設定](https://github.com/ModelTC/Minimax-H3-Turbo/blob/main/COMFYUI_SETUP_AND_INFERENCE.md)。

## 2. Fused Turbo + Mystic 4step

対象: `minimax_h3_fused_refdelta_r1024_turbo8_mystic07_int8_convrot.safetensors`。MATLOWAI配布の約21 GBの統合モデル。

| 名前の部分 | 内容 |
| --- | --- |
| `fused_refdelta_r1024` | FL2VAへRef2VAとの差分をrank 1024で統合。開始／終了フレームと参照素材を同じ重みで扱う |
| `turbo8` | FL2VA Turbo 8step v1.0を強度1で統合。配布元は4step運用を案内 |
| `mystic07` | 動きを滑らかにするMystic v2を強度0.7で統合。後から強度だけ変更できない |
| `int8_convrot` | 統合後に量子化。標準のUNETLoaderでロード可能 |

配布元の4step設定は`res_multistep`／`simple`、video/audio shift 12/3、CFGなし、SLA sparsity 0.90。既存のTurbo LoRAを重ねて適用しない。SLA拡張を使う公開ワークフローの互換性は別途確認する。標準Sol-Attnと同じ実装だとは扱わない。

配布元のRTX PRO 6000 96 GB・1152×640・243フレームでは、単段4step 76秒、8step 103秒。焼き込み版と同じLoRAを別途適用した版の8stepは両方103秒で、ピークVRAMが68.9→47.8 GB。統合の実測上の利点は省メモリで、現行Turboより速い証拠ではない。Ambientの576×1024・124フレームで比較する。公開の4+4 de-ropeは追加処理を含む約374秒の構成なので、速度比較には単段版を使う。

出典: [配布元の構成・設定・実測](https://huggingface.co/MATLOWAI/minimax-h3-fused-turbo-int8-convrot)。

## 3. H3 Turbo + Comfy Kitchen／Sol-Attn

現行のTurboレシピには、FastH3側にある`ModelAttentionBackend`と`BlockSparseAttention`が接続されていない。まず通常AttentionをComfy Kitchenに指定する比較、その後Sol-Attnを追加する比較ができる。

Sol-Attnは再学習を前提としない。初回候補は標準値のtau 1.3、start 0.2、end 1、min_tokens 12288、extra_tokens 256、sink_conditioning `exact_kv_and_rows`。音声の行をdenseで処理する設定を維持し、実行ログでsparse経路に入ったかも確認する。短い系列ではdenseの方が速い場合があり、効果の倍率は未確定。

VSAは対応学習済みのFastH3用なので、通常H3へ無条件に流用しない。旧`ComfyUI-SolAttn_triton`は作者が非推奨とし、ComfyUI／Comfy Kitchenの標準実装を案内しているため、新たな導入候補にはしない。

出典: [デプロイ版ComfyUIのSparse Attention実装](https://github.com/Comfy-Org/ComfyUI/blob/7a0b5eede3f9721c8faab290689893f36edc6d66/comfy_extras/nodes_sparse_attention.py)、[旧拡張の移行案内](https://github.com/kijai/ComfyUI-SolAttn_triton)。

## 4. MiniMax-H3 PDD Acc

Alibaba PAIがFL2VAとRef2VA向けのPDD Accを公開している。4／8回のモデル評価による生成例があり、音声付きH3の別候補になる。

通常のLoRAに加えてステップごとの出力ヘッドを持つため、現行の`LoraLoaderModelOnly`へファイルだけ差し替える方法は使わない。ヘッドとスケジュールを扱うComfyUI対応実装を選び、既存のINT8 ConvRotモデル・開始画像・音声契約との整合を検証する。4step版がTurbo 4stepより速いことは未確認。主に速度と品質の選択肢として比較する。

出典: [提供元モデルカード](https://huggingface.co/alibaba-pai/MiniMax-H3-Acc-LoRAs)、[ComfyUI対応ノードの説明](https://github.com/Jalen-Brunson/ComfyUI-MiniMax-H3-PDD-Acc)。

## ほかに調べた候補

| 候補 | 公開情報 | 今回の判断 |
| --- | --- | --- |
| MotionCache／FirstBlockCache系 | 計算結果を再利用してモデル評価を減らす | 4／8stepの後で検討。導入済みMotionCacheの説明ではwarmupが既定4回なので、4stepでは省略余地がない。音と動きへの影響も確認が必要 |
| Fast VAEの追加調整 | バッチ化でデコード呼び出しを減らす | 優先度は低い。同じ環境でのデコード単体中央値は標準1.797秒、Fast VAE 1.724秒。既に改善余地が小さい |
| Sol-H3 | 5秒の音声付き768pを1×B300で13.745秒、8×B300で1.653秒 | 専用実行基盤の候補。読み込み・初回コンパイル・MP4エンコードを除いた計測で、現GPUへそのまま適用できない。公開実装でもSM120経路は未検証とされる |

出典: [MotionCache作者の説明](https://github.com/Mozer/ComfyUI-MiniMax-H3-MotionCache-FastVAE)、[ローカルVAE計測](../ambient/docs/validation/2026-09-16/comfyui-update/benchmark.json)、[Sol-H3の計測と制約](https://github.com/NVlabs/Sana/tree/sol-engine/models/minimax_h3/Sol-H3)。

## モデル以外の待ち時間

- **Jevと生成のキューを分離する候補。** タグ処理のModal関数自体はCPUだが、内部でComfyUIの`/prompt`へ投入するため、生成用GPUワーカーとキューを共有する。9月20日のI2V記録にも先行タグ処理の待ちが含まれる。外部APIによるJev処理をGPUの枠から外すか、動画生成の優先順位を上げれば、モデルを変えずに待ちを減らせる可能性がある。ComfyUIでの表示・編集と出力契約は維持する。
- **連続生成時の再読み込みを計測する。** 現行ワーカーにはComfyUIプロセスの再利用が既にある。単純に毎回起動する設計とは言えないが、Volumeの再読込でファイルが開いている場合はプロセスを止める。連続ジョブでこの経路が起きているかを測る。生成停止後にCPU/GPUを0台へ戻す条件は維持する。
- **Previewの画素数を比較条件に含める。** 576×1024は589,824画素、以前の832×480は399,360画素で約1.48倍。少しの設定変更に見えても計算量は同じではない。低解像度を追加するなら9:16指定とH3の32px単位の制約を両立させ、自由文・画角を勝手に変更しない。
- **初回コンパイルと定常運転を分ける。** torch.compileや専用カーネルの高速化は準備時間を伴う。常時起動しない運用では、1本目と連続生成を別々に評価する。

根拠: [タグ投入経路](../ambient/tagging.py)、[GPUワーカー](../comfy_split/worker.py)、[プロセス管理](../comfy_split/runtime.py)、[解像度定義](../ambient/contracts.py)。

## 次に実測するときの条件

1. 同じGPU、576×1024、124フレーム・24fps、同じ映像・音声指示を固定する。現行FastH3 4step／8stepも同じ条件で基準として測る。
2. 自然風景、UK garage、shoegaze、波形系の複数プロンプトでT2V／I2Vを比較する。映像だけでなく音楽・打楽器・開始画像の保持も確認する。
3. コールド起動と、モデル読み込み済みの連続実行を分ける。定常実行は複数回測り中央値を使う。
4. キュー、ワーカー準備、テキスト処理、サンプリング、デコード、保存を分けて記録する。最終判断には受付からStudioで再生可能になるまでの時間を使う。
5. ジョブ完了とライブラリ保存の後、CPU/GPUが0台まで戻ることを確認する。

未測定の候補について秒数や高速化率を保証しない。上記は次の比較対象を選ぶための調査結果である。
