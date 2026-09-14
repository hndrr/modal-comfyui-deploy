"""FastVideo model lifecycle, imported only inside the dedicated GPU image."""

from pathlib import Path
import sys
import tempfile

from .config import MODEL_ROOT
from .contracts import RESOLUTIONS, prompt_text


class FastH3Engine:
    def __init__(self, revision: str):
        sys.path.insert(0, "/opt/FastVideo/examples/inference/basic")
        from basic_fasth3 import parse_args, configure_environment, build_generator_config
        from fastvideo import VideoGenerator

        model = Path(MODEL_ROOT) / revision
        if not (model / "modular_model_index.json").exists():
            raise RuntimeError("Prepare the pinned FastH3 model snapshot first")
        self.args = parse_args(
            [
                "--prompt",
                "ambient",
                "--model-path",
                str(model),
                "--num-gpus",
                "1",
                "--vsa-kernel",
                "triton",
                "--no-fa4",
                "--no-parallel-vae",
                "--no-replicated-dit",
                "--no-inference-torch-compile",
                "--no-compile-vae",
                "--profile",
                "strict",
            ]
        )
        configure_environment(self.args)
        self.generator = VideoGenerator.from_config(build_generator_config(self.args))

    def generate(self, request: dict) -> bytes:
        from basic_fasth3 import build_request, _actual_output_path

        self.args.prompt = prompt_text(request)
        self.args.width, self.args.height = RESOLUTIONS[request["resolution"]]
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "generated.mp4"
            result = self.generator.generate(build_request(self.args, target, request["seed"]))
            return _actual_output_path(result, target).read_bytes()

    def close(self) -> None:
        self.generator.shutdown()
