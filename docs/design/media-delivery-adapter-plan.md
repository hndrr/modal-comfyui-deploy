# メディア配信アダプタ

実装とローカル連携確認まで対応。本番への反映は別作業とする。画像・音声・動画・GLB/SPZを、任意の外部サービスから本人用URLとMCPの表示用バイト列として取得する。

## 本体と別リポジトリの境界

本体が持つのは接続設定、以下のAPI契約、原本の照会・転送、MCP応答、プレイヤー連携まで。配信側の保存方式、保持方針、無料枠管理、手元ファイル、共有・公開、ギャラリー、デプロイは提供側リポジトリで管理する。提供側のコードやパッケージを本体へ取り込まない。

任意のWorker Secret `MEDIA_DELIVERY` を設定する。

```json
{"origin":"https://<MEDIA_DELIVERY_HOST>","source_id":"<SOURCE_ID>","token":"<SERVICE_TOKEN>"}
```

未設定では既存経路を使い、外部サービスへの通信は0件。設定が不正ならMCP入口で拒否する。HTTPSの単一originに固定し、パス・ポート・URL内認証情報・クエリは禁止。リダイレクトを追わず、返された本人用URLも設定originの `/media/{media_id}` と一致することを検証する。

本体のOAuth本人認証を通過した後に利用する。サービス間BearerはWorker内だけで使い、利用者のCookie・JWT・Modal認証情報を外部へ渡さない。ワークフローやプロンプトも送らない。本人用URLは公開リンクではなく、配信サービス側のブラウザ認証が必要。

## 利用するAPI契約

すべて設定originに `Authorization: Bearer <SERVICE_TOKEN>` で接続する。`source_id` は提供側トークンの接続元と一致させる。

| API | 入力 | 応答・制約 |
| --- | --- | --- |
| `POST /v1/media/lookup` | `source_id`, `references: [{output_path, source_etag}]` | 最大24参照。入力順の `results` |
| `POST /v1/media` | `source_id`, `output_path`, `source_etag`, `filename`, `kind`, `mime_type`, `size_bytes` | `ready` / `pending` / `upload_required` |
| `PUT /v1/media/{media_id}/content` | バイナリ、`Content-Type`, `Content-Length`, `X-Upload-Id` | 登録で受け取った `upload_id` を使用。完了後のコピー情報 |
| `GET /v1/media/{media_id}/content` | `Range`, `If-Range`（配信ETag） | `206`, `Content-Range`, `ETag`, `Content-Type` と実バイト数を照合 |

状態は以下を区別する。

- `ready`: `media_id`, `url`, `etag`, `mime_type`, `size_bytes`, `expires_at`（null可）, `cleansed`。
- `missing`: `reason` は `not_found` / `expired` / `upload_failed`。
- `pending`: `media_id`, `retry_after_seconds`。まだ利用可能なURLを返さない。
- `upload_required`: `media_id`, `upload_id`。この時だけ本体を転送する。

エラーはHTTP非2xxと `error.code`、任意の `error.retry_after_seconds`。本文や上流の例外をMCPへそのまま転記しない。現在の契約は1ファイル25 MiBまで。GLBは `kind: "3d"` / `model/gltf-binary`、SPZは `kind: "3d"` / `application/octet-stream`。MOV/MKVなど提供側非対応形式や上限超過は既存の原本取得へ戻す。

原本の識別は `source_id + output_path + source_etag` に統一する。ジョブID・種類別番号からファイルへの解決は本体で行い、提供側にはジョブ別名を登録しない。同じ参照の再登録は既存コピー・転送予約を再利用する。

## 過去作品の取得

1. `get_image` / `get_3d` はファイル名、`get_audio` / `get_video` は完了履歴と種類別番号から原本を解決する。
2. 原本をHEADし、ETag・サイズ・MIMEを取得する。`output_path` は出力ルート相対パスで、隠しパス・パス逸脱を拒否する。
3. lookupがreadyならコピーを再利用する。原本の本体GET・PUTは0件。版確認のHEADは行う。
4. pendingなら再試行情報を返し、登録を繰り返さない。missingなら登録して現在の状態・予約を確認する。upload_requiredの時だけ原本をGETし、ストリームをPUTする。GETは原本ETagを `If-Range` に指定し、取得ヘッダーを照合する。転送後にもHEADで原本の版を確認する。
5. readyの本人用URLを本文と `structuredContent.url` に返す。原本ETagとコピーETagは別物で、表示にはAPIが返した現在のコピーのMIME・サイズ・ETagを使う。提供側でメタデータ除去により置換済みでも原本を上書き転送しない。

PUTの応答が失われた場合も、次回lookup/登録から再開する。生成を再投入しない。配信用CPU設定 `SPLIT_MEDIA_ORIGIN` があれば原本取得はそのCPUを使い、なければ既存のComfyUI経路を使う。

## 新規生成と一覧

`enqueue_workflow` の受付・承認待ちは変更しない。`get_job_progress` がcompletedの時だけ、出力一覧の最大4件を同じ準備処理へ渡す。`outputs` と `outputs_next_offset` を返し、続きは任意引数 `outputs_offset` で取得する。失敗した出力にも原本の認証付きURLと理由を残し、completedや成功済みの出力を消さない。準備中の項目は同じページを再照会する。

`list_outputs` は原本HEADと最大24件ずつのlookupだけを行う。HEADは最大6件ずつ並行し、completedの出力準備でもlookupをまとめる。準備済みコピーにURL・ETag・サイズ・MIMEを追加し、欠落から登録・転送は始めない。missingの理由は `not_found` / `expired` / `upload_failed` を保持する。

外部APIの制約としてファイル名255文字、サブフォルダ768文字、ETag256文字を検証する。原本経路の制約と異なるため、外れた項目だけを配信不可とし、一覧全体や他の出力を失敗させない。原本の `audio/x-wav`・`audio/vnd.wave`・`audio/x-flac`・`audio/opus`・`application/ogg` は対応する契約MIMEへ正規化する。任意のMIME不一致は許容しない。

設定有効時だけ、コピー作成が発生する4取得ツールと `get_job_progress` に説明を追加し、`readOnlyHint: false`, `idempotentHint: true`, `openWorldHint: true` とする。

## プレイヤーと失敗時の動作

画像は既存のインライン表示を維持する。音声・動画・3Dの初回応答に `delivery_ref`（ファイル名・サブフォルダ・media_id・配信ETag・MIME・サイズ、音声・動画はprompt_id・indexも）を付ける。プレイヤーはこの参照を変更せず内部の `read_audio_part` / `read_video_part` / `read_3d_part` へ渡し、続きはModal履歴や原本を再取得しない。別ジョブや番号の参照は拒否し、対応付けのない旧参照は作品の再取得を案内する。CLIの音声・動画のチャンク取得でも `delivery_ref` を引き継ぐ。

画像のインライン表示と音声・動画の先頭取得では、コピーのMIMEに対応する形式ヘッダーを検証する。音声・動画のプレイヤー応答はコピーの先頭16バイトだけを取得する。この確認は、壊れた／実体のないreadyコピーから、バイトをまだ返していない初回取得で原本へ戻すために維持する。小さなチャンクは、ヘッダーが含まれる範囲で検証する。GLB/SPZの解析は既存3Dビューアーの境界で行う。

チャンクの版・サイズ・Rangeが変わったらエラーにし、作品の再取得を案内する。原本のバイトへ途中で切り替えない。ブラウザはサービス間トークンを持たず、配信URLを直接fetchしたり `/api/view` に書き換えたりしない。本人用ページへのリンクとして扱う。3Dの描画は本体の既存ビューアーを使う。

準備中は再試行情報を返す。一覧とcompleted出力準備のHEAD・lookup・登録・転送で発生した一時障害にも、提供された再試行秒数を保持する。恒久的な検証エラーに準備中フラグや再試行秒数を付けない。

初回準備の容量・形式制限、サービス障害、および参照なし初回取得でのコピー読取失敗は理由を添えて既存の原本取得へ戻す。原本応答には `delivery_route: "original"` を付ける。CLIとプレイヤーは、原本ならこの取得経路、コピーなら `delivery_ref` を続きの要求へ変更せず引き継ぐ。両方の同時指定は拒否する。これにより、コピーが途中で準備できた場合も原本取得を維持し、コピーが途中で失われた場合も原本へ切り替えない。

CLIの `get_audio` / `get_video` は、`byte_offset > 0` でどちらもない要求を `delivery_reference_required` とし、0からの再取得を案内する。現在のlookup状態から過去の取得経路を推測しない。内部の `read_*_part` は旧プレイヤーとの互換性のため、参照・取得経路のない要求を全offsetで原本経路として扱う。キャッシュ済み旧プレイヤーも最初から最後まで原本を読み、配信コピーの準備状態に左右されない。3D原本の継続取得には従来どおり `etag` を必須とする。現行プレイヤーは原本の `delivery_route` またはコピーの `delivery_ref` を後続へ渡す。明示したコピー参照の読取失敗はエラーにし、コピーのバイトと原本を混ぜない。

Secretを削除すると新しいMCP応答は既存経路へ戻る。既存コピーや外部サービスのリンクを自動削除しない。配信側の停止・削除は提供側で行う。

## 検証

通常は `worker/tests/media-delivery.test.mjs` と既存の認証・メディア・プレイヤーテストで完結する。未設定・設定解除、初回転送、再利用、期限切れ、pending、応答消失、原本変更、コピー置換、容量制限、接続先固定、秘密情報、最大24件lookup、4出力ページングを確認する。

実APIとの確認は[独立したローカル連携手順](../../tests/README.md#外部配信アダプタのローカル連携確認)を明示実行する。本体のビルド・通常テスト・CIは別repoへ依存しない。ローカルAPIの認証拒否、PNG/WAV/MP4/GLB/SPZの登録とMCP応答、コピー再利用、チャンクの完全一致、ブラウザ表示・再生を確認する。本番Accessや実際のMCPホスト内での表示確認はデプロイ時の作業とする。

## 関連資料

[保存データの配信設計](split-media-delivery.md)、[MCP経路図](split-mcp-routing.md)、[設定手順](../mcp/setup.md)。

外部配信が有効な `list_outputs` は1ページ最大32件に制限し、`next_offset` で続ける。原本HEAD、24件ずつのlookup、認証・起動確認を合わせて [Workers Freeのサブリクエスト上限](https://developers.cloudflare.com/workers/platform/limits/#subrequests) に収めるための上限であり、登録・転送は行わない。
