"""A pending review is not a job: one owner decision creates at most one journal entry."""

import asyncio
import copy
import hashlib
import html
import json
import mimetypes
import os
import re
import secrets
import time
from pathlib import Path

from aiohttp import web

from comfy_split import input_media
from comfy_split import mcp_policy as policy
from comfy_split.config import DEPLOYMENT_ENV
from comfy_split import gpu
from comfy_split.state import body_digest
from comfy_split.media_server import byte_range, version as file_version
from comfy_split.storage import MOUNTS

TTL = 15 * 60
PUBLIC = "/mcp-approvals/"
INTERNAL = "/_remote_mcp/review/"
MAX_BODY = 1024 * 1024


def prune(rows):
    now = time.time()
    changed = False
    for key, row in list(rows.items()):
        if row["status"] == "pending" and row["expires"] <= now:
            close(row, "expired")
            changed = True
        if row["expires"] < now - 7 * 86400:
            del rows[key]
            changed = True
    return changed


def close(row, status):
    row["status"] = status
    for field in ("body", "calls", "assets", "csrf"):
        row.pop(field, None)


def version(c):
    return [c.journal.data["environment"], os.environ.get(DEPLOYMENT_ENV, "")]


def reply(row):
    if row["status"] == "submitted":
        return web.json_response(row["receipt"])
    if row["status"] != "pending":
        raise ValueError("Review " + row["status"] + ". Submit a new request_id for a new decision.")
    return web.json_response(
        {
            "status": "approval_required",
            "approval_path": PUBLIC + row["id"],
            **{k: row[k] for k in ("gpu", "gpu_reason") if k in row},
            "expires_at": row["expires"],
        },
        status=202,
    )


async def submit(c, body, key):
    # Caller holds the journal lock. Only the trusted Worker marks a machine submission.
    if not isinstance(body, dict) or body.keys() - {"prompt", "client_id", "front", "extra_data", "gpu"}:
        raise ValueError("MCP cannot supply credentials, hidden metadata or partial execution targets.")
    if len(json.dumps(body, ensure_ascii=False).encode()) > MAX_BODY:
        raise ValueError("Workflow exceeds the review size limit.")
    # artokun attaches an editor canvas under extra_pnginfo.workflow. Drop this
    # inert copy: the reviewed API graph is the sole source of execution inputs.
    extra = body.get("extra_data")
    if extra is not None and (
        not isinstance(extra, dict)
        or extra.keys() - {"extra_pnginfo"}
        or not isinstance(extra.get("extra_pnginfo", {}), dict)
        or extra.get("extra_pnginfo", {}).keys() - {"workflow"}
    ):
        raise ValueError("MCP does not accept credentials or hidden metadata.")
    client_id = "comfyui-mcp" if key.startswith("stdio:") else "remote-mcp"
    requested = gpu.request_gpu(body.get("gpu", "auto"))
    body = dict(
        **({"gpu": requested} if requested != "auto" else {}),
        prompt=body.get("prompt"),
        client_id=client_id,
        **({"front": True} if body.get("front") is True else {}),
    )
    digest = body_digest(body)
    rows = c.journal.data.setdefault("mcp_reviews", {})
    if prune(rows):
        await c.persist()
    for row in rows.values():
        if row["key"] == key:
            if row["digest"] != digest:
                raise ValueError("request_id was already used with different content.")
            return reply(row)
    # A previously accepted job remains replayable after its review is pruned.
    for job in (*c.journal.data["jobs"].values(), *c.journal.data["retired_jobs"].values()):
        if job.get("request_id") == key:
            previous = job.get("mcp_digest") or job.get("body_digest") or body_digest(job.get("body"))
            if previous != digest:
                raise ValueError("request_id was already used with different content.")
            return web.json_response(c.receipt(job))
    prepared = c.prepare_prompt(copy.deepcopy(body))
    graph = prepared["prompt"]
    definitions = json.loads((await c.objects("/object_info", graph=graph)).body)
    graph = prepared["prompt"] = policy.executable_graph(graph, definitions)
    calls, loaders = policy.plan(graph, definitions)
    if not calls:
        selection = gpu.select_gpu(requested, graph, definitions)
        job = c.admit_prompt(prepared, key, selection)
        job.update(mcp_guard=policy.execution_guard(prepared, [], []), mcp_digest=digest)
        await c.commit_prompt()
        await c.status()
        return web.json_response(c.receipt(job))
    # Pin the CPU/GPU choice at admission, including across owner approval.
    selection = gpu.select_gpu(requested, graph, definitions)
    if sum(r["status"] == "pending" for r in rows.values()) >= 64:
        raise ValueError("Too many pending reviews; decline one or wait for expiry.")
    async with input_media._lock:
        graph, assets = await asyncio.to_thread(policy.freeze_inputs, graph, loaders)
        if assets:
            await c.volumes["input"].commit.aio()
    prepared["prompt"] = graph
    # A public review reference, not a bearer token. Separate short groups so
    # the pinned stdio client can display the URL without credential redaction.
    token = ".".join(secrets.token_hex(4) for _ in range(4))
    row = rows[token] = {
        "id": token,
        **selection,
        "key": key,
        "digest": digest,
        "body": prepared,
        "calls": calls,
        "assets": assets,
        "version": version(c),
        "csrf": secrets.token_urlsafe(24),
        "expires": time.time() + TTL,
        "status": "pending",
    }
    await c.persist()
    return reply(row)


def missing_credentials(row):
    for call in row["calls"]:
        if call["billing"] == policy.COMFY:
            if os.getenv("SPLIT_COMFY_BILLING_ENABLED") != "1":
                return "Comfy API キー"
        elif call["provider"] == "Google Gemini" and not os.getenv("GEMINI_API_KEY", "").strip():
            return "Gemini API キー"
        elif call["provider"] == "OpenRouter" and not os.getenv("OPENROUTER_API_KEY", "").strip():
            return "OpenRouter API キー"
    return None


def page(row, message=""):
    esc = lambda v: html.escape(str(v), quote=True)
    chunks = [
        "<!doctype html><html lang=ja><meta charset=utf-8><meta name=viewport content='width=device-width, initial-scale=1'>",
        "<title>外部サービスへの送信</title><style>body{font:16px/1.65 system-ui;max-width:48rem;margin:3rem auto;padding:0 1rem}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f4f6;padding:1rem}button{font:inherit;padding:.5rem 1rem;margin-right:.6rem}img,video{max-width:100%}</style>",
        "<h1>外部サービスへの送信</h1>",
    ]
    if message:
        chunks.append("<p>" + esc(message) + "</p>")
    if row["status"] == "submitted":
        chunks.append(
            "<p>受付済み。このページを更新しても再投入しません。</p><code>"
            + esc(row["receipt"]["prompt_id"])
            + "</code>"
        )
    elif row["status"] == "pending":
        if "gpu" in row:
            chunks.append(f"<p>実行先：<strong>{esc(row['gpu'])}</strong>（{esc(row['gpu_reason'])}）</p>")
        chunks.append(
            "<p>まだ送信していません。以下の内容をこのジョブで送信します。実行料金はモデル・入力・提供元の料金体系により変わり、金額は未確定です。</p>"
        )
        for call in row["calls"]:
            chunks.append(
                "<h2>"
                + esc(call["provider"])
                + "</h2><p>課金先：<strong>"
                + esc(call["billing"])
                + "</strong>"
                + (" · " + esc(call["account"]) if call["account"] else "")
                + "<br>ノード："
                + esc(call["class_type"])
                + "（"
                + esc(call["node_id"])
                + "）</p><pre>"
                + esc(json.dumps(call["inputs"], ensure_ascii=False, indent=2))
                + "</pre>"
            )
        for i, asset in enumerate(row["assets"]):
            url = PUBLIC + row["id"] + "/media/" + str(i)
            mime = mimetypes.guess_type(asset["snapshot"])[0] or ""
            tag = (
                "img" if mime.startswith("image/") else "audio" if mime.startswith("audio/") else "video"
            )
            chunks.append(
                "<p>"
                + esc(asset["name"])
                + " · "
                + esc(asset["size"])
                + " bytes<br><code>"
                + esc(asset["sha256"])
                + "</code></p>"
                + "<"
                + tag
                + " controls src='"
                + url
                + "'></"
                + tag
                + ">"
            )
        missing = missing_credentials(row)
        if missing:
            chunks.append(f"<p>{esc(missing)}がサーバーに未設定のため、実行できません。</p>")
        chunks.append(
            "<form method=post action='"
            + PUBLIC
            + row["id"]
            + "'><input type=hidden name=csrf value='"
            + esc(row["csrf"])
            + "'>"
            + "<button name=decision value=approve"
            + (" disabled" if missing else "")
            + ">この1件を承認して実行</button><button name=decision value=deny>送信しない</button></form>"
        )
    else:
        chunks.append(
            "<p>"
            + esc(
                {
                    "expired": "期限切れ",
                    "denied": "拒否済み",
                    "invalidated": "入力または環境が変更されました",
                }.get(row["status"], row["status"])
            )
            + "。実行していません。</p>"
        )
    return web.Response(
        text="".join(chunks),
        content_type="text/html",
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; media-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
            # HTML form POSTs need their same-origin Origin for the Worker CSRF
            # check. no-referrer makes browsers send Origin: null instead.
            "Referrer-Policy": "same-origin",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        },
    )


async def review_asset(c, request, token, asset):
    """Hash outside the controller lock and stream only the verified file range."""
    def verified_file():
        with policy.regular(Path(MOUNTS["input"]), asset["snapshot"]) as file:
            before = file_version(os.fstat(file.fileno()))
            if (hashlib.file_digest(file, "sha256").hexdigest() != asset["sha256"]
                    or file_version(os.fstat(file.fileno())) != before):
                raise web.HTTPConflict(text="Review media changed")
            return os.dup(file.fileno()), before

    task = asyncio.create_task(asyncio.to_thread(verified_file))
    try:
        fd, original = await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            fd, _ = await task
            os.close(fd)
        finally:
            raise
    try:
        async with c.lock:
            row = c.journal.data.get("mcp_reviews", {}).get(token)
            if not row or row["status"] != "pending" or row["expires"] <= time.time():
                raise web.HTTPNotFound()
        etag = '"' + asset["sha256"] + '"'
        requested = request.headers.get("Range") if request.headers.get("If-Range", etag) == etag else None
        try:
            start, end, status = byte_range(requested, original[0])
        except ValueError:
            raise web.HTTPRequestRangeNotSatisfiable(headers={"Content-Range": f"bytes */{original[0]}"}) from None
        headers = {"Cache-Control": "no-store", "Content-Security-Policy": "sandbox; default-src 'none'",
            "X-Content-Type-Options": "nosniff", "Accept-Ranges": "bytes", "ETag": etag,
            "Content-Length": str(end-start),
            "Content-Type": mimetypes.guess_type(asset["snapshot"])[0] or "application/octet-stream"}
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end-1}/{original[0]}"
        response = web.StreamResponse(status=status, headers=headers)
        await response.prepare(request)
        def read(offset):
            if file_version(os.fstat(fd)) != original:
                raise OSError("Review media changed during delivery")
            block = os.pread(fd, min(256 * 1024, end-offset), offset)
            if not block or file_version(os.fstat(fd)) != original:
                raise OSError("Review media changed during delivery")
            return block
        while start < end:
            task = asyncio.create_task(asyncio.to_thread(read, start))
            try:
                block = await asyncio.shield(task)
            except asyncio.CancelledError:
                # A thread cannot be cancelled: keep its descriptor valid
                # until it finishes, then let the outer finally close it.
                try:
                    await task
                finally:
                    raise
            start += len(block)
            await response.write(block)
        await response.write_eof()
        return response
    finally:
        os.close(fd)


async def review(c, request, path):
    if request.headers.get("X-Split-Browser-Review") != "1":
        raise web.HTTPForbidden()
    match = re.fullmatch(
        re.escape(INTERNAL) + r"([0-9a-f]{8}(?:\.[0-9a-f]{8}){3})(?:/media/([0-9]{1,3}))?", path
    )
    if not match or request.method not in {"GET", "POST"}:
        raise web.HTTPNotFound()
    form = None
    if request.method == "POST":
        if request.content_type != "application/x-www-form-urlencoded":
            raise web.HTTPUnsupportedMediaType()
        form = await request.clone(client_max_size=4096).post()
    async with c.lock:
        rows = c.journal.data.get("mcp_reviews", {})
        if prune(rows):
            await c.persist()
        row = rows.get(match[1])
        if not row:
            raise web.HTTPNotFound()
        if match[2] is not None:
            i = int(match[2])
            if request.method != "GET" or row["status"] != "pending" or i >= len(row["assets"]):
                raise web.HTTPNotFound()
            asset = copy.deepcopy(row["assets"][i])
        elif request.method == "GET" or row["status"] != "pending":
            return page(row)
        else:
            if not secrets.compare_digest(row["csrf"], str(form.get("csrf", ""))) or form.get(
                "decision"
            ) not in {"approve", "deny"}:
                raise web.HTTPForbidden()
            if form["decision"] == "deny":
                close(row, "denied")
                await c.persist()
                return page(row)
            if missing_credentials(row):
                return page(row, "API キーが未設定です。実行していません。")
            candidate = copy.deepcopy(row)
    if match[2] is not None:
        return await review_asset(c, request, match[1], asset)
    # Large files must not block submit/cancel/status under the journal lock.
    valid = True
    try:
        await asyncio.to_thread(policy.verify_assets, candidate["assets"])
    except (OSError, ValueError, web.HTTPException):
        valid = False
    async with c.lock:
        rows = c.journal.data.get("mcp_reviews", {})
        if prune(rows):
            await c.persist()
        row = rows.get(match[1])
        if not row:
            raise web.HTTPNotFound()
        if row["status"] != "pending":
            return page(row)
        # Another approval/denial or an environment change may have completed
        # during verification. Recheck before creating the one permitted job.
        if (not valid or c.journal.data["mode"] != "split" or version(c) != row["version"]
                or body_digest(row) != body_digest(candidate)):
            close(row, "invalidated")
        elif missing_credentials(row):
            return page(row, "API キーが未設定です。実行していません。")
        else:
            job = c.admit_prompt(row["body"], row["key"],
                                 {k: row[k] for k in ("gpu", "gpu_reason") if k in row})
            job.update(mcp_guard=policy.execution_guard(row["body"], row["calls"], row["assets"]),
                       mcp_digest=row["digest"])
            row["receipt"] = c.receipt(job)
            close(row, "submitted")
            await c.commit_prompt()
            await c.status()
            return page(row)
        await c.persist()
        return page(row)
