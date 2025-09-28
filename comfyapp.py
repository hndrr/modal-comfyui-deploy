import filecmp
import os
import shutil
import subprocess
from pathlib import Path

import modal

volume = modal.Volume.from_name("model-weights-vol", create_if_missing=True)
custom_node_volume = modal.Volume.from_name(
    "comfy-custom-nodes", create_if_missing=True
)
output_volume = modal.Volume.from_name("comfy-outputs", create_if_missing=True)
MODEL_VOLUME_DIR = Path("/models")
COMFY_ROOT_CANDIDATES = [
    Path("/root/comfy/ComfyUI"),
    Path("/root/ComfyUI"),
    Path("/root/.cache/comfyui/ComfyUI"),
]
CUSTOM_NODE_VOLUME_MOUNT = Path("/data/custom_nodes")
OUTPUT_VOLUME_MOUNT = Path("/data/output")

# 使用するカスタムノードのリスト
NODES = [
    "https://github.com/crystian/ComfyUI-Crystools",
    "https://github.com/Firetheft/ComfyUI_Local_Media_Manager",
    "https://github.com/hayden-fr/ComfyUI-Image-Browsing",
    "https://github.com/rgthree/rgthree-comfy",
]

# イメージファイルの作成
image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("git")
    .pip_install("comfy-cli==1.5.1")
    .run_commands("comfy --skip-prompt install --nvidia")
    .run_commands(*[f"comfy node install {node}" for node in NODES])
)

app = modal.App(name="comfyui", image=image)


@app.function(
    max_containers=1,
    scaledown_window=30,
    timeout=1800,
    gpu="T4",
    volumes={
        MODEL_VOLUME_DIR.as_posix(): volume,
        CUSTOM_NODE_VOLUME_MOUNT.as_posix(): custom_node_volume,
        OUTPUT_VOLUME_MOUNT.as_posix(): output_volume,
    },
)
@modal.concurrent(max_inputs=10)
@modal.web_server(8000, startup_timeout=60)
def ui():
    CUSTOM_NODE_VOLUME_MOUNT.mkdir(parents=True, exist_ok=True)
    OUTPUT_VOLUME_MOUNT.mkdir(parents=True, exist_ok=True)
    MODEL_VOLUME_DIR.mkdir(parents=True, exist_ok=True)

    comfy_roots = [root_dir for root_dir in COMFY_ROOT_CANDIDATES if root_dir.exists()]
    if not comfy_roots:
        # どの候補も存在しない場合は最初の候補を作成ターゲットとして扱う。
        comfy_roots.append(COMFY_ROOT_CANDIDATES[0])

    def _merge_directory_contents(source_dir: Path, target_dir: Path) -> None:
        """対象ディレクトリの中身をソースディレクトリへ統合する"""

        for item in list(target_dir.iterdir()):
            destination = source_dir / item.name

            if item.is_dir():
                if destination.exists():
                    if destination.is_dir():
                        shutil.copytree(item, destination, dirs_exist_ok=True)
                        shutil.rmtree(item)
                    else:
                        backup = destination.with_suffix(".dir_conflict")
                        shutil.move(str(item), backup)
                else:
                    shutil.move(str(item), destination)
            else:
                if destination.exists():
                    try:
                        same_file = destination.is_file() and filecmp.cmp(
                            item, destination, shallow=False
                        )
                    except OSError:
                        same_file = False
                    if same_file:
                        item.unlink()
                    else:
                        backup = destination.with_suffix(".conflict")
                        shutil.move(str(item), backup)
                else:
                    shutil.move(str(item), destination)

    def link_directory(target: Path, source: Path) -> bool:
        """指定ディレクトリを永続化 Volume に向ける"""

        source.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)

        if target.is_symlink():
            current_target = Path(os.readlink(target))
            if current_target != source:
                target.unlink()
                target.symlink_to(source, target_is_directory=True)
            return True

        if target.exists():
            if target.is_dir():
                _merge_directory_contents(source, target)
                if any(target.iterdir()):
                    print(
                        f"警告: {target} を空にできなかったためシンボリックリンクを作成しません"
                    )
                    return False
                target.rmdir()
                target.symlink_to(source, target_is_directory=True)
                return True

            print(
                f"警告: {target} は既存ファイルのためシンボリックリンクを作成しません"
            )
            return False

        target.symlink_to(source, target_is_directory=True)
        return True

    for comfy_root in comfy_roots:
        models_dir = comfy_root / "models"

        if link_directory(models_dir, MODEL_VOLUME_DIR):
            print(f"{models_dir} を {MODEL_VOLUME_DIR} に接続しました")

        if link_directory(comfy_root / "custom_nodes", CUSTOM_NODE_VOLUME_MOUNT):
            print(f"{comfy_root} の custom_nodes を永続化 Volume に接続しました")

        if link_directory(comfy_root / "output", OUTPUT_VOLUME_MOUNT):
            print(f"{comfy_root} の output を永続化 Volume に接続しました")

    subprocess.Popen("comfy launch -- --listen 0.0.0.0 --port 8000", shell=True)
