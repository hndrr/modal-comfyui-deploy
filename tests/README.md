# テスト

本リポジトリでは、`scripts/test_standalone.py` を共通ランナーとしてローカルテストを実行します。
すべてのテストスイートは共通の環境で実行でき、非公開の外部拡張を別途インストールする必要はありません。

---

## ディレクトリ構成と検証対象

| ディレクトリ | 主な検証対象 |
| :--- | :--- |
| `tests/standard/` | 通常構成（`comfyapp.py`）の設定、ComfyUI Manager 連携、WebSocket 圧縮無効化パッチ |
| `tests/split/` | CPU/GPU 分離構成（`splitapp.py`）の Journal、Gateway、GPU worker、起動・CPU Snapshot 復元、Volume 整合性、候補環境の検証、拡張機能の配布 |
| `tests/assets/` | アセット管理および RPC 連携（※Web 側の単体テストは `web/` 配下で実行） |
| `tests/models/` | モデルの事前準備、保存・コミット処理、公開 API の設定 |

---

## テストの実行方法

事前にテスト用の依存関係を同期した上で実行します。

```bash
# テスト環境のセットアップ（依存関係の同期）
uv sync --locked --extra split-test

# 全テストスイートの実行
uv run --locked --extra split-test python scripts/test_standalone.py

# 特定のスイートのみ実行（standard / split / assets / models）
uv run --locked --extra split-test python scripts/test_standalone.py --suite split

# 特定のテストファイルに絞り込んで実行
uv run --locked --extra split-test python scripts/test_standalone.py --suite split --pattern test_worker.py
```

---

## テストの設計原則と運用方針

### 1. 外部拡張の隔離（import 禁止）
- テスト実行時は、登録された全任意拡張の直接 import を禁止しています。
- `isolation.py` がこのインポート禁止ルールを管理し、拡張の有無に依存しない再現性を担保します。
- 共通の拡張連携テストでは、登録情報に基づくロード順序、API ガード、GPU 実行のラップ順序、Secret の分離を検証します。

### 2. 外部依存のモック化
- すべてのテストはローカル素材とモックを用いて実行します。実際のリモートデプロイ、モデルダウンロード、外部 AI API、実 GPU インスタンスの起動は行いません。
- 複数テストで共有する Modal のモックは `split/support.py` に集約しています。
- 通常構成とモデル保存で共通する環境変数の境界値検証は `env_checks.py` で管理します。

### 3. 分離構成（split）特有の検証
- **未 import での環境検証**: `split/test_deployment.py` の子プロセス実行は、モジュール未 import のクリーンな状態でイメージ構成と Secret 定義を確認するためだけに使用します（子プロセス内で他のテストスイートを再実行することはありません）。
- **本体テストの範囲**: 候補環境の依存検査、失敗時のロールバック、CPU/GPU 間の環境バージョン一致確認は split 本体のテストで保証します。
- **外部拡張リポジトリとの境界**: 各任意拡張との実通信、受付ロック、保存形式、再接続テストは各拡張リポジトリ側の `tests/split/` で `scripts/test_split.py --split-root /path/to/modal-comfyui-deploy` を用いて検証します（消費側のテストコードを直接 import しません）。
- **ComfyUI-Modal-Bridge の検証**: CPU 実行制御を担う [ComfyUI-Modal-Bridge](../../extensions/ComfyUI-Modal-Bridge/README.md) は split 本体の一部であるため、本リポジトリ側で検証します。

### 4. テスト追加時の指針
- 新規テストは対象機能のファイルに配置し、既存の別テストケースを import やクラス継承で使い回さないでください（テスト間の不要な結合を防ぐため）。
- 単なるファイルの存在確認や、既存ケースと重複する冗長なテストは追加せず、実際の動作や境界条件を検証する最小限のテストを作成してください。
