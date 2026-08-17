# 既知の不具合と注意点

Modal 上で ComfyUI を動かすときの、未解決の問題と注意点をまとめています。対処済みのものは各機能のドキュメント側に書いてあります。

## 未解決

### Manager の再起動が 500 を返す

**症状**: ノードインストール後の再起動で、次のようなログが出ます。

```text
SystemExit: 0                                  ← Manager の restart が exit(0) を呼ぶ
Terminating runner due to @web_server connection issue: Cannot connect to host ...:8000
POST /api/v2/manager/reboot -> 500 Internal Server Error
ValueError: I/O operation on closed file       ← 終了後のログ書き込み。無害
```

**原因**: Manager はプロセスを `exit(0)` で落として再起動する作りですが、Modal では ComfyUI が死ぬとコンテナごと終了します。応答を返す相手がいなくなるため 500 になります。

**影響**: 保存と API 応答で結果が分かれます。

- **ノードの保存は成功します**。`custom_nodes` Volume に残り、次のアクセスで起動する新しいコンテナでも読み込まれます（実測で 39 ノードの読み込みを確認）
- **再起動 API は失敗を返します**。画面には再起動が失敗したように表示され、この API を叩く自動化クライアントはエラーとして扱います

**回避**: 再起動後にページを再読み込みしてください。

**根治案**: `comfy launch` を監視し、落ちたら同じコンテナ内で起動し直す仕組みを入れると、Manager の再起動ボタンが本来の挙動になり、コールドスタートも避けられます。起動失敗時の無限ループ対策が必要なため未実装です。

### Manager で入れたノードの Python 依存はコンテナ内にしか残らない

`custom_nodes` は Volume なのでノード本体は残りますが、`pip install` された依存は `site-packages`（コンテナ内）に入るため、コンテナが入れ替わると消えます。

実測では、コンテナ入れ替え後も KJNodes が正常に読み込まれました。Manager が起動時に依存を入れ直しているためと見られますが、**挙動は確認していません**。恒久的に使うノードは `comfyapp.py` の `NODES` に足してイメージへ焼く方が確実です。

### SageAttention がブランチ追従で再現性がない

`SAGEATTENTION_REF = "abi3_stable"` はブランチ名なので、`git clone --branch` はその時点の HEAD を引きます。torch は wheel URL で固定されているのに対し、SageAttention だけが動きます。

そのため**同じコードでもビルドする日によって結果が変わります**。実際に一度、ブランチが進んだ先の実装がビルドできず（`USE_CUDA` 未定義）、対処が必要になりました。

恒久対処は、動作確認できた commit SHA への固定です。今は動いているので急ぎではありません。

## 未検証

### `COMFYUI_SAGE_ATTENTION` と `COMFYUI_CLI_ARGS` がコンテナに届いていない疑い

この 2 つはコンテナ内の `_build_launch_command()` で `os.environ` から読んでいます。`.env` はコンテナに送られない（送られるのは `comfyapp.py` 1 ファイルだけ）ため、**`.env` に `COMFYUI_SAGE_ATTENTION=off` と書いても既定の `on` のまま動いている**可能性があります。`COMFYUI_CLI_ARGS` も常に空として扱われている可能性があります。

既定値と一致している限り表面化しないため、今まで問題になっていません。決着させるには次を実行し、`--use-sage-attention` が消えるかを見ます。

```bash
COMFYUI_SAGE_ATTENTION=off ./scripts/modal.sh deploy comfyapp.py
curl -s https://<workspace>--comfyui-ui.modal.run/system_stats | grep -o '"argv":[^]]*]'
```

消えれば推測は誤り、残っていれば届いていません。修正すると**挙動が変わる**（`off` にしていた人は本当に無効になる）ため、確認してから直す方針です。

### `COMFYUI_CLI_ARGS` でディレクトリを差し替えると永続化が壊れる

`--base-directory` や `--user-directory` を指定すると ComfyUI の user ディレクトリが変わります。Volume の接続先と ComfyUI-Manager の `config.ini` の位置は既定の配置（`<comfy_root>/user`）を前提にしているため、workflow が永続化されなくなったり `COMFYUI_MANAGER_INSTALL` が効かなくなったりします。

これらの引数を検出した場合は起動時に警告を出しますが、動作は止めません。`--models-directory` / `--output-directory` / `--input-directory` / `--temp-directory` も同様です。

## 運用上の注意

- **`COMFYUI_REQUIRES_PROXY_AUTH=off` は認証なし**です。Modal の直 URL を知っている人は誰でも開けます。ここに `COMFYUI_MANAGER_INSTALL=on` を併用し、かつ実効の `security_level` がインストールを許す値（`normal` / `normal-` / `weak`）だと、**その人が任意のコードをインストールできる状態**になります。`config.ini` で `security_level = strong` を明示している場合は `COMFYUI_MANAGER_INSTALL=on` でもインストールは拒否されます（起動時に警告が出ます）
- Volume・Secret・Proxy Auth トークン・デプロイ URL はすべて**ワークスペース単位**です。アカウントを切り替えたときの影響は [modal-profiles.md](modal-profiles.md) を参照してください
