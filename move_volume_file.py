import argparse
import shutil
import sys
from pathlib import Path, PurePosixPath
from typing import Tuple

import modal


def _normalize_volume_path(raw_path: str) -> PurePosixPath:
    """Volume 内の相対パスだけを受け付ける。"""

    normalized = PurePosixPath(raw_path.strip())
    if not raw_path.strip():
        raise ValueError("パスを空にはできません。")
    if normalized.is_absolute():
        raise ValueError("Volume 内の相対パスを指定してください。")
    if ".." in normalized.parts:
        raise ValueError("'..' を含むパスは指定できません。")
    return normalized


def _build_app(
    source_volume_name: str,
    destination_volume_name: str,
    create_destination_volume: bool,
) -> Tuple[modal.App, modal.Function]:
    """指定された Volume を使う移動用の Modal アプリを構築する。"""

    app = modal.App(name="volume-file-mover")

    source_mount = "/source_vol"
    destination_mount = "/dest_vol"
    same_volume = source_volume_name == destination_volume_name

    source_volume = modal.Volume.from_name(source_volume_name)
    destination_volume = (
        source_volume
        if same_volume
        else modal.Volume.from_name(
            destination_volume_name,
            create_if_missing=create_destination_volume,
        )
    )

    volumes = {source_mount: source_volume}
    if same_volume:
        destination_mount = source_mount
    else:
        volumes[destination_mount] = destination_volume

    @app.function(
        volumes=volumes,
        timeout=1800,
        serialized=True,
    )
    def move_path(
        source_path_raw: str,
        destination_path_raw: str,
        overwrite: bool,
    ) -> None:
        """Volume 間、または同一 Volume 内でファイル/ディレクトリを移動する。"""

        source_rel_path = _normalize_volume_path(source_path_raw)
        destination_rel_path = _normalize_volume_path(destination_path_raw)

        source_path = Path(source_mount) / source_rel_path
        destination_base = Path(destination_mount) / destination_rel_path

        if not source_path.exists():
            raise FileNotFoundError(
                f"Source path '{source_rel_path.as_posix()}' does not exist."
            )

        destination_path = destination_base
        if destination_path.exists() and destination_path.is_dir():
            destination_path = destination_path / source_path.name

        if destination_path == source_path:
            raise ValueError("移動元と移動先が同じです。")

        destination_path.parent.mkdir(parents=True, exist_ok=True)

        if destination_path.exists():
            if not overwrite:
                raise FileExistsError(
                    f"Destination '{destination_rel_path.as_posix()}' already exists."
                )
            if destination_path.is_dir():
                shutil.rmtree(destination_path)
            else:
                destination_path.unlink()

        shutil.move(str(source_path), str(destination_path))

        item_type = "directory" if destination_path.is_dir() else "file"
        print(
            f"Moved {item_type}: "
            f"{source_volume_name}:{source_rel_path.as_posix()} -> "
            f"{destination_volume_name}:{destination_path.relative_to(destination_mount).as_posix()}"
        )

    return app, move_path


def run_move(
    source_volume_name: str,
    source_path: str,
    destination_volume_name: str,
    destination_path: str,
    auto_confirm: bool,
    overwrite: bool,
    create_destination_volume: bool,
) -> None:
    """移動処理の実行フローを管理する。"""

    normalized_source_path = _normalize_volume_path(source_path).as_posix()
    normalized_destination_path = _normalize_volume_path(destination_path).as_posix()

    app, move_path = _build_app(
        source_volume_name,
        destination_volume_name,
        create_destination_volume=create_destination_volume,
    )

    print(
        "This script will move data in Modal Volume storage:\n"
        f"  from {source_volume_name}:{normalized_source_path}\n"
        f"  to   {destination_volume_name}:{normalized_destination_path}"
    )
    if overwrite:
        print("Existing destination entries will be overwritten.")
    if create_destination_volume:
        print("The destination volume will be created if it does not exist.")

    if not auto_confirm:
        if input("Proceed? (y/n): ").lower().strip() != "y":
            print("Operation cancelled.")
            return

    try:
        with app.run():
            move_path.remote(
                source_path_raw=normalized_source_path,
                destination_path_raw=normalized_destination_path,
                overwrite=overwrite,
            )
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        print(f"Move failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except modal.exception.Error as exc:  # noqa: BLE001
        print(f"Failed to start Modal job. Reason: {exc}")
        raise

    print("Process finished.")


def parse_args() -> argparse.Namespace:
    """コマンドライン引数を解釈する。"""

    parser = argparse.ArgumentParser(
        description="Modal Volume 内のファイルやディレクトリを移動します"
    )
    parser.add_argument("source_volume", help="移動元の Modal Volume 名")
    parser.add_argument("source_path", help="移動元 Volume 内の相対パス")
    parser.add_argument("destination_volume", help="移動先の Modal Volume 名")
    parser.add_argument("destination_path", help="移動先 Volume 内の相対パス")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="確認プロンプトをスキップして即時実行します",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="移動先が既に存在する場合に上書きします",
    )
    parser.add_argument(
        "--create-destination-volume",
        action="store_true",
        help="移動先 Volume が存在しない場合に作成します",
    )
    return parser.parse_args()


def main() -> None:
    """スクリプトのエントリーポイント。"""

    args = parse_args()
    run_move(
        source_volume_name=args.source_volume,
        source_path=args.source_path,
        destination_volume_name=args.destination_volume,
        destination_path=args.destination_path,
        auto_confirm=args.yes,
        overwrite=args.overwrite,
        create_destination_volume=args.create_destination_volume,
    )


if __name__ == "__main__":
    main()
