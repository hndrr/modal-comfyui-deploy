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
- `web/comfy-adapter.mjs`: ComfyUIの拡張登録とワークフロー保存・復元APIへの接続です。
  本体更新でAPIが変わった場合はここで対応します。保存APIが使えない場合、切替を中止します。
- `GET /modal-control/v1/status`: CPU側バックエンドとのバージョン付き契約です。
  `api_version: 1`、`gpu: {containers, checked_at, phase}` とモード・環境情報を返します。
  `containers: null` は確認失敗であり、0台とは区別します。
- 明示的な管理操作は既存の `POST /split/mode`、`/split/environment/apply`、
  `/split/environment/discard` を使います。状態表示だけでは呼びません。

読み込みにはComfyUIの `app.registerExtension()` を使います。
現行検証対象はComfyUI `5bbdf8a76678e2c7cfb519a49a9c3a7137fd6280`、frontend `1.51.10`。
将来のすべてのバージョンとの互換性を保証するものではありません。
更新時は標準拡張一覧、状態API、GPU0台の表示、保存・復元を確認してください。
本体の固定バージョンをこの拡張の変更に合わせて更新する必要はありません。

公式の読み込み仕様: https://docs.comfy.org/custom-nodes/js/javascript_overview

GPU状態は標準ツールバー内の独立したボタンにも常時表示し、クリックするとGPUサイドバーを切り替える。
位置の固定やキャンバスへの重ね描きは行わない。

台数表示は `GPU(0)` の形式とし、稼働中は赤、取得失敗時は `GPU(?)` とする。
