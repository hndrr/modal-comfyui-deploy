"""Versioned workflow registry owned by the single-writer CPU gateway."""
from ambient.contracts import DEFAULT_BACKENDS, IMAGE_MODES, MUSIC_DIRECTION
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import tempfile

STAGES = {*DEFAULT_BACKENDS, "jev", "media", "text", "imagegen"}
PROTECTED = {"cwd", "extra_args_json", "sandbox_mode", "ephemeral", "skip_git_repo_check",
             "output_schema_json", "cli_skill", "concurrency_count", "auto_save_to_output"}


def field_value(graph, binding):
    if binding.get("optional") and (binding["node"] not in graph or binding["input"] not in graph[binding["node"]]["inputs"]):
        return None
    value = graph[binding["node"]]["inputs"][binding["input"]]
    if binding.get("part"):
        if not isinstance(value, str) or "\n\noverall_soundscape: " not in value:
            raise ValueError("Keep the H3 prompt's integrated_multimodal_description / overall_soundscape sections")
        prompt, sound = value.split("\n\noverall_soundscape: ", 1)
        return (prompt.removeprefix("integrated_multimodal_description: [Shot 1] ")
                if binding["part"] == "prompt" else sound.split("\n\nnon_diegetic_music:", 1)[0] + (
                    "\nMusic: " + sound.split("\n\nnon_diegetic_music:", 1)[1].strip()
                    if "\n\nnon_diegetic_music:" in sound and sound.split("\n\nnon_diegetic_music:", 1)[1].strip() not in {"N/A", MUSIC_DIRECTION, ""} else ""))
    return deepcopy(value)


def set_field(graph, binding, value):
    inputs = graph[binding["node"]]["inputs"]
    if binding.get("part"):
        other = {**binding, "part": "sound" if binding["part"] == "prompt" else "prompt"}
        raw = inputs[binding["input"]]
        previous = field_value(graph, other)
        if other["part"] == "sound":
            previous = raw.split("\n\noverall_soundscape: ", 1)[1].split("\n\nnon_diegetic_music:", 1)[0]
        music = raw.split("\n\nnon_diegetic_music:", 1)[1].strip() if "\n\nnon_diegetic_music:" in raw else MUSIC_DIRECTION
        if music == "N/A" and binding["part"] == "sound":
            music = MUSIC_DIRECTION
        prompt, sound = (value, previous) if binding["part"] == "prompt" else (previous, value)
        value = (f"integrated_multimodal_description: [Shot 1] {prompt}\n\n"
                 f"overall_soundscape: {sound}\n\nnon_diegetic_music: {music}")
    inputs[binding["input"]] = deepcopy(value)


def validate_template(template, baseline, objects):
    from ambient.h3 import input_fields

    if not isinstance(template, dict) or not isinstance(template.get("bindings"), dict) or not isinstance(template.get("outputs"), dict):
        raise ValueError("Expected graph, bindings and outputs objects")
    graph = template.get("graph")
    if not isinstance(graph, dict) or not graph or len(graph) > 500:
        raise ValueError("Expected a workflow with 1–500 nodes")
    required_bindings = {key for key, value in baseline["bindings"].items() if not value.get("optional")}
    if not required_bindings <= set(template.get("bindings", {})):
        raise ValueError("Keep all Ambient input bindings")
    if set(template.get("outputs", {})) != set(baseline["outputs"]):
        raise ValueError("Keep all Ambient output bindings")
    for name, binding in template["bindings"].items():
        if not isinstance(binding, dict) or not isinstance(binding.get("node"), str) or not isinstance(binding.get("input"), str):
            raise ValueError(f"Invalid binding: {name}")
        if name in required_bindings and binding.get("optional"):
            raise ValueError(f"Keep required Ambient input: {name}")
        if binding.get("part") not in (None, "prompt", "sound"):
            raise ValueError(f"Invalid prompt section: {name}")
        try:
            field_value(graph, binding)
            if binding.get("source", "ambient") not in {"ambient", "workflow"}:
                raise ValueError("Invalid input source")
        except (KeyError, TypeError) as error:
            raise ValueError(f"Reconnect Ambient input: {name}") from error
    for name, node_id in template["outputs"].items():
        expected = baseline["graph"][baseline["outputs"][name]]["class_type"]
        if graph.get(node_id, {}).get("class_type") != expected:
            raise ValueError(f"Reconnect {name} to a {expected} output")
    for node_id, node in graph.items():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            raise ValueError(f"Invalid node {node_id}")
        kind, inputs = node.get("class_type"), node.get("inputs", {})
        if kind not in objects:
            raise ValueError(f"Missing node {kind} ({node_id})")
        if inputs.get("api_key"):
            raise ValueError("Use server environment variables for API keys")
        required, fields = input_fields(objects[kind]["input"], inputs)
        # Autogrow is an editor descriptor, not an execution input. Expand the
        # published template names just as the v3 wire schema specifies.
        for key, spec in list(fields.items()):
            if spec[0] != "COMFY_AUTOGROW_V3":
                continue
            required.pop(key, None)
            fields.pop(key)
            template_input = spec[1]["template"]
            names = template_input.get("names", [f"{template_input.get('prefix')}{i}" for i in range(template_input.get("max", 0))])
            sections = template_input["input"]
            child = next(value for section in sections.values() for value in section.values())
            for i, name in enumerate(names):
                fields[f"{key}.{name}"] = child
                if i < template_input.get("min", 0) and sections.get("required"):
                    required[f"{key}.{name}"] = child
        for key in required:
            if key not in inputs:
                raise ValueError(f"Missing input {node_id}.{key}")
        for key, value in inputs.items():
            if key not in fields:
                raise ValueError(f"Unknown input {node_id}.{key}")
            if isinstance(value, list):
                if len(value) != 2 or str(value[0]) not in graph or type(value[1]) is not int:
                    raise ValueError(f"Broken connection {node_id}.{key}")
                outputs = objects[graph[str(value[0])]["class_type"]].get("output", [])
                if not 0 <= value[1] < len(outputs):
                    raise ValueError(f"Broken output slot {node_id}.{key}")
                target_type = fields[key][0]
                if isinstance(target_type, str) and target_type not in ("*", outputs[value[1]]):
                    raise ValueError(f"Wrong connection type {node_id}.{key}")
            else:
                expected = fields[key][0]
                options = fields[key][1] if len(fields[key]) > 1 and isinstance(fields[key][1], dict) else {}
                if expected == "STRING" and not isinstance(value, str):
                    raise ValueError(f"Expected text at {node_id}.{key}")
                if expected in ("INT", "FLOAT") and (type(value) not in (int, float) or not math.isfinite(value) or (expected == "INT" and type(value) is not int)):
                    raise ValueError(f"Expected {expected} at {node_id}.{key}")
                if expected == "BOOLEAN" and type(value) is not bool:
                    raise ValueError(f"Expected boolean at {node_id}.{key}")
                if isinstance(expected, list) and value not in expected and not options.get("image_upload"):
                    raise ValueError(f"Unavailable choice at {node_id}.{key}")
        if kind.startswith("AgentRuntimeBridge"):
            original = next((n for n in baseline["graph"].values() if n["class_type"] == kind), None)
            if original is None:
                raise ValueError("Bridge execution nodes must belong to this processing stage")
            for key in PROTECTED:
                if inputs.get(key) != original["inputs"].get(key):
                    raise ValueError(f"Keep Bridge execution constraint: {key}")
    # Every declared output must depend on each required input boundary. This catches
    # disconnected ports even when the nodes themselves still exist on the canvas.
    ancestry = {}
    def ancestors(node_id, visiting=None):
        visiting = set() if visiting is None else visiting
        if node_id in visiting:
            raise ValueError("Workflow contains a cycle")
        if node_id in ancestry:
            return ancestry[node_id]
        found = {node_id}
        for value in graph[node_id]["inputs"].values():
            if isinstance(value, list):
                found |= ancestors(str(value[0]), visiting | {node_id})
        ancestry[node_id] = found
        return found
    reachable = set().union(*(ancestors(node) for node in template["outputs"].values()))
    for name, binding in template["bindings"].items():
        if binding["node"] not in reachable and not binding.get("optional"):
            raise ValueError(f"Ambient input {name} is disconnected from the outputs")
    return template


def pin_references(template, volume):
    """Fixed references must outlive the job/upload cleanup that created them."""
    template = deepcopy(template)
    graph, visited = template["graph"], set()
    def visit(node_id):
        if node_id in visited or node_id not in graph:
            return
        visited.add(node_id)
        node = graph[node_id]
        for value in node["inputs"].values():
            if isinstance(value, list):
                visit(str(value[0]))
        key = {"LoadImage": "image", "LoadAudio": "audio", "LoadVideo": "file"}.get(node["class_type"])
        if not key:
            return
        source = node["inputs"][key]
        if not isinstance(source, str):
            raise ValueError("Fixed reference must point to an uploaded input file")
        path = PurePosixPath(source)
        if path.is_absolute() or ".." in path.parts or "[" in source:
            raise ValueError("Fixed reference must use a relative input path")
        if source.startswith("ambient/workflow-assets/"):
            return
        with tempfile.TemporaryDirectory(prefix="ambient-workflow-ref-") as directory:
            local = Path(directory) / "asset"
            with local.open("wb") as handle:
                volume.read_file_into_fileobj(source, handle)
            digest = hashlib.sha256(local.read_bytes()).hexdigest()
            target = f"ambient/workflow-assets/{digest}{path.suffix}"
            with volume.batch_upload(force=True) as batch:
                batch.put_file(str(local), target)
        node["inputs"][key] = target
    for binding in template["bindings"].values():
        if binding.get("source") == "workflow":
            visit(binding["node"])
    return template


class WorkflowRegistry:
    def __init__(self, state):
        self.state = state.setdefault("ambient_workflows", {"revision": 0, "versions": {"0": {}}, "defaults": {}})

    def snapshot(self, revision=None):
        revision = self.state["revision"] if revision is None else revision
        if type(revision) is not int or str(revision) not in self.state["versions"]:
            raise ValueError("Unknown workflow revision")
        return {"revision": revision, "stages": deepcopy(self.state["versions"][str(revision)])}

    def describe(self, revision=None):
        current = self.snapshot(revision)
        current["stages"] = {**deepcopy(self.state["defaults"]), **current["stages"]}
        current["history"] = [int(key) for key in self.state["versions"]]
        return current

    def apply(self, stage, template, expected_revision, objects):
        if expected_revision != self.state["revision"]:
            raise ValueError("Workflow changed on another device. Reload before applying.")
        if stage not in STAGES or stage not in self.state["defaults"]:
            raise ValueError("Run this stage once before editing its workflow")
        if len(json.dumps(template)) > 2 * 1024 * 1024:
            raise ValueError("Workflow exceeds 2 MiB")
        validate_template(template, self.state["defaults"][stage], objects)
        snapshot = self.snapshot()["stages"]
        snapshot[stage] = deepcopy(template)
        revision = self.state["revision"] + 1
        self.state["versions"][str(revision)] = snapshot
        self.state["revision"] = revision
        return self.describe()

    def prepare(self, body):
        body = deepcopy(body)
        meta = body.get("extra_data", {}).get("ambient")
        if not meta:
            return body
        if not isinstance(meta, dict) or not all(key in meta for key in ("sessionId", "stage", "revision", "bindings", "outputs")):
            raise ValueError("Incomplete Ambient workflow metadata")
        stage = meta.get("stage")
        if stage not in STAGES:
            raise ValueError("Invalid Ambient stage")
        baseline = {"graph": body["prompt"], "bindings": meta["bindings"], "outputs": meta["outputs"],
                    "workflow": body.get("extra_data", {}).get("extra_pnginfo", {}).get("workflow")}
        # Refresh display templates with shipped schemas. Applied snapshots and
        # accepted execution graphs remain unchanged and keep their own version.
        self.state["defaults"][stage] = deepcopy(baseline)
        saved = self.snapshot(meta["revision"])["stages"].get(stage)
        if saved:
            graph = deepcopy(saved["graph"])
            bindings = deepcopy(saved["bindings"])
            # Reference/skill subgraphs contain per-job uploaded paths. Copy these
            # inputs with fresh IDs rather than retaining an earlier job's media.
            copied = {}
            def live_value(value):
                if not isinstance(value, list):
                    return value
                node_id = str(value[0])
                if node_id not in copied:
                    new_id = str(max([int(k) for k in graph if k.isdigit()] + [0]) + 1)
                    copied[node_id] = new_id
                    graph[new_id] = deepcopy(baseline["graph"][node_id])
                    graph[new_id]["inputs"] = {key: live_value(v) for key, v in graph[new_id]["inputs"].items()}
                return [copied[node_id], value[1]]
            for name, binding in baseline["bindings"].items():
                if name not in bindings and binding.get("optional") and stage in {"media", "text", "imagegen"}:
                    bindings[name] = {**binding, "node": bindings["prompt"]["node"]}
            for name, binding in bindings.items():
                if binding.get("source", "ambient") == "ambient":
                    source = baseline["bindings"].get(name)
                    value = field_value(baseline["graph"], source) if source else None
                    if name == "reference" and stage in IMAGE_MODES:
                        if value is None:
                            graph.get(bindings["prompt"]["node"], {}).get("inputs", {}).pop("first_frame", None)
                            graph.pop(binding["node"], None)
                            continue
                        graph.setdefault(binding["node"], {"class_type": "LoadImage", "inputs": {}})
                        graph[bindings["prompt"]["node"]]["inputs"]["first_frame"] = [binding["node"], 0]
                    elif value is None and binding.get("optional"):
                        graph[binding["node"]]["inputs"].pop(binding["input"], None)
                        continue
                    set_field(graph, binding, live_value(value))
            # Output paths and ephemeral Bridge constraints always belong to the job.
            for node_id, node in graph.items():
                original = baseline["graph"].get(node_id)
                if node["class_type"].startswith("AgentRuntimeBridge"):
                    original = next((n for n in baseline["graph"].values()
                                     if n["class_type"] == node["class_type"]), None)
                if original and original["class_type"] == node["class_type"]:
                    for key in {"filename_prefix"} | PROTECTED:
                        if key in original["inputs"]:
                            node["inputs"][key] = deepcopy(original["inputs"][key])
            for name, output_id in saved["outputs"].items():
                original = baseline["graph"][baseline["outputs"][name]]
                if "filename_prefix" in original["inputs"]:
                    graph[output_id]["inputs"]["filename_prefix"] = original["inputs"]["filename_prefix"]
            body["prompt"] = graph
            meta["bindings"], meta["outputs"] = bindings, deepcopy(saved["outputs"])
            # The saved UI graph is a layout template; its widgets have not had
            # this job's inputs applied. Do not mislabel it as a native snapshot.
            body["extra_data"].pop("extra_pnginfo", None)
            meta["layout"] = saved.get("workflow")
        meta["effective"] = {name: field_value(body["prompt"], binding) for name, binding in meta["bindings"].items()}
        if stage == "fasth3-8step-i2v" and not meta["effective"].get("reference"):
            raise ValueError("FastH3 8-step I2V requires a first-frame image. Add an image in Ambient or fix the reference in its ComfyUI workflow.")
        if stage in DEFAULT_BACKENDS:
            meta["effective"]["settings"] = {
                node_id: {"class_type": node["class_type"], "inputs": deepcopy(node["inputs"])}
                for node_id, node in body["prompt"].items()
            }
        return body


def h3_metadata(request, graph):
    binding = lambda node, key, **extra: {"node": node, "input": key, "source": "ambient", **extra}
    bindings = {"prompt": binding("6", "prompt", part="prompt"),
                "sound": binding("6", "prompt", part="sound"),
                "seed": binding("8", "noise_seed"), "width": binding("6", "width"),
                "height": binding("6", "height")}
    if request["mode"] in IMAGE_MODES:
        bindings["reference"] = binding("16", "image", optional=request["mode"] != "fasth3-8step-i2v")
    return {"sessionId": request["sessionId"], "stage": request["mode"],
            "revision": request["workflowRevision"], "bindings": bindings, "outputs": {"video": "15"}}
