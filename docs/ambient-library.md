# Ambientの保存データとワークフロー

Studioが新しい動画の保存・再生・タグ付けを管理します。旧AmbientアプリのAPI・CLIは廃止しましたが、その保存データは削除しません。

## 旧アプリが保存したデータ

| 保存先 | 内容 |
| --- | --- |
| Modal Volume `comfy-outputs` の `ambient/library/` | 保存した動画ファイル |
| Modal Dict `comfyui-ambient-library` | 動画の一覧情報、タグ、削除済みの記録 |
| 既存のsplit Journal・Volume | ワークフローrevision、グラフ、参照画像、実行状態 |

「既存のModalライブラリ」は上記の動画と一覧情報を指します。コード上の保存先であり、実際のデータの有無はこの整理では確認していません。

`SPLIT_EXTENSIONS=ambient`の場合のみ、splitが次の読み取りAPIを提供します。

| API | 動作 |
| --- | --- |
| `GET /ambient/library` | 保存済み動画とタグ情報を返す。削除済み項目は除く |
| `GET /ambient/library/:id/video` | 既存の動画ファイルを読み出す |

Dictがなければ空の一覧を返し、新規作成しません。更新・削除APIはありません。
StudioでModal targetを設定し、**Browse existing Modal library**で一覧を開き、**Copy to this Mac**で必要な動画をコピーできます。
元ファイル・タグには書き込みません。通常のStudioライブラリ表示・再生ではModalへ接続しません。

## ワークフロー

Local・Modalの生成レシピ、編集、Bridgeテンプレート、パネルは独立したComfyUI-Ambientパッケージが提供します。
任意拡張の`/ambient/workflows`、`/ambient/executions`とイベント形式は維持します。
保存済みのrevisionと固定したグラフ・参照画像を引き継ぎ、通常のComfyUIキューはsplit自身が管理します。
