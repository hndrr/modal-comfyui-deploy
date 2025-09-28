import subprocess
import modal

# 使用するモデルのリスト（LORAやVAEなども含む）
MODELS = [
    (
        "https://huggingface.co/Comfy-Org/flux1-schnell/resolve/main/flux1-schnell-fp8.safetensors",
        "models/checkpoints",
    ),
]

# 使用するカスタムノードのリスト
NODES = []

# イメージファイルの作成
image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("git")
    .pip_install("comfy-cli==1.2.3")
    .run_commands("comfy --skip-prompt install --nvidia")
    .run_commands(
        *[
            f"comfy --skip-prompt model download --url {url} --relative-path {path}"
            for url, path in MODELS
        ]
    )
    .run_commands(*[f"comfy node install {node}" for node in NODES])
)

app = modal.App(name="example-comfyui", image=image)


@app.function(
    allow_concurrent_inputs=10,
    concurrency_limit=1,
    container_idle_timeout=30,
    timeout=1800,
    gpu="L4",
)
@modal.web_server(8000, startup_timeout=60)
def ui():
    subprocess.Popen("comfy launch -- --listen 0.0.0.0 --port 8000", shell=True)
