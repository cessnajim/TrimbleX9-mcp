#!/usr/bin/env python3
"""scanner-mcp — an MCP server for a tripod LiDAR / terrestrial laser scanner
that exposes a local HTTP/REST API (`/api/v1`) over its USB or WiFi link.

Read / query / download tools are always available. Actuating tools (start a
scan, create/delete projects, delete scans) are only registered when the
environment variable SCANNER_ALLOW_ACTUATION is truthy ("1"/"true"/"yes") —
"receive-only by default", because starting a scan moves the instrument and
fires its laser, and the delete tools are destructive.

Config:
  SCANNER_BASE             HTTP/REST API base URL. Default http://192.168.42.1:34567/api/v1
  SCANNER_ALLOW_ACTUATION  Enable actuating/destructive tools when set truthy.
  SCANNER_DOWNLOAD_DIR     Directory downloads are written to (default
                           ~/scanner-downloads). Download tools never write
                           outside it.

Transport: stdio (run under an MCP client). Independent project, not affiliated
with any instrument manufacturer.
"""
import asyncio
import os
import re
import time

import httpx
from mcp.server.fastmcp import Context, FastMCP

BASE = os.environ.get("SCANNER_BASE", "http://192.168.42.1:34567/api/v1").rstrip("/")
ALLOW_ACTUATION = os.environ.get("SCANNER_ALLOW_ACTUATION", "").lower() in ("1", "true", "yes")
DOWNLOAD_DIR = os.path.realpath(
    os.path.expanduser(os.environ.get("SCANNER_DOWNLOAD_DIR", "~/scanner-downloads"))
)
TIMEOUT = httpx.Timeout(15.0, connect=5.0)

mcp = FastMCP("scanner")


def _url(path: str) -> str:
    return f"{BASE}/{path.lstrip('/')}"


async def _get_json(path: str):
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(_url(path))
        r.raise_for_status()
        return r.json()


# ------------------------------------------------------- download safety -----

_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _dest(filename: str | None, default: str, overwrite: bool) -> str:
    """Resolve a download target strictly inside DOWNLOAD_DIR.

    Accepts a plain file name only (letters, digits, '.', '_', '-'; no
    directories, no leading dot). Refuses symlink escapes and, unless
    `overwrite` is set, existing files.
    """
    name = filename or default
    if not _SAFE_NAME.fullmatch(name) or ".." in name:
        raise ValueError(
            f"invalid filename {name!r}: use a plain name such as 'scan-14.tzf' "
            "(letters, digits, '.', '_', '-'; no paths)"
        )
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    path = os.path.realpath(os.path.join(DOWNLOAD_DIR, name))
    if os.path.dirname(path) != DOWNLOAD_DIR:
        raise ValueError(f"refusing to write outside the download directory {DOWNLOAD_DIR}")
    if os.path.exists(path) and not overwrite:
        raise ValueError(f"{path} already exists; pass overwrite=true to replace it")
    return path


async def _stream_to(url: str, path: str) -> int:
    """Stream a GET response to `path` atomically (temp file + rename)."""
    tmp = path + ".part"
    total = 0
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=5.0)) as c:
            async with c.stream("GET", url) as r:
                r.raise_for_status()
                with open(tmp, "wb") as fh:
                    async for chunk in r.aiter_bytes():
                        total += len(chunk)
                        fh.write(chunk)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return total


def _task_summary(t: dict) -> dict:
    p = t.get("percentage")
    return {
        "taskUUID": t.get("taskUUID"),
        "type": t.get("type"),
        "state": t.get("state"),
        "percent": None if p is None else round(float(p) * 100, 1),
        "scanID": t.get("scanID"),
        "exitResult": t.get("exitResult"),
    }


# ---------------------------------------------------------------- read-only ---

@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def scanner_status() -> dict:
    """Get the instrument's current status: battery, AC state, leveling, on-board
    storage, IMU, laser-pointer, WiFi config, and the active error list."""
    return await _get_json("status")


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def scanner_projects() -> list:
    """List all projects on the instrument (id, projectName, projectUUID,
    nextProjectScanID)."""
    return await _get_json("projects")


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def scanner_scans() -> list:
    """List all scans as compact records (id, scanMode, points, file path, and
    the project folder the scan belongs to)."""
    scans = await _get_json("scans")
    out = []
    for s in scans:
        t = s.get("tzf") or {}
        f = t.get("file", "") or ""
        out.append({
            "id": s.get("id"),
            "scanMode": s.get("scanMode"),
            "nbPoints": t.get("nbPoints"),
            "fileSizeMB": round((t.get("fileSize") or 0) / 1e6, 1),
            "scanFileId": s.get("tzfId"),
            "file": f,
            "project": f.split("/")[0] if "/" in f else None,
            "downloaded": t.get("downloaded"),
        })
    return out


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def scanner_scan_modes() -> list:
    """List the available scan modes (the density/range/time menu): mode name,
    range (m), point count, file size, scan time (s), spacing, and rate.
    Use these names when starting a scan."""
    return await _get_json("scans/modes")


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def scanner_errors() -> list:
    """Get the instrument's current error list (type, errorLevel, description)."""
    return await _get_json("errors")


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def scanner_tasks() -> list:
    """List running or queued tasks (e.g. a scan in progress) with their state
    and percent complete (0-100). An empty list means the instrument is idle."""
    return [_task_summary(t) for t in await _get_json("tasks")]


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def scanner_wait_for_scan(ctx: Context, timeout_s: int = 900, poll_s: float = 2.0) -> dict:
    """Wait until no task is running (use after scanner_launch_scan), polling the
    task list every `poll_s` seconds (0.5-30) for up to `timeout_s` (1-7200).
    Reports progress to the client when supported. Returns
    {done, timed_out, elapsed_s, last} where `last` is the final task snapshot."""
    timeout_s = max(1, min(int(timeout_s), 7200))
    poll_s = max(0.5, min(float(poll_s), 30.0))
    start = time.monotonic()
    last = None
    while True:
        tasks = await _get_json("tasks")
        elapsed = round(time.monotonic() - start, 1)
        if not tasks:
            return {"done": True, "timed_out": False, "elapsed_s": elapsed, "last": last}
        last = _task_summary(tasks[0])
        if last["percent"] is not None:
            try:
                await ctx.report_progress(last["percent"], 100)
            except Exception:
                pass  # client did not request progress notifications
        if elapsed >= timeout_s:
            return {"done": False, "timed_out": True, "elapsed_s": elapsed, "last": last}
        await asyncio.sleep(poll_s)


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def scanner_scanfile_info(scan_file_id: int) -> dict:
    """Get metadata for one point-cloud file: file path, size, point count, and
    whether it has been downloaded."""
    return await _get_json(f"tzfs/{scan_file_id}")


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True})
async def scanner_download_scanfile(
    scan_file_id: int, filename: str | None = None, overwrite: bool = False
) -> dict:
    """Download a scan's point-cloud file into the server's download directory
    (SCANNER_DOWNLOAD_DIR). `filename` is a plain name (default
    'scan-<id>.tzf'); paths are rejected. Existing files are kept unless
    `overwrite` is true. Streams the full file; returns bytes and path."""
    path = _dest(filename, f"scan-{scan_file_id}.tzf", overwrite)
    n = await _stream_to(_url(f"tzfs/{scan_file_id}/file"), path)
    return {"scanFileId": scan_file_id, "bytes": n, "path": path}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True})
async def scanner_download_preview(
    scan_file_id: int, filename: str | None = None, overwrite: bool = False
) -> dict:
    """Download a scan's panoramic preview JPEG into the server's download
    directory (default name 'scan-<id>-preview.jpg'); same rules as
    scanner_download_scanfile."""
    path = _dest(filename, f"scan-{scan_file_id}-preview.jpg", overwrite)
    n = await _stream_to(_url(f"tzfs/{scan_file_id}/preview"), path)
    return {"scanFileId": scan_file_id, "bytes": n, "path": path}


# --------------------------------------------- actuating / destructive -------
# Registered only when SCANNER_ALLOW_ACTUATION is truthy.

if ALLOW_ACTUATION:

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True})
    async def scanner_create_project(project_name: str, project_uuid: str) -> dict:
        """Create a project on the instrument. Supply a projectName and a unique
        projectUUID (UUIDv4). Returns the created project record."""
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.post(_url("projects"),
                             json={"projectName": project_name, "projectUUID": project_uuid})
            r.raise_for_status()
            return r.json()

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True})
    async def scanner_launch_scan(project_name: str, project_uuid: str,
                                  scan_mode: str) -> dict:
        """ACTUATING: start a scan — moves the instrument and fires its laser for
        the mode's duration. To target a project you MUST pass BOTH project_name
        and project_uuid (UUID alone is ignored). `scan_mode` is one of the names
        from scanner_scan_modes. Returns the accepted task; follow it with
        scanner_tasks or scanner_wait_for_scan. Ensure the instrument is safely
        positioned before calling."""
        body = {"scanMode": scan_mode, "projectName": project_name,
                "projectUUID": project_uuid, "fastMode": False, "colorMode": "OFF"}
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.post(_url("scans"), json=body)
            r.raise_for_status()
            return r.json()

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True})
    async def scanner_delete_scan(scan_id: int) -> dict:
        """DESTRUCTIVE: permanently delete a scan (and its file) from the instrument."""
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.delete(_url(f"scans/{scan_id}"))
            r.raise_for_status()
            return {"deleted": "scan", "id": scan_id, "status": r.status_code}

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True})
    async def scanner_delete_project(project_id: int) -> dict:
        """DESTRUCTIVE: permanently delete a project (by integer id) and its scans."""
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.delete(_url(f"projects/{project_id}"))
            r.raise_for_status()
            return {"deleted": "project", "id": project_id, "status": r.status_code}


if __name__ == "__main__":
    mcp.run()
