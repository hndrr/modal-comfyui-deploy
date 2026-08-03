"""Local Gradio administration UI for Modal-backed ComfyUI assets."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import tempfile
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Optional, Sequence

import gradio as gr

from asset_manager import (
    ALLOWED_VOLUMES,
    INPUT_VOLUME,
    MODEL_VOLUME,
    OUTPUT_VOLUME,
    AssetEntry,
    AssetManager,
    normalize_volume_path,
)
from preserve_model_gui import build_model_import_panel

PAGE_SIZE = 24
VOLUME_LABELS = {
    INPUT_VOLUME: "Inputs",
    OUTPUT_VOLUME: "Outputs",
    MODEL_VOLUME: "Models",
}
SORT_CHOICES = {
    "名前（昇順）": "name_asc",
    "名前（降順）": "name_desc",
    "更新日時（新しい順）": "modified_desc",
    "更新日時（古い順）": "modified_asc",
    "サイズ（大きい順）": "size_desc",
    "サイズ（小さい順）": "size_asc",
}

ASSET_MANAGER = AssetManager()
MUTATION_LOCK = threading.Lock()


@dataclass(frozen=True)
class BrowserView:
    gallery: list[tuple[str, str]]
    gallery_records: list[dict[str, Any]]
    table: list[list[Any]]
    table_records: list[dict[str, Any]]
    breadcrumb_choices: list[tuple[str, str]]
    current_path: str
    page: int
    page_count: int
    status: str


def create_session_workspace() -> str:
    return tempfile.mkdtemp(prefix="comfy-assets-")


def cleanup_session_workspace(workspace: str) -> None:
    if workspace:
        shutil.rmtree(workspace, ignore_errors=True)


def _human_size(size: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _record(entry: AssetEntry) -> dict[str, Any]:
    record = asdict(entry)
    record["modified_at"] = entry.modified_at.isoformat()
    record["is_directory"] = entry.is_directory
    return record


def _sort_entries(entries: list[AssetEntry], sort_mode: str) -> list[AssetEntry]:
    reverse = sort_mode.endswith("_desc")

    def sort_key(item: AssetEntry):
        if sort_mode.startswith("modified"):
            return item.modified_at
        if sort_mode.startswith("size"):
            return item.size
        return item.name.casefold()

    sorted_entries = sorted(entries, key=sort_key, reverse=reverse)
    return sorted(sorted_entries, key=lambda item: not item.is_directory)


def _breadcrumb(path: str) -> list[tuple[str, str]]:
    choices: list[tuple[str, str]] = [("/", "")]
    parts: list[str] = []
    for part in PurePosixPath(path).parts if path else ():
        parts.append(part)
        value = PurePosixPath(*parts).as_posix()
        choices.append((f"/ {value}", value))
    return choices


def _cached_asset_path(workspace: str, entry: AssetEntry) -> Path:
    digest = hashlib.sha256(f"{entry.volume}:{entry.path}:{entry.modified_at}".encode()).hexdigest()
    suffix = PurePosixPath(entry.path).suffix
    return Path(workspace) / "cache" / f"{digest}{suffix}"


def _local_asset(entry: AssetEntry, workspace: str) -> Path:
    destination = _cached_asset_path(workspace, entry)
    if not destination.exists():
        ASSET_MANAGER.download_listed_asset(entry, destination)
    return destination


def build_browser_view(
    volume: str,
    path: str,
    search: str,
    sort_mode: str,
    page: int,
    workspace: str,
) -> BrowserView:
    normalized_path = normalize_volume_path(path, allow_root=True)
    entries = ASSET_MANAGER.list_assets(volume, normalized_path)
    query = search.strip().casefold()
    if query:
        entries = [entry for entry in entries if query in entry.name.casefold()]
    entries = _sort_entries(entries, sort_mode)

    if volume in {INPUT_VOLUME, OUTPUT_VOLUME}:
        gallery_entries = [entry for entry in entries if entry.media_type == "image"]
        table_entries = [entry for entry in entries if entry.media_type != "image"]
    else:
        gallery_entries = []
        table_entries = entries
    page_count = max(1, (len(gallery_entries) + PAGE_SIZE - 1) // PAGE_SIZE)
    safe_page = min(max(int(page or 1), 1), page_count)
    page_start = (safe_page - 1) * PAGE_SIZE
    visible_gallery_entries = gallery_entries[page_start : page_start + PAGE_SIZE]

    gallery: list[tuple[str, str]] = []
    gallery_records: list[dict[str, Any]] = []
    preview_errors = 0
    for entry in visible_gallery_entries:
        try:
            gallery.append((_local_asset(entry, workspace).as_posix(), entry.name))
            gallery_records.append(_record(entry))
        except Exception:
            preview_errors += 1

    table = [
        [
            "フォルダ" if entry.is_directory else entry.media_type,
            entry.name,
            "—" if entry.is_directory else _human_size(entry.size),
            entry.modified_at.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            entry.path,
        ]
        for entry in table_entries
    ]
    status = f"{VOLUME_LABELS[volume]}: {len(entries)}件"
    if gallery_entries:
        status += f" / 画像 {len(gallery_entries)}件（{safe_page}/{page_count}ページ）"
    if preview_errors:
        status += f" / {preview_errors}件のサムネイル取得に失敗"

    return BrowserView(
        gallery=gallery,
        gallery_records=gallery_records,
        table=table,
        table_records=[_record(entry) for entry in table_entries],
        breadcrumb_choices=_breadcrumb(normalized_path),
        current_path=normalized_path,
        page=safe_page,
        page_count=page_count,
        status=status,
    )


def refresh_browser(
    volume: str,
    path: str,
    search: str,
    sort_mode: str,
    page: int,
    workspace: str,
):
    try:
        view = build_browser_view(volume, path, search, sort_mode, page, workspace)
        return (
            view.gallery,
            view.table,
            view.gallery_records,
            view.table_records,
            gr.update(choices=view.breadcrumb_choices, value=view.current_path),
            view.current_path,
            view.page,
            f"{view.page} / {view.page_count}",
            gr.update(interactive=view.page > 1),
            gr.update(interactive=view.page < view.page_count),
            view.status,
        )
    except Exception as exc:
        return (
            [],
            [],
            [],
            [],
            gr.update(choices=[("/", "")], value=""),
            "",
            1,
            "1 / 1",
            gr.update(interactive=False),
            gr.update(interactive=False),
            f"エラー: {exc}",
        )


def _selected_outputs(record: Optional[dict[str, Any]], workspace: str):
    if not record:
        return (
            None,
            "資産を選択してください。",
            gr.update(value=None, visible=False),
            gr.update(value=None, visible=False),
            gr.update(value=None, visible=False),
            gr.update(value=None, visible=False),
            gr.update(visible=False),
            gr.update(visible=False),
            gr.update(visible=False),
            None,
            "",
            None,
            "",
            gr.update(visible=False),
        )

    kind = record["kind"]
    info = (
        f"**{record['path']}**  \n"
        f"種類: {kind} / サイズ: {_human_size(record['size']) if kind != 'directory' else '—'}  \n"
        f"更新日時: {record['modified_at']}"
    )
    local_path: Optional[str] = None
    if kind != "directory" and record["media_type"] in {"image", "video", "audio"}:
        entry = AssetEntry(
            volume=record["volume"],
            path=record["path"],
            name=record["name"],
            kind=record["kind"],
            size=record["size"],
            modified_at=datetime.fromisoformat(record["modified_at"]),
            media_type=record["media_type"],
        )
        try:
            local_path = _local_asset(entry, workspace).as_posix()
        except Exception as exc:
            info += f"  \nプレビュー取得エラー: {exc}"

    media_type = record["media_type"]
    return (
        record,
        info,
        gr.update(value=local_path if media_type == "image" else None, visible=bool(local_path and media_type == "image")),
        gr.update(value=local_path if media_type == "video" else None, visible=bool(local_path and media_type == "video")),
        gr.update(value=local_path if media_type == "audio" else None, visible=bool(local_path and media_type == "audio")),
        gr.update(value=local_path, visible=bool(local_path)),
        gr.update(visible=kind != "directory" and not local_path),
        gr.update(visible=kind == "directory"),
        gr.update(visible=True),
        record["volume"],
        record["path"],
        None,
        "",
        gr.update(visible=False),
    )


def select_gallery(records: list[dict[str, Any]], workspace: str, evt: gr.SelectData):
    index = int(evt.index)
    record = records[index] if 0 <= index < len(records) else None
    return _selected_outputs(record, workspace)


def select_table(records: list[dict[str, Any]], workspace: str, evt: gr.SelectData):
    index = evt.index[0] if isinstance(evt.index, (tuple, list)) else evt.index
    index = int(index)
    record = records[index] if 0 <= index < len(records) else None
    return _selected_outputs(record, workspace)


def reset_selection():
    return _selected_outputs(None, "")


def prepare_download(selected: Optional[dict[str, Any]], workspace: str):
    if not selected or selected.get("kind") == "directory":
        raise gr.Error("ダウンロードするファイルを選択してください。")
    entry = AssetEntry(
        volume=selected["volume"],
        path=selected["path"],
        name=selected["name"],
        kind=selected["kind"],
        size=selected["size"],
        modified_at=datetime.fromisoformat(selected["modified_at"]),
        media_type=selected["media_type"],
    )
    try:
        local_path = _local_asset(entry, workspace).as_posix()
        return gr.update(value=local_path, visible=True), "ダウンロードの準備が完了しました。"
    except Exception as exc:
        raise gr.Error(f"ダウンロード準備に失敗しました: {exc}") from exc


def change_volume(volume: str):
    return "", 1, "", volume, ""


def navigate_to(path: str):
    normalized = normalize_volume_path(path, allow_root=True)
    return normalized, 1, normalized


def navigate_parent(path: str):
    normalized = normalize_volume_path(path, allow_root=True)
    parent = PurePosixPath(normalized).parent.as_posix() if normalized else ""
    destination = "" if parent == "." else parent
    return destination, 1, destination


def open_selected_folder(selected: Optional[dict[str, Any]]):
    if not selected or selected.get("kind") != "directory":
        raise gr.Error("フォルダを選択してください。")
    return selected["path"], 1, selected["path"]


def change_page(page: int, delta: int):
    return max(1, int(page or 1) + delta)


def upload_selected_files(
    files: Optional[Sequence[str]],
    volume: str,
    destination: str,
    overwrite: bool,
) -> str:
    try:
        with MUTATION_LOCK:
            uploaded = ASSET_MANAGER.upload_assets(
                volume,
                destination,
                list(files or []),
                overwrite,
            )
        return "アップロード完了: " + ", ".join(uploaded)
    except Exception as exc:
        raise gr.Error(f"アップロードに失敗しました: {exc}") from exc


def move_selected_asset(
    selected: Optional[dict[str, Any]],
    destination_volume: str,
    destination_path: str,
    overwrite: bool,
) -> str:
    if not selected:
        raise gr.Error("移動する資産を選択してください。")
    try:
        with MUTATION_LOCK:
            moved_to = ASSET_MANAGER.move_asset(
                selected["volume"],
                selected["path"],
                destination_volume,
                destination_path,
                overwrite,
            )
        return f"移動完了: {destination_volume}:{moved_to}"
    except Exception as exc:
        raise gr.Error(f"移動に失敗しました: {exc}") from exc


def request_delete(selected: Optional[dict[str, Any]]):
    if not selected:
        raise gr.Error("削除する資産を選択してください。")
    recursive_note = " 配下を含めて再帰的に削除します。" if selected["kind"] == "directory" else ""
    message = (
        f"⚠️ `{selected['volume']}:{selected['path']}` を完全に削除します。"
        f"{recursive_note}この操作は取り消せません。"
    )
    return dict(selected), message, gr.update(visible=True)


def cancel_delete():
    return None, "", gr.update(visible=False)


def confirm_delete(candidate: Optional[dict[str, Any]]) -> str:
    if not candidate:
        raise gr.Error("削除確認の対象がありません。もう一度選択してください。")
    try:
        with MUTATION_LOCK:
            ASSET_MANAGER.delete_asset(
                candidate["volume"],
                candidate["path"],
                recursive=candidate["kind"] == "directory",
            )
        return f"削除完了: {candidate['volume']}:{candidate['path']}"
    except Exception as exc:
        raise gr.Error(f"削除に失敗しました: {exc}") from exc


def _parse_cli_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Modal ComfyUI資産管理画面を起動します")
    parser.add_argument("--server-port", type=int, default=7860)
    parser.add_argument("--server-name", default="127.0.0.1")
    return parser.parse_args(argv)


def build_interface() -> gr.Blocks:
    css = """
    .asset-manager-root { max-width: 1400px; margin: 0 auto; }
    .asset-browser-pane, .asset-side-pane { gap: 0.65rem !important; }
    .asset-side-pane {
        border-left: 1px solid var(--border-color-primary, #e5e7eb);
        padding-left: 1rem;
    }
    .asset-toolbar .wrap { gap: 0.5rem; }
    .asset-page-label {
        text-align: center;
        display: flex;
        align-items: center;
        justify-content: center;
        min-height: 2rem;
    }
    .asset-section-title { margin: 0.15rem 0 0 !important; }
    .asset-section-title h3, .asset-section-title h4 {
        margin: 0.25rem 0 !important;
        font-size: 1rem;
    }
    """
    with gr.Blocks(title="Modal ComfyUI Asset Manager", css=css, elem_classes=["asset-manager-root"]) as demo:
        workspace = gr.State(
            value=create_session_workspace,
            time_to_live=60 * 60 * 12,
            delete_callback=cleanup_session_workspace,
        )
        current_path = gr.State("")
        page = gr.State(1)
        selected = gr.State(None)
        delete_candidate = gr.State(None)
        gallery_records = gr.State([])
        table_records = gr.State([])

        gr.Markdown(
            """# Modal ComfyUI Asset Manager
`comfy-model`・`comfy-inputs`・`comfy-outputs` を、Modal CLIの認証情報で管理します。削除は完全削除です。"""
        )
        with gr.Tabs():
            with gr.Tab("資産ブラウザ"):
                with gr.Row(equal_height=False):
                    with gr.Column(scale=7, min_width=520, elem_classes=["asset-browser-pane"]):
                        with gr.Row(elem_classes=["asset-toolbar"]):
                            volume = gr.Radio(
                                choices=[
                                    (VOLUME_LABELS[name], name)
                                    for name in sorted(ALLOWED_VOLUMES)
                                ],
                                value=INPUT_VOLUME,
                                label="Volume",
                                scale=3,
                            )
                            breadcrumb = gr.Dropdown(
                                choices=[("/", "")],
                                value="",
                                label="現在のフォルダ",
                                interactive=True,
                                scale=4,
                            )
                            with gr.Column(scale=1, min_width=100):
                                parent_button = gr.Button("一つ上へ", size="sm")
                                refresh_button = gr.Button("更新", size="sm")

                        with gr.Row():
                            search = gr.Textbox(
                                label="現在のフォルダを検索",
                                scale=3,
                                show_label=True,
                            )
                            sort_mode = gr.Dropdown(
                                choices=[
                                    (label, value) for label, value in SORT_CHOICES.items()
                                ],
                                value="name_asc",
                                label="並べ替え",
                                scale=2,
                            )
                        browser_status = gr.Markdown()
                        gallery = gr.Gallery(
                            label="画像",
                            columns=4,
                            rows=3,
                            height=360,
                            object_fit="contain",
                            show_download_button=False,
                        )
                        with gr.Row():
                            previous_page = gr.Button("前へ", interactive=False, size="sm", scale=1)
                            with gr.Column(scale=1, min_width=80):
                                page_label = gr.Markdown(
                                    "1 / 1",
                                    elem_classes=["asset-page-label"],
                                )
                            next_page = gr.Button("次へ", interactive=False, size="sm", scale=1)
                        table = gr.Dataframe(
                            headers=["種類", "名前", "サイズ", "更新日時", "パス"],
                            datatype=["str", "str", "str", "str", "str"],
                            value=[],
                            interactive=False,
                            show_search="search",
                            show_row_numbers=False,
                            label="フォルダ・その他のファイル",
                            max_height=280,
                        )

                    with gr.Column(scale=4, min_width=340, elem_classes=["asset-side-pane"]):
                        gr.Markdown("### 選択した資産", elem_classes=["asset-section-title"])
                        selected_info = gr.Markdown("資産を選択してください。")
                        preview_image = gr.Image(
                            label="画像プレビュー",
                            visible=False,
                            height=220,
                        )
                        preview_video = gr.Video(label="動画プレビュー", visible=False)
                        preview_audio = gr.Audio(label="音声プレビュー", visible=False)
                        with gr.Row():
                            download_button = gr.DownloadButton("ダウンロード", visible=False)
                            prepare_download_button = gr.Button(
                                "ダウンロードを準備", visible=False, size="sm"
                            )
                            open_folder_button = gr.Button(
                                "フォルダを開く", visible=False, size="sm"
                            )

                        with gr.Group(visible=False) as action_group:
                            gr.Markdown(
                                "#### 名前変更・移動",
                                elem_classes=["asset-section-title"],
                            )
                            with gr.Row():
                                destination_volume = gr.Dropdown(
                                    choices=[
                                        (VOLUME_LABELS[name], name)
                                        for name in sorted(ALLOWED_VOLUMES)
                                    ],
                                    label="移動先Volume",
                                    scale=2,
                                )
                                destination_path = gr.Textbox(
                                    label="移動先パス",
                                    info="ファイル名を含む相対パス",
                                    scale=3,
                                )
                            with gr.Row():
                                move_overwrite = gr.Checkbox(
                                    label="移動先を上書き", value=False, scale=2
                                )
                                move_button = gr.Button(
                                    "名前変更・移動", scale=2, size="sm"
                                )
                                delete_button = gr.Button(
                                    "削除", variant="stop", scale=2, size="sm"
                                )

                        with gr.Group(visible=False) as delete_group:
                            delete_message = gr.Markdown()
                            with gr.Row():
                                confirm_delete_button = gr.Button(
                                    "完全に削除する", variant="stop", scale=2
                                )
                                cancel_delete_button = gr.Button("キャンセル", scale=1)

                        with gr.Accordion("ローカルファイルを追加", open=True):
                            upload_files = gr.File(
                                label="ファイル",
                                file_count="multiple",
                                type="filepath",
                                height=120,
                            )
                            upload_destination = gr.Textbox(
                                label="保存先フォルダ",
                                info="Modelsではモデル種別ディレクトリを指定",
                            )
                            with gr.Row():
                                upload_overwrite = gr.Checkbox(
                                    label="同名を上書き", value=False, scale=2
                                )
                                upload_button = gr.Button("アップロード", scale=2)
                        operation_status = gr.Markdown()

            with gr.Tab("Hugging Faceからモデル追加"):
                build_model_import_panel(show_standalone_options=False)

        refresh_inputs = [volume, current_path, search, sort_mode, page, workspace]
        refresh_outputs = [
            gallery,
            table,
            gallery_records,
            table_records,
            breadcrumb,
            current_path,
            page,
            page_label,
            previous_page,
            next_page,
            browser_status,
        ]
        selection_outputs = [
            selected,
            selected_info,
            preview_image,
            preview_video,
            preview_audio,
            download_button,
            prepare_download_button,
            open_folder_button,
            action_group,
            destination_volume,
            destination_path,
            delete_candidate,
            delete_message,
            delete_group,
        ]

        demo.load(refresh_browser, inputs=refresh_inputs, outputs=refresh_outputs)
        refresh_button.click(refresh_browser, inputs=refresh_inputs, outputs=refresh_outputs)
        search.submit(refresh_browser, inputs=refresh_inputs, outputs=refresh_outputs)
        sort_mode.change(refresh_browser, inputs=refresh_inputs, outputs=refresh_outputs)

        volume.change(
            change_volume,
            inputs=volume,
            outputs=[current_path, page, search, destination_volume, upload_destination],
        ).then(refresh_browser, inputs=refresh_inputs, outputs=refresh_outputs).then(
            reset_selection, outputs=selection_outputs
        )
        breadcrumb.change(
            navigate_to,
            inputs=breadcrumb,
            outputs=[current_path, page, upload_destination],
        ).then(refresh_browser, inputs=refresh_inputs, outputs=refresh_outputs).then(
            reset_selection, outputs=selection_outputs
        )
        parent_button.click(
            navigate_parent,
            inputs=current_path,
            outputs=[current_path, page, upload_destination],
        ).then(refresh_browser, inputs=refresh_inputs, outputs=refresh_outputs).then(
            reset_selection, outputs=selection_outputs
        )
        open_folder_button.click(
            open_selected_folder,
            inputs=selected,
            outputs=[current_path, page, upload_destination],
        ).then(refresh_browser, inputs=refresh_inputs, outputs=refresh_outputs).then(
            reset_selection, outputs=selection_outputs
        )
        previous_page.click(
            lambda value: change_page(value, -1), inputs=page, outputs=page
        ).then(refresh_browser, inputs=refresh_inputs, outputs=refresh_outputs)
        next_page.click(
            lambda value: change_page(value, 1), inputs=page, outputs=page
        ).then(refresh_browser, inputs=refresh_inputs, outputs=refresh_outputs)

        gallery.select(
            select_gallery,
            inputs=[gallery_records, workspace],
            outputs=selection_outputs,
        )
        table.select(
            select_table,
            inputs=[table_records, workspace],
            outputs=selection_outputs,
        )
        prepare_download_button.click(
            prepare_download,
            inputs=[selected, workspace],
            outputs=[download_button, operation_status],
        )

        upload_button.click(
            upload_selected_files,
            inputs=[upload_files, volume, upload_destination, upload_overwrite],
            outputs=operation_status,
        ).then(refresh_browser, inputs=refresh_inputs, outputs=refresh_outputs)
        move_button.click(
            move_selected_asset,
            inputs=[selected, destination_volume, destination_path, move_overwrite],
            outputs=operation_status,
        ).then(refresh_browser, inputs=refresh_inputs, outputs=refresh_outputs).then(
            reset_selection, outputs=selection_outputs
        )
        delete_button.click(
            request_delete,
            inputs=selected,
            outputs=[delete_candidate, delete_message, delete_group],
        )
        cancel_delete_button.click(
            cancel_delete,
            outputs=[delete_candidate, delete_message, delete_group],
        )
        confirm_delete_button.click(
            confirm_delete,
            inputs=delete_candidate,
            outputs=operation_status,
        ).then(cancel_delete, outputs=[delete_candidate, delete_message, delete_group]).then(
            refresh_browser, inputs=refresh_inputs, outputs=refresh_outputs
        ).then(reset_selection, outputs=selection_outputs)

    return demo


def main(argv: Optional[list[str]] = None) -> None:
    args = _parse_cli_args(argv)
    build_interface().queue(default_concurrency_limit=1).launch(
        server_name=args.server_name,
        server_port=args.server_port,
        share=False,
    )


if __name__ == "__main__":
    main()
