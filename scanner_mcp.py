#!/usr/bin/env python3
"""scanner-mcp — an MCP server for a tripod LiDAR / terrestrial laser scanner
that exposes a local HTTP/REST API (`/api/v1`) over its USB or WiFi
link.

Read / query / download tools are always available. Actuating tools (start a
scan, create/delete projects, delete scans) are only registered when the
environment variable SCANNER_ALLOW_ACTUATION is truthy ("1"/"true"/"yes") —
"receive-only by default", because starting a scan moves the instrument and
fires its laser, and the delete tools are destructive.

Config:
  SCANNER_BASE             HTTP/REST API base URL. Default http://192.168.42.1:34567/api/v1
  SCANNER_ALLOW_ACTUATION  Enable actuating/destructive tools when set truthy.

Transport: stdio (run under an MCP client). Independent project, not affiliated
with any instrument manufacturer.
"""
import os
import httpx
from mcp.server.fastmcp import FastMCP

BASE = os.environ.get("SCANNER_BASE", "http://192.168.42.1:34567/api/v1").rstrip("/")
ALLOW_ACTUATION = os.environ.get("SCANNER_ALLOW_ACTUATION", "").lower() in ("1", "true", "yes")
TIMEOUT = httpx.Timeout(15.0, connect=5.0)

mcp = FastMCP("scanner")


def _url(path: str) -> str:
    return f"{BASE}/{path.lstrip('/')}"


async def _get_json(path: str):
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(_url(path))
        r.raise_for_status()
        return r.json()


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
async def scanner_scanfile_info(scan_file_id: int) -> dict:
    """Get metadata for one point-cloud file: file path, size, point count, and
    whether it has been downloaded."""
    return await _get_json(f"tzfs/{scan_file_id}")


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def scanner_download_scanfile(scan_file_id: int, dest_path: str) -> dict:
    """Download a scan's point-cloud file to a local path on this host. Streams
    the full file (the instrument does not support partial/Range requests).
    Returns the bytes written and the destination path."""
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)) or ".", exist_ok=True)
    total = 0
    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=5.0)) as c:
        async with c.stream("GET", _url(f"tzfs/{scan_file_id}/file")) as r:
            r.raise_for_status()
            with open(dest_path, "wb") as fh:
                async for chunk in r.aiter_bytes():
                    total += len(chunk)
                    fh.write(chunk)
    return {"scanFileId": scan_file_id, "bytes": total, "path": dest_path}


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def scanner_download_preview(scan_file_id: int, dest_path: str) -> dict:
    """Download a scan's panoramic preview JPEG to a local path on this host."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(_url(f"tzfs/{scan_file_id}/preview"))
        r.raise_for_status()
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)) or ".", exist_ok=True)
        with open(dest_path, "wb") as fh:
            fh.write(r.content)
    return {"scanFileId": scan_file_id, "bytes": len(r.content), "path": dest_path}


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
        from scanner_scan_modes. Returns the accepted task; poll scanner_scans /
        scanner_status to follow progress. Ensure the instrument is safely
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
