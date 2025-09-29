"""Hugging FaceのモデルをModalボリュームへ保存するためのGradio GUI"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
from pathlib import Path
import threading
import os
from typing import Optional, Tuple
from urllib.parse import urlparse

import gradio as gr
from modal import Function, FunctionCall
from modal.exception import ConnectionError as ModalConnectionError
from modal.exception import InvalidError as ModalInvalidError
from modal.exception import NotFoundError as ModalNotFoundError
from modal.exception import RemoteError as ModalRemoteError
from modal.exception import TimeoutError as ModalTimeoutError
from modal_proto import api_pb2

# preserve_model.py を動的に読み込んで、元の関数や定数を再利用する
_MODULE_PATH = Path(__file__).with_name("preserve_model.py")
_SPEC = importlib.util.spec_from_file_location("preserve_model_module", _MODULE_PATH)
_MODULE = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MODULE)

_PRESERVE_FUNCTION = _MODULE.preserve_model
_APP = _MODULE.app
_COMFY_MODEL_SUBDIRS = sorted(_MODULE.COMFY_MODEL_SUBDIRS)

_USE_DEPLOYED = os.getenv("PRESERVE_MODEL_USE_DEPLOYED", "").strip().lower() in {
    "1",
    "true",
    "yes",
}
_DEPLOYED_APP_NAME = os.getenv("PRESERVE_MODEL_DEPLOYED_APP", "preserve-model")
_DEPLOYED_FUNCTION_NAME = os.getenv(
    "PRESERVE_MODEL_DEPLOYED_FUNCTION", "preserve_model"
)


def _run_async(coro):
    """Gradioコールバックから安全に非同期処理を実行する"""
    try:
        return asyncio.run(coro)
    except RuntimeError as err:  # 既存イベントループが動作中の場合に備える
        if "asyncio.run() cannot be called" not in str(err):
            raise
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(coro)
        finally:
            asyncio.set_event_loop(None)
            loop.close()


async def _invoke_preserve(
    repo_id: str,
    filename: str,
    revision: Optional[str],
    destination_subdir: Optional[str],
) -> Tuple[FunctionCall, bool, Optional[dict], Optional[str]]:
    async def _spawn_and_poll(
        spawn_coro, app_id: Optional[str]
    ) -> Tuple[FunctionCall, bool, Optional[dict], Optional[str]]:
        call = await spawn_coro
        result = None
        try:
            result = await call.get.aio(timeout=0.5)
            completed = True
        except (asyncio.TimeoutError, ModalTimeoutError):
            completed = False
        return call, completed, result, app_id

    if _USE_DEPLOYED:
        try:
            remote_function: Function = await Function.from_name.aio(
                _DEPLOYED_APP_NAME, _DEPLOYED_FUNCTION_NAME
            )
        except ModalNotFoundError as exc:  # デプロイ済み関数が存在しない場合
            raise ModalInvalidError(
                f"デプロイ済みのアプリ '{_DEPLOYED_APP_NAME}' または関数 '{_DEPLOYED_FUNCTION_NAME}' が見つかりません"
            ) from exc
        return await _spawn_and_poll(
            remote_function.spawn.aio(
                repo_id=repo_id,
                filename=filename,
                revision=revision or None,
                destination_subdir=destination_subdir or None,
            ),
            None,
        )

    async with _APP.run(detach=True) as running_app:
        return await _spawn_and_poll(
            _PRESERVE_FUNCTION.spawn.aio(
                repo_id=repo_id,
                filename=filename,
                revision=revision or None,
                destination_subdir=destination_subdir or None,
            ),
            running_app.app_id,
        )


def _schedule_app_stop(call: FunctionCall, app_id: Optional[str]) -> None:
    """FunctionCall完了後にAppを停止する補助処理をバックグラウンドで走らせる"""

    if not app_id:
        return

    call_id = getattr(call, "object_id", None)
    if call_id is None:
        return

    async def _wait_and_stop() -> None:
        try:
            fc = await FunctionCall.from_id.aio(call_id)
        except Exception:
            return
        try:
            await fc.get.aio()
        except Exception:
            pass
        client = getattr(fc, "client", None)
        if client is None:
            return
        try:
            await client.stub.AppStop(
                api_pb2.AppStopRequest(
                    app_id=app_id, source=api_pb2.APP_STOP_SOURCE_PYTHON_CLIENT
                )
            )
        except Exception:
            pass

    threading.Thread(target=lambda: asyncio.run(_wait_and_stop()), daemon=True).start()


def _cancel_inflight_call(call: FunctionCall, app_id: Optional[str]) -> None:
    """GUI側が中断された場合にFunctionCallをキャンセルしAppも終了させる"""

    call_id = getattr(call, "object_id", None)
    if call_id is None:
        return

    async def _cancel_and_stop() -> None:
        try:
            await call.cancel.aio(terminate_containers=True)
        except Exception:
            pass

        client = getattr(call, "client", None)
        if client is None or not app_id:
            return
        try:
            await client.stub.AppStop(
                api_pb2.AppStopRequest(
                    app_id=app_id, source=api_pb2.APP_STOP_SOURCE_PYTHON_CLIENT
                )
            )
        except Exception:
            pass

    threading.Thread(
        target=lambda: asyncio.run(_cancel_and_stop()), daemon=True
    ).start()


def _parse_repo_and_filename(raw: str) -> Tuple[str, str, Optional[str]]:
    """入力文字列からリポジトリID・ファイル名・URLで指定された場合のリビジョンを抽出する"""
    value = raw.strip()
    if not value:
        raise ValueError("リポジトリとファイルの指定が空です。")

    # Hugging Faceのresolve URLに対応
    if "huggingface.co" in value:
        parsed = urlparse(value)
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) < 3:
            raise ValueError("URLからリポジトリIDとファイル名を特定できませんでした。")

        special_prefixes = {"datasets", "spaces", "models"}
        prefix = parts[0] if parts[0] in special_prefixes else None
        repo_parts_start = 1 if prefix else 0
        if len(parts) - repo_parts_start < 2:
            raise ValueError("URLからリポジトリIDを特定できませんでした。")

        repo_core_parts = parts[repo_parts_start : repo_parts_start + 2]
        repo_id = "/".join(repo_core_parts)

        filename_parts = parts[repo_parts_start + 2 :]
        if not filename_parts:
            raise ValueError("URLにファイルパスが含まれていません。")

        revision = None
        special_segment = filename_parts[0]
        if len(filename_parts) >= 2 and special_segment in {"resolve", "blob"}:
            revision = filename_parts[1]
            filename_parts = filename_parts[2:]

        if not filename_parts:
            raise ValueError("URLにファイルパスが含まれていません。")

        filename = "/".join(filename_parts)
        if prefix and prefix != "models":
            raise ValueError(
                "現在のGUIはモデルリポジトリのみ対応しています。datasetsやspacesは直接指定してください。"
            )
        return repo_id, filename, revision

    if "::" in value:
        repo_id, filename = (part.strip() for part in value.split("::", 1))
    else:
        parts = value.split()
        if len(parts) < 2:
            raise ValueError(
                "スペースまたは'::'でリポジトリIDとファイルパスを区切ってください。"
            )
        repo_id, filename = parts[0], " ".join(parts[1:])

    if not repo_id or not filename:
        raise ValueError("リポジトリIDとファイルパスの両方を指定してください。")

    return repo_id, filename, None


def download_model(
    repo_and_file: str,
    revision: str,
    destination_subdir: str,
):
    call: Optional[FunctionCall] = None
    app_id: Optional[str] = None
    finished_normally = False
    try:
        try:
            repo_id, filename, revision_from_input = _parse_repo_and_filename(
                repo_and_file
            )
        except ValueError as exc:
            yield str(exc), gr.update(interactive=True)
            return

        def _auto_detect_subdir(filename: str) -> Optional[str]:
            """ファイルパス中から保存先候補を推測する"""

            for part in Path(filename).parts:
                if part in _COMFY_MODEL_SUBDIRS:
                    return part
            return None

        if destination_subdir == "(自動判定)":
            destination_subdir = ""

        chosen_revision = revision_from_input or revision.strip() or "main"
        chosen_subdir = destination_subdir or None
        auto_selected = False
        detected_subdir = _auto_detect_subdir(filename)

        if chosen_subdir is None:
            if detected_subdir is None:
                yield (
                    "ComfyUIの保存先を自動判定できませんでした。\n"
                    "プルダウンから保存先サブディレクトリを選択してください。",
                    gr.update(interactive=True),
                )
                return
            chosen_subdir = detected_subdir
            auto_selected = True

        yield (
            "Modalへリクエストを送信しています...\n"
            f"- リポジトリ: {repo_id.strip()}\n"
            f"- 対象ファイル: {filename.strip()}\n"
            f"- リビジョン: {chosen_revision}\n"
            "この処理には数十秒かかる場合があります。",
            gr.update(interactive=False),
        )

        try:
            call, completed, result_info, app_id = _run_async(
                _invoke_preserve(
                    repo_id=repo_id.strip(),
                    filename=filename.strip(),
                    revision=chosen_revision,
                    destination_subdir=chosen_subdir,
                )
            )
            _schedule_app_stop(call, app_id)
        except ModalConnectionError:
            yield "Modalサーバーに接続できません。CLIでログイン済みか、ネットワーク設定をご確認ください。", gr.update(
                interactive=True
            )
            return
        except ModalInvalidError as exc:
            yield f"Modal側で入力内容が無効と判定されました: {exc}", gr.update(
                interactive=True
            )
            return
        except ModalRemoteError as exc:
            message = str(exc)
            if "404" in message or "Not Found" in message:
                yield (
                    "Hugging Faceで指定されたファイルが見つかりませんでした。\n"
                    "リポジトリID・リビジョン・ファイルパスを再確認してください。",
                    gr.update(interactive=True),
                )
                return
            yield f"リモート実行中にエラーが発生しました: {message}", gr.update(
                interactive=True
            )
            return
        except Exception as exc:  # pylint: disable=broad-except
            yield f"予期しないエラーが発生しました: {exc}", gr.update(interactive=True)
            return

        call_id = getattr(call, "object_id", None)
        if completed:
            status_message = "Modal側でモデル保存処理が完了しました。"
        else:
            followups = [
                "Modal側で処理が継続中です。以下の手順で進捗を確認できます。",
                "- CLI: `modal app list --limit 5` で対象のApp IDを確認し、`modal app logs <App ID>` でログを表示する",
                "- Web: Modalのダッシュボードで該当のFunction Callを開く",
            ]
            if call_id:
                followups.append(
                    f'- Python: `modal.FunctionCall.from_id("{call_id}").get(timeout=120)` で状態を取得する'
                )
            status_message = "\n".join(followups)

        msg_lines = [
            status_message,
            f"- 実行モード: {'デプロイ済み関数' if _USE_DEPLOYED else 'ローカル(app.run)'}",
            f"- リポジトリ: {repo_id.strip()}",
            f"- 対象ファイル: {filename.strip()}",
            f"- リビジョン: {chosen_revision}",
            f"- 保存先サブディレクトリ: {chosen_subdir if not auto_selected else f'自動判定({chosen_subdir})'}",
        ]
        if result_info and completed:
            destination_path = result_info.get("destination")
            size_bytes = result_info.get("size_bytes")
            completed_at = result_info.get("completed_at")
            if destination_path:
                msg_lines.append(f"- 保存先パス: {destination_path}")
            if size_bytes is not None:
                msg_lines.append(f"- 保存サイズ: {size_bytes} バイト")
            if completed_at:
                msg_lines.append(f"- 完了時刻(UTC): {completed_at}")
        if call_id:
            msg_lines.append(f"- コールID: {call_id}")

        finished_normally = True
        yield "\n".join(msg_lines), gr.update(interactive=True)
    finally:
        if call is not None and not finished_normally:
            _cancel_inflight_call(call, app_id)


def _parse_cli_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """CLI引数を解析してGUI起動時の挙動を上書きできるようにする"""

    parser = argparse.ArgumentParser(
        description="Hugging FaceモデルをModalへ保存するGUIを起動します",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--use-deployed",
        dest="use_deployed",
        action="store_true",
        help="デプロイ済みのModal関数を利用して実行します",
    )
    group.add_argument(
        "--use-local",
        dest="use_deployed",
        action="store_false",
        help="ローカルからmodal.App.run()で一時コンテナを起動します",
    )
    parser.set_defaults(use_deployed=None)
    parser.add_argument(
        "--deployed-app-name",
        dest="deployed_app_name",
        help="デプロイ済みアプリの名前を指定します",
    )
    parser.add_argument(
        "--deployed-function-name",
        dest="deployed_function_name",
        help="デプロイ済み関数の名前を指定します",
    )
    parser.add_argument(
        "--share",
        action="store_true",
        help="Gradioの共有URLを有効化します",
    )
    parser.add_argument(
        "--server-port",
        type=int,
        help="GUIを起動するポートを指定します",
    )
    parser.add_argument(
        "--server-name",
        help="GUIをバインドするホスト名またはIPを指定します",
    )
    return parser.parse_args(argv)


def build_interface() -> gr.Blocks:
    with gr.Blocks(title="Modal: Hugging Face モデル取り込み") as demo:
        gr.Markdown(
            """### Hugging FaceのモデルをModalボリュームに保存
`preserve_model.py` の処理をGUIから呼び出します。Modal CLIでログイン済みであることを確認してください。\n\n- デプロイ済み関数を利用したい場合は `--use-deployed` フラグ、または環境変数 `PRESERVE_MODEL_USE_DEPLOYED=1` を指定してください。\n- デフォルト以外のアプリ名・関数名でデプロイしているときは `--deployed-app-name` / `--deployed-function-name` あるいは環境変数 `PRESERVE_MODEL_DEPLOYED_APP` / `PRESERVE_MODEL_DEPLOYED_FUNCTION` で上書きできます。"""
        )

        repo_and_file_input = gr.Textbox(
            label="リポジトリとファイルの指定",
            value="",
            placeholder="Comfy-Org/Qwen-Image-Edit_ComfyUI::split_files/diffusion_models/model.safetensors",
            info="'リポジトリID::ファイルパス'またはスペース区切り、もしくはresolve URLを指定できます",
        )
        revision_input = gr.Textbox(
            label="リビジョン(ブランチ名/タグ/コミット)",
            value="main",
            info="空欄の場合はmainを使用します (URLにresolveが含まれていた場合はその指定を優先)",
        )
        subdir_choices = ["(自動判定)"] + _COMFY_MODEL_SUBDIRS
        destination_dropdown = gr.Dropdown(
            label="保存先サブディレクトリ",
            choices=subdir_choices,
            value="(自動判定)",
            info="空欄の場合はファイルパスから自動で判定します",
        )

        output = gr.Markdown()
        submit_btn = gr.Button("Modalへ保存の実行")

        submit_btn.click(
            fn=download_model,
            inputs=[
                repo_and_file_input,
                revision_input,
                destination_dropdown,
            ],
            outputs=[output, submit_btn],
        )

    return demo


def main(argv: Optional[list[str]] = None) -> None:
    args = _parse_cli_args(argv)

    global _USE_DEPLOYED, _DEPLOYED_APP_NAME, _DEPLOYED_FUNCTION_NAME

    if args.use_deployed is not None:
        _USE_DEPLOYED = args.use_deployed
    if args.deployed_app_name:
        _DEPLOYED_APP_NAME = args.deployed_app_name
    if args.deployed_function_name:
        _DEPLOYED_FUNCTION_NAME = args.deployed_function_name

    launch_kwargs = {}
    if args.share:
        launch_kwargs["share"] = True
    if args.server_port is not None:
        launch_kwargs["server_port"] = args.server_port
    if args.server_name:
        launch_kwargs["server_name"] = args.server_name

    build_interface().launch(**launch_kwargs)


if __name__ == "__main__":
    main()
