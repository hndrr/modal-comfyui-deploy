"""Owner MCP operations, scoped to the split environment and shared model library."""

import asyncio
import json
import os
import re
from pathlib import Path

from aiohttp import ClientTimeout, web

from comfy_split import model_files, node_packs
from comfy_split.production import page
from comfy_split.state import body_digest
from comfy_split.storage import MOUNTS

MUTATIONS = {
    "prepare_environment": {"active"},
    "change_node": {"candidate", "action", "node", "version"},
    "apply_environment": {"candidate"},
    "discard_environment": {"candidate"},
    "remove_model": {"path", "version"},
    "restore_model": {"removal_id"},
    "add_model": {"repo", "file", "category", "revision", "sha256"},
}
ACTIVE = {"dispatching", "running"}


def conflict(reason):
    raise web.HTTPConflict(text=reason)


def receipt(row):
    return {k: v for k, v in row.items() if not k.startswith("_")}


def editing(c, version, states=("editing",)):
    candidate = c.journal.data["candidate"]
    if not candidate or candidate["version"] != version or candidate["status"] not in states:
        conflict("Candidate version or status changed; read get_split_status.")
    return candidate


async def manager(c, method, path, **kwargs):
    async with c.client.request(
        method, c.candidate.url + "/v2/" + path, timeout=ClientTimeout(total=15), **kwargs
    ) as response:
        if not response.ok:
            raise web.HTTPConflict(text=f"Candidate Manager rejected the operation ({response.status}).")
        if method == "GET":
            return await response.json()


async def queue_status(c):
    if c.candidate.process and c.candidate.process.returncode is None:
        state = await manager(c, "GET", "manager/queue/status")
        return {key: state.get(key) for key in ("is_processing", "pending_count", "in_progress_count")}
    return None


async def refresh_nodes(c, state):
    """Save Manager's in-memory result before letting the candidate CPU scale down."""
    changed = False
    candidate = c.journal.data["candidate"]
    for row in c.journal.data.get("admin_operations", {}).values():
        if row["tool"] != "change_node" or row["status"] not in ACTIVE:
            continue
        if (
            not candidate
            or candidate["version"] != row["candidate"]
            or state is None
            or not row.get("_task")
        ):
            result = "unknown"
        else:
            response = await manager(c, "GET", "manager/queue/history", params={"ui_id": row["_task"]})
            history = response.get("history") or {}
            status = history.get("status") or {}
            if status.get("completed") or history.get("end_time"):
                result = (
                    "completed"
                    if status.get("status_str") in {"success", "skipped", "skip"}
                    else "failed"
                )
            elif state.get("is_processing") or state.get("pending_count"):
                continue
            else:
                result = "unknown"
        row["status"] = result
        changed = True
    if changed:
        await c.persist()


def settle_apply(c, version, success):
    for row in c.journal.data.get("admin_operations", {}).values():
        if (
            row["tool"] == "apply_environment"
            and row.get("candidate") == version
            and row["status"] in ACTIVE
        ):
            row["status"] = "completed" if success else "failed"


async def start_download(args):
    import modal

    call = await modal.Function.from_name(os.environ["SPLIT_APP"], "import_model").spawn.aio(**args)
    return call.object_id


async def download_result(call_id):
    import modal

    return await modal.FunctionCall.from_id(call_id).get.aio(timeout=0)


async def poll(c, row):
    if row["status"] in ACTIVE:
        if row["tool"] == "change_node":
            await refresh_nodes(c, await queue_status(c))
        elif row["tool"] == "apply_environment":
            data = c.journal.data
            if data["environment"] == row["candidate"]:
                row["status"] = "completed"
            elif (data["candidate"] or {}).get("status") != "validating":
                row["status"] = "unknown"
        elif row["tool"] == "add_model" and row.get("_call"):
            try:
                row["result"] = await download_result(row["_call"])
            except TimeoutError:
                return receipt(row)
            except Exception:
                row.update(
                    status="failed",
                    error="Model import failed; check the repository, filename, checksum and destination.",
                )
            else:
                row["status"] = "completed"
                await c.volumes["models"].reload.aio()
        else:
            row["status"] = "unknown"
        await c.persist()
    return receipt(row)


async def change(c, a, row):
    editing(c, a["candidate"])
    node = a["node"]
    if (
        not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,99}", node)
        or node in {"all", "comfyui", "comfyui-manager", "comfyui_manager"}
        or node in {n.lower() for n in node_packs.NODE_NAMES}
        or node in os.environ.get("SPLIT_IMAGE_NODE_NAMES", "").lower().split(",")
    ):
        raise web.HTTPBadRequest(text="Expected one editable registry node ID.")
    await c.ensure_candidate()
    action = a["action"]
    if action == "install":
        version = a.get("version", "latest")
        if "version" in a and not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise web.HTTPBadRequest(text="Use a registry release version.")
        params = dict(
            id=node, version=version, selected_version=version, mode="remote", channel="default"
        )
    elif action == "update" and "version" not in a:
        packs = await manager(c, "GET", "customnode/installed")
        matches = [
            (k, v["ver"])
            for k, v in packs.items()
            if v.get("cnr_id") == node and re.fullmatch(r"\d+\.\d+\.\d+", v.get("ver", ""))
        ]
        if len(matches) != 1:
            conflict("No unambiguous installed registry release found for this node.")
        params = dict(node_name=matches[0][0], node_ver=matches[0][1])
    else:
        raise web.HTTPBadRequest(text="Use install or update; only install accepts a version.")
    await c.pin_cpu(True)
    row["_task"] = "split-mcp-" + row["request_id"]
    await c.persist()
    await manager(
        c,
        "POST",
        "manager/queue/task",
        json=dict(ui_id=row["_task"], client_id="split-mcp", kind=action, params=params),
    )
    await manager(c, "POST", "manager/queue/start", json={})
    row.update(status="running", node=node)


async def mutate(c, name, a, row):
    data = c.journal.data
    if name == "prepare_environment":
        if data["environment"] != a["active"]:
            conflict("Active version changed; read get_split_status.")
        await c.ensure_candidate()
        row["candidate"] = data["candidate"]["version"]
    elif name == "change_node":
        await change(c, a, row)
    elif name in {"discard_environment", "apply_environment"}:
        candidate = editing(
            c, a["candidate"], ("editing", "failed") if name == "discard_environment" else ("editing",)
        )
        if data["session"] or c.journal.busy() or (c.apply_task and not c.apply_task.done()):
            conflict("Wait for active jobs or validation to finish.")
        state = await queue_status(c)
        if state and (state["is_processing"] or state["pending_count"]):
            conflict("Wait for the candidate Manager queue.")
        await refresh_nodes(c, state)
        if name == "discard_environment":
            await c.close_candidate_relays()
            await c.candidate.stop()
            data["candidate"] = None
            c.cleanup_due = 0
        else:
            # A failed install must be repaired by a successful retry of the same node, or discarded.
            outcomes = {
                r.get("node"): r["status"]
                for r in data["admin_operations"].values()
                if r.get("candidate") == a["candidate"] and r["tool"] == "change_node"
            }
            if any(v != "completed" for v in outcomes.values()):
                conflict("A node change is unverified. Retry that node or discard this candidate.")
            await c.ensure_candidate()
            candidate["status"] = "validating"
            row["status"] = "running"
            await c.persist()
            c.apply_task = asyncio.create_task(c.apply_environment())
    else:
        root = Path(MOUNTS["models"])
        await c.volumes["models"].reload.aio()
        if name == "add_model":
            spec = {k: v for k, v in a.items() if k != "request_id"}
            path = model_files.import_spec(**spec)
            if os.path.lexists(model_files.target(root, path)):
                conflict("Model filename already in use.")
            row.update(path=path, _call=await start_download(spec), status="running")
        else:
            c.journal.assert_idle()
            if name == "remove_model":
                row["result"] = model_files.remove(root, a["path"], a["version"], a["request_id"])
            else:
                source = data["admin_operations"].get(a["removal_id"], {})
                if source.get("tool") != "remove_model" or source.get("status") != "completed":
                    conflict("Expected a completed remove_model request_id.")
                row["result"] = model_files.restore(root, source["result"]["path"], a["removal_id"])
            await c.volumes["models"].commit.aio()


async def handle(c, name, args):
    data = c.journal.data
    if name == "list_nodes":
        nodes = json.loads((await c.objects("/object_info")).body)
        query = args.get("query", "").casefold()
        rows = [
            {"name": k, "display_name": v.get("display_name") or k, "category": v.get("category") or ""}
            for k, v in nodes.items()
        ]
        return page(
            sorted(
                (r for r in rows if any(query in s.casefold() for s in r.values())),
                key=lambda r: r["name"],
            ),
            args,
        )
    async with c.lock:
        if name == "get_split_status":
            return {
                "active": data["environment"],
                "candidate": (
                    {k: data["candidate"].get(k) for k in ("version", "status")}
                    if data["candidate"]
                    else None
                ),
                "busy": c.journal.busy(),
                "gpu": await c.gpu_status(),
                "manager_queue": await queue_status(c),
            }
        if name == "inspect_model":
            await c.volumes["models"].reload.aio()
            try:
                return model_files.inspect(Path(MOUNTS["models"]), args["path"])
            except FileNotFoundError:
                raise web.HTTPNotFound(text="Model not found.") from None
        if name != "get_admin_operation" and name not in MUTATIONS:
            raise web.HTTPNotFound(text="Unknown operation.")
        key = args.get("request_id", "")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", key):
            raise web.HTTPBadRequest(text="Invalid request_id.")
        records = data.setdefault("admin_operations", {})
        row = records.get(key)
        if name == "get_admin_operation":
            if row is None:
                raise web.HTTPNotFound(text="Unknown management request_id.")
            return await poll(c, row)
        if args.keys() - MUTATIONS[name] - {"request_id"}:
            raise web.HTTPBadRequest(text="Unknown argument.")
        digest = body_digest([name, args])
        if row:
            if row["_digest"] != digest:
                conflict("This request_id belongs to another operation.")
            return await poll(c, row)
        row = records[key] = {
            "tool": name,
            "request_id": key,
            "status": "dispatching",
            "_digest": digest,
        }
        if "candidate" in args:
            row["candidate"] = args["candidate"]
        if name == "change_node":
            row["node"] = args.get("node")
        await c.persist()
        try:
            await mutate(c, name, args, row)
        except (web.HTTPException, ValueError, KeyError, FileNotFoundError, FileExistsError) as exc:
            row.update(
                status="failed",
                error=exc.text
                if isinstance(exc, web.HTTPException)
                else str(exc)
                if isinstance(exc, ValueError)
                else "Invalid arguments, missing file or filename conflict.",
            )
        except Exception:
            row.update(
                status="unknown", error="Outcome not confirmed; inspect state before any new request."
            )
        else:
            if row["status"] == "dispatching":
                row["status"] = "completed"
        await c.persist()
        return receipt(row)
