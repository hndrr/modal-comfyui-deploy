import argparse
import os
import shutil
from typing import Tuple

import modal


def _build_app(old_volume_name: str, new_volume_name: str) -> Tuple[modal.App, modal.Function]:
    """指定されたVolume名でModalアプリとコピー関数を構築する"""

    app = modal.App(name=f"volume-renamer-{old_volume_name}-to-{new_volume_name}")

    source_mount = "/source_vol"
    dest_mount = "/dest_vol"

    source_volume = modal.Volume.from_name(old_volume_name)
    destination_volume = modal.Volume.from_name(
        new_volume_name, create_if_missing=True
    )

    # serialized=True を付与し、ローカルスコープ内でのデコレータ利用を許可
    @app.function(
        volumes={
            source_mount: source_volume,
            dest_mount: destination_volume,
        },
        timeout=1800,
        serialized=True,
    )
    def copy_data() -> None:
        """古いVolumeから新しいVolumeへデータをコピーします。"""

        if not os.path.exists(source_mount) or not os.listdir(source_mount):
            print(
                f"Source volume '{old_volume_name}' is empty or does not exist. Nothing to copy."
            )
            return

        os.makedirs(dest_mount, exist_ok=True)

        copied_items = 0
        skipped_items = 0

        for item in os.listdir(source_mount):
            source_item_path = os.path.join(source_mount, item)
            dest_item_path = os.path.join(dest_mount, item)

            try:
                if os.path.isdir(source_item_path):
                    shutil.copytree(
                        source_item_path,
                        dest_item_path,
                        dirs_exist_ok=True,
                    )
                    print(f"Copied directory: {item}")
                else:
                    shutil.copy2(source_item_path, dest_item_path)
                    print(f"Copied file: {item}")

                copied_items += 1

            except FileExistsError:
                print(f"Item '{item}' already exists in destination. Skipping.")
                skipped_items += 1
            except Exception as exc:  # noqa: BLE001 例外内容をログへ出すために広めに捕捉
                print(f"Could not copy '{item}'. Reason: {exc}")
                skipped_items += 1

        print("\n" + "=" * 30)
        print("Copy operation summary:")
        print(f"Successfully copied: {copied_items} items")
        print(f"Skipped/Failed: {skipped_items} items")
        print(
            f"Data copy from '{old_volume_name}' to '{new_volume_name}' is complete."
        )
        print("=" * 30 + "\n")

    return app, copy_data


def run_copy(old_volume_name: str, new_volume_name: str, auto_confirm: bool) -> None:
    """コピー処理の実行フローを管理する"""

    app, copy_data = _build_app(old_volume_name, new_volume_name)

    print(
        f"This script will copy all data from Modal Volume '{old_volume_name}' "
        f"to '{new_volume_name}'."
    )
    print("The destination volume will be created if it doesn't exist.")

    if not auto_confirm:
        if input("Proceed? (y/n): ").lower().strip() != "y":
            print("Operation cancelled.")
            return

    try:
        with app.run():
            copy_data.remote()
    except Exception as exc:  # noqa: BLE001 Modal APIからの例外をそのまま通知
        print(f"Failed to start Modal job. Reason: {exc}")
        raise

    print("Process finished.")
    print("Please verify the contents of the new volume.")
    print("Once confirmed, you can manually delete the old volume by running:")
    print(f"\n  modal volume delete {old_volume_name}\n")


def parse_args() -> argparse.Namespace:
    """コマンドライン引数を解釈する"""

    parser = argparse.ArgumentParser(
        description="Modal Volume間でデータをコピーします"
    )
    parser.add_argument(
        "source",
        help="コピー元のModal Volume名",
    )
    parser.add_argument(
        "destination",
        help="コピー先のModal Volume名",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="確認プロンプトをスキップして即時実行します",
    )
    return parser.parse_args()


def main() -> None:
    """スクリプトのエントリーポイント"""

    args = parse_args()
    run_copy(args.source, args.destination, auto_confirm=args.yes)


if __name__ == "__main__":
    main()
