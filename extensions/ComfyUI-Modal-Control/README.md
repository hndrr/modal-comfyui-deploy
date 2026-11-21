# ComfyUI-Modal-Control

標準サイドバーの「GPU」タブに稼働状態を表示する、フロントエンド専用のComfyUI拡張です。
キャンバス上に固定ボタンや独自ポップアップは置きません。
モード切替は折りたたまず表示し、ノード更新操作は未反映の更新がある場合だけ表示します。
推論ノード、Python依存パッケージ、ComfyUI本体へのパッチはありません。

## インストール

このディレクトリを `ComfyUI/custom_nodes/ComfyUI-Modal-Control` に配置し、
ComfyUIを再起動してブラウザを再読み込みします。
`__init__.py` の `WEB_DIRECTORY` により、ComfyUIが標準の方法で配信します。
Modal版ではイメージ内の `/opt/comfy-extensions` を
`--extra-model-paths-config` で追加しています。Managerが編集する環境Volumeとは分かれており、
ComfyUIや環境バージョンを交換しても拡張はイメージから読み込まれます。

対応するModalバックエンドが必要です。通常のComfyUIではAPIが存在しないため、
ボタンを追加せず終了します。拡張自体に認証情報やModal SDKは不要です。

## 境界とアップデート

- `web/modal-control.js`: 独立したDOMで状態表示・操作を提供します。
  ComfyUI内部のCSSクラス、Vueストア、既存ツールバーのDOM構造には依存しません。
- `web/comfy-adapter.mjs`: サイドバー登録、標準ツールバーへのボタン追加、ワークフロー保存・復元APIへの接続です。`ComfyButton`と`app.menu.element`にも依存します。
  本体更新でAPIが変わった場合はここで対応します。保存APIが使えない場合、切替を中止します。
- `GET /modal-control/v1/status`: CPU側バックエンドとのバージョン付き契約です。
  `api_version: 1`、`gpu: {containers, checked_at, phase}` とモード・環境情報を返します。
  `containers: null` は確認失敗であり、0台とは区別します。
- GPUパネルでは割り当て待ちのジョブを手動で別機種へ移せます（`POST /split/jobs/<prompt_id>/gpu`）。自動切り替えの初期値は3分後にRTX PRO 6000で、パネルから全員に適用する設定を保存します（`POST /split/gpu-fallback`）。
- モード切替と環境更新は `POST /split/mode`、`/split/environment/apply`、
  `/split/environment/discard` を使います。状態表示だけでは呼びません。

読み込みにはComfyUIの `app.registerExtension()` を使います。
split はデプロイ時に ComfyUI の安定リリースを選び、そのリリースが指定するフロントエンドを使います（[split の更新方法](../../docs/guides/comfyui-split.md)）。

### フロントエンド更新時の確認

1. 検証環境で標準 UI を開き、`GET /extensions` の一覧にこの拡張の `modal-control.js` があることを確認します。読み込みに失敗したら、ブラウザのコンソールで `comfy-adapter.mjs` の import を確認します。[公式の拡張読み込み仕様](https://docs.comfy.org/custom-nodes/js/javascript_overview)
2. GPU 停止中にツールバーの `GPU(0)` をクリックし、サイドバーが開くことを確認します。状態 API が台数を返しているのにボタンが出ない場合は、アダプターの `registerGpuIndicator` と `registerGpuSidebar` を確認します。
3. 検証用ワークフローを編集し、キューが空の状態で GPU 常時起動へ切り替え、分離モードへ戻します。各回の再読み込み後にノード・接続・編集値が復元されることを確認します。保存時は `saveWorkflow`、復元時は `restoreWorkflow` が公式 UI との接点です。
