import filecmp
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import modal

volume = modal.Volume.from_name("comyfy-model", create_if_missing=True)
custom_node_volume = modal.Volume.from_name(
    "comfy-custom-nodes", create_if_missing=True
)
output_volume = modal.Volume.from_name("comfy-outputs", create_if_missing=True)
user_data_volume = modal.Volume.from_name("comfy-user-data", create_if_missing=True)
MODEL_VOLUME_DIR = Path("/models")
COMFY_ROOT_CANDIDATES = [
    Path("/root/comfy/ComfyUI"),
    Path("/root/ComfyUI"),
    Path("/root/.cache/comfyui/ComfyUI"),
]
CUSTOM_NODE_VOLUME_MOUNT = Path("/data/custom_nodes")
OUTPUT_VOLUME_MOUNT = Path("/data/output")
USER_DATA_VOLUME_MOUNT = Path("/data/user")
WORKFLOWS_PATCH_MARKER = "# MODAL_PATCH_ALLOW_WORKFLOWS_START"
WORKFLOWS_PATCH_SNIPPET = textwrap.dedent(
    """
    # MODAL_PATCH_ALLOW_WORKFLOWS_START
    def _modal_allow_workflows():
        _candidates = (
            "ALLOWED_JSON_TYPES",
            "ALLOWED_TYPES",
            "ALLOWED_JSON_DIRS",
            "ALLOWED_DIRS",
        )
        for _name in _candidates:
            _container = globals().get(_name)
            if isinstance(_container, list):
                if "workflows" not in _container:
                    _container.append("workflows")
            elif isinstance(_container, set):
                if "workflows" not in _container:
                    _container.add("workflows")
            elif isinstance(_container, tuple):
                if "workflows" not in _container:
                    globals()[_name] = _container + ("workflows",)

        for _name in ("ALLOWED_JSON_TYPES_MAP", "ALLOWED_TYPES_MAP"):
            _mapping = globals().get(_name)
            if isinstance(_mapping, dict) and "workflows" not in _mapping:
                _mapping["workflows"] = "json"

    _modal_allow_workflows()
    del _modal_allow_workflows
    # MODAL_PATCH_ALLOW_WORKFLOWS_END
    """
).lstrip("\n")

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
    .pip_install(
        "comfy-cli==1.5.1",
        "diffusers==0.32.0",
        "moviepy==1.0.3",
        "librosa==0.10.2.post1",
        "soundfile==0.12.1",
        "ftfy==6.2.3",
        "sageattention",
        "accelerate==1.1.0",
        "gguf",
    )
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
        USER_DATA_VOLUME_MOUNT.as_posix(): user_data_volume,
    },
)
@modal.concurrent(max_inputs=10)
@modal.web_server(8000, startup_timeout=60)
def ui():
    CUSTOM_NODE_VOLUME_MOUNT.mkdir(parents=True, exist_ok=True)
    OUTPUT_VOLUME_MOUNT.mkdir(parents=True, exist_ok=True)
    MODEL_VOLUME_DIR.mkdir(parents=True, exist_ok=True)
    USER_DATA_VOLUME_MOUNT.mkdir(parents=True, exist_ok=True)

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

    def patch_user_manager_for_workflows(comfy_root: Path) -> None:
        """ComfyUI のユーザーデータ API を補正し workflows 保存を許可する"""
        candidate_paths = [
            comfy_root / "comfy" / "ui" / "user_manager.py",
            comfy_root / "app" / "user_manager.py",
        ]

        replacements = {
            '@routes.get("/userdata/{file}")': '@routes.get(r"/userdata/{file:.*}")',
            "@routes.get('/userdata/{file}')": "@routes.get(r'/userdata/{file:.*}')",
            '@routes.post("/userdata/{file}")': '@routes.post(r"/userdata/{file:.*}")',
            "@routes.post('/userdata/{file}')": "@routes.post(r'/userdata/{file:.*}')",
            '@routes.delete("/userdata/{file}")': '@routes.delete(r"/userdata/{file:.*}")',
            "@routes.delete('/userdata/{file}')": "@routes.delete(r'/userdata/{file:.*}')",
            '@routes.post("/userdata/{file}/move/{dest}")': '@routes.post(r"/userdata/{file:.*}/move/{dest:.*}")',
            "@routes.post('/userdata/{file}/move/{dest}')": "@routes.post(r'/userdata/{file:.*}/move/{dest:.*}')",
        }

        for user_manager_path in candidate_paths:
            if not user_manager_path.exists():
                continue

            try:
                content = user_manager_path.read_text(encoding="utf-8")
            except OSError as exc:
                print(f"警告: {user_manager_path} の読み込みに失敗しました: {exc}")
                continue

            updated = content
            modified = False

            for original, replacement in replacements.items():
                if replacement not in updated and original in updated:
                    updated = updated.replace(original, replacement)
                    modified = True

            if WORKFLOWS_PATCH_MARKER not in updated:
                updated = f"{updated}\n{WORKFLOWS_PATCH_SNIPPET}"
                modified = True

            if not modified:
                continue

            try:
                user_manager_path.write_text(updated, encoding="utf-8")
                print(f"{user_manager_path} に workflows 保存許可パッチを適用しました")
            except OSError as exc:
                print(f"警告: {user_manager_path} の書き込みに失敗しました: {exc}")

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
        patch_user_manager_for_workflows(comfy_root)
        models_dir = comfy_root / "models"

        if link_directory(models_dir, MODEL_VOLUME_DIR):
            print(f"{models_dir} を {MODEL_VOLUME_DIR} に接続しました")

        if link_directory(comfy_root / "custom_nodes", CUSTOM_NODE_VOLUME_MOUNT):
            print(f"{comfy_root} の custom_nodes を永続化 Volume に接続しました")

        if link_directory(comfy_root / "output", OUTPUT_VOLUME_MOUNT):
            print(f"{comfy_root} の output を永続化 Volume に接続しました")

        if link_directory(comfy_root / "user", USER_DATA_VOLUME_MOUNT):
            print(f"{comfy_root} の user ディレクトリを永続化 Volume に接続しました")

    subprocess.Popen("comfy launch -- --listen 0.0.0.0 --port 8000", shell=True)
