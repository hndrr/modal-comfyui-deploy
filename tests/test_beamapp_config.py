import os
import unittest
from unittest.mock import patch

import beamapp


class BeamConfigTests(unittest.TestCase):
    def test_gpu_profiles_match_beam_gpu_names(self) -> None:
        expected = {
            "rtx5090": ("RTX5090", "12.0", "120f", "serverless", "scaled_mm_nvfp4"),
            "rtx4090": ("RTX4090", "8.9", "89", "serverless", "int8_linear"),
            "rtx-pro-6000": (
                "RTXPro6000",
                "12.0",
                "120f",
                "on-demand",
                "scaled_mm_nvfp4",
            ),
            "h100": ("H100", "9.0", "90a", "on-demand", "int8_linear"),
            "a100-80gb": ("A100-80", "8.0", "80", "on-demand", "int8_linear"),
        }
        for name, (gpu, arch, kitchen_arch, capacity, capability) in expected.items():
            with self.subTest(name=name):
                with patch.dict(os.environ, {beamapp.COMFYUI_BEAM_GPU_ENV: name}):
                    resolved_name, profile = beamapp._resolve_gpu_profile()
                self.assertEqual(resolved_name, name)
                self.assertEqual(profile["beam_gpu"], gpu)
                self.assertEqual(profile["cuda_arch_list"], arch)
                self.assertEqual(profile["comfy_cuda_archs"], kitchen_arch)
                self.assertEqual(profile["capacity"], capacity)
                self.assertIn(capability, profile["required_kitchen_capabilities"])

    def test_rejects_unknown_gpu(self) -> None:
        with (
            patch.dict(
                os.environ,
                {beamapp.COMFYUI_BEAM_GPU_ENV: "not-a-gpu"},
            ),
            self.assertRaisesRegex(ValueError, "h100.*rtx5090"),
        ):
            beamapp._resolve_gpu_profile()

    def test_default_profile_is_serverless_blackwell(self) -> None:
        self.assertEqual(beamapp.DEFAULT_GPU_PROFILE, "rtx5090")
        self.assertEqual(beamapp.GPU_PROFILE_NAME, "rtx5090")
        self.assertEqual(beamapp.COMFY_CUDA_ARCHS, "120f")

    def test_optional_pool_name(self) -> None:
        with patch.dict(os.environ, {beamapp.COMFYUI_BEAM_POOL_ENV: "gpu-pool"}):
            self.assertEqual(
                beamapp._resolve_optional_name(beamapp.COMFYUI_BEAM_POOL_ENV),
                "gpu-pool",
            )
        with patch.dict(os.environ, {beamapp.COMFYUI_BEAM_POOL_ENV: ""}):
            self.assertIsNone(
                beamapp._resolve_optional_name(beamapp.COMFYUI_BEAM_POOL_ENV)
            )

    def test_switch_accepts_only_on_or_off(self) -> None:
        env_name = "TEST_BEAM_SWITCH"
        for value, expected in (("on", True), (" OFF ", False)):
            with self.subTest(value=value), patch.dict(os.environ, {env_name: value}):
                self.assertEqual(beamapp._resolve_switch(env_name, False), expected)
        with (
            patch.dict(os.environ, {env_name: "yes"}),
            self.assertRaisesRegex(ValueError, "on, off"),
        ):
            beamapp._resolve_switch(env_name, False)

    def test_cpu_rejects_non_finite_values(self) -> None:
        env_name = "TEST_BEAM_CPU"
        for value in ("nan", "inf", "+inf", "-inf"):
            with (
                self.subTest(value=value),
                patch.dict(os.environ, {env_name: value}),
                self.assertRaisesRegex(ValueError, "positive number"),
            ):
                beamapp._resolve_positive_float_env(env_name, 1.0)

        with patch.dict(os.environ, {env_name: "1.5"}):
            self.assertEqual(beamapp._resolve_positive_float_env(env_name, 1.0), 1.5)

    def test_pod_mounts_all_comfyui_state(self) -> None:
        mounts = {volume.mount_path: volume.name for volume in beamapp.volumes}
        self.assertEqual(
            mounts,
            {
                "/models": "comfyui-models",
                "/data/custom_nodes": "comfyui-custom-nodes",
                "/data/output": "comfyui-outputs",
                "/data/input": "comfyui-inputs",
                "/data/user": "comfyui-user-data",
            },
        )
        self.assertEqual(beamapp.comfyui.ports, [8000])
        self.assertEqual(beamapp.comfyui.entrypoint, ["python3", "beam_runtime.py"])
        self.assertEqual(beamapp.image.base_image, "")
        self.assertIsNone(beamapp.comfyui.pool)
        pod_env = dict(item.split("=", 1) for item in beamapp.comfyui.env)
        self.assertEqual(pod_env[beamapp.COMFYUI_EXPECTED_CUDA_ARCH_ENV], "12.0")
        self.assertIn(
            "scaled_mm_nvfp4",
            pod_env[beamapp.COMFYUI_REQUIRED_KITCHEN_CAPABILITIES_ENV],
        )
        self.assertEqual(pod_env[beamapp.COMFYUI_SAGE_ATTENTION_ENV], "off")

    def test_image_uses_cuda_13_and_pinned_comfy_kitchen_wheel(self) -> None:
        commands = "\n".join(command.command for command in beamapp.image.build_steps)
        self.assertIn("cuda-toolkit-13-0", commands)
        self.assertIn("--cuda-version 13.0", commands)
        self.assertIn("torch-2.10.0%2Bcu130", commands)
        # comfy-cli rejects --commit unless --version precedes it on the command line.
        self.assertIn(
            f'--version nightly --commit "{beamapp.COMFYUI_COMMIT}"', commands
        )
        self.assertIn("nvidia-cublas>=13.0.0", commands)
        self.assertIn("comfy-kitchen==0.2.30", commands)
        self.assertIn("--force-reinstall --no-deps", commands)
        self.assertIn(beamapp.FLASH_ATTN_WHEEL_SHA256, commands)
        self.assertIn("sha256sum -c -", commands)
        self.assertNotIn("comfy node install", commands)
        self.assertNotIn("install.py", commands)
        for name, repository, commit in beamapp.CUSTOM_NODES:
            with self.subTest(name=name):
                self.assertIn(repository, commands)
                self.assertIn(f'checkout --detach "{commit}"', commands)
        self.assertNotIn("cuda-toolkit-12-8", commands)
        self.assertNotIn("SageAttention.git", commands)

    def test_sage_attention_uses_an_immutable_commit(self) -> None:
        self.assertRegex(beamapp.SAGEATTENTION_COMMIT, r"^[0-9a-f]{40}$")
        command = beamapp._sage_attention_build_command("")
        self.assertIn(f'checkout --detach "{beamapp.SAGEATTENTION_COMMIT}"', command)
        self.assertNotIn("--branch", command)


if __name__ == "__main__":
    unittest.main()
