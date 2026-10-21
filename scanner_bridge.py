"""Bridge mode: the agent tools, served through an x9 bridge service.

The bridge is the only thing that talks to the scanner and it enforces the
rules (approvals, battery, busy scanner, request echo checks), so these tools
are thin: each one is a call to the bridge's HTTP API with this server's
token. With an `agent` token, scans wait for a person's approval unless a grant
covers them.

Config:
  SCANNER_BRIDGE_URL    Bridge API base, e.g. http://bridge.local:8795/v1
  SCANNER_BRIDGE_TOKEN  Bearer token issued by the bridge (usually role `agent`)
  SCANNER_DOWNLOAD_DIR  Where downloads are written (default ~/scanner-downloads)
"""
import asyncio
import json
import math
import os
import time
from urllib.parse import quote

import httpx

try:  # mcp 2.x renamed FastMCP to MCPServer
    from mcp.server.mcpserver import Context, Image
    from mcp.server.mcpserver import MCPServer as FastMCP
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import Context, FastMCP, Image
    from mcp.server.fastmcp.exceptions import ToolError

import scanner_mcp as direct  # download-path safety and error helpers

URL = os.environ.get("SCANNER_BRIDGE_URL", "").rstrip("/")
TOKEN = os.environ.get("SCANNER_BRIDGE_TOKEN", "")
TIMEOUT = httpx.Timeout(30.0, connect=5.0)

mcp = FastMCP("trimblex9-bridge")


_tool_errors = direct.error_wrapper("bridge")


def tool(**kw):
    """mcp.tool, with HTTP and data failures reported as tool errors."""
    return lambda fn: mcp.tool(**kw)(_tool_errors(fn))


class BridgeError(ToolError):
    """A bridge refusal, carrying its machine-readable code and hint."""


def _headers() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


def _seg(value) -> str:
    """An id as one URL path segment ('/', '?', '#' and the like escaped)."""
    s = str(value)
    if s in ("", ".", ".."):
        raise ToolError(f"invalid id {s!r}")
    return quote(s, safe="")


def _raise(r: httpx.Response) -> None:
    if r.status_code < 400:
        return
    try:
        p = r.json()
    except ValueError:
        p = None
    if isinstance(p, dict) and (p.get("code") or p.get("title")):
        msg = f"{p.get('code', r.status_code)}: {p.get('title', '')}"
        if p.get("detail"):
            msg += f" — {p['detail']}"
        if p.get("hint"):
            msg += f" (hint: {p['hint']})"
    else:
        msg = f"HTTP {r.status_code}: {r.text[:200]}"
    raise BridgeError(msg)


async def _call(method: str, path: str, body=None, params=None):
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.request(method, f"{URL}{path}", json=body, params=params, headers=_headers())
    _raise(r)
    return r.json() if r.content else None


async def _download(path: str, dest: str) -> int:
    tmp = dest + ".part"
    total = 0
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=5.0)) as c:
            async with c.stream("GET", f"{URL}{path}", headers=_headers()) as r:
                if r.status_code >= 400:
                    await r.aread()
                    _raise(r)
                with open(tmp, "wb") as fh:
                    async for chunk in r.aiter_bytes():
                        total += len(chunk)
                        fh.write(chunk)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return total


READ = {"readOnlyHint": True, "openWorldHint": True}
WRITE = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True}

# ------------------------------------------------------------------ status ---


@tool(annotations=READ)
async def scanner_status() -> dict:
    """Bridge and scanner state: connection, busy, battery, storage, leveling,
    whether the scanner moved since its last scan, and the queue length."""
    return await _call("GET", "/status")


@tool(annotations=READ)
async def scanner_scan_modes() -> list:
    """The scan modes: name, range (m), angular step (rad), full-dome point
    count, fixed overhead (s) and full-dome time (s). Use the names in scans."""
    return await _call("GET", "/scan-modes")


@tool(annotations=READ)
async def scanner_projects() -> list:
    """Projects on the scanner (project_id, name, scan_count)."""
    return await _call("GET", "/projects")


@tool(annotations=WRITE)
async def scanner_create_project(name: str) -> dict:
    """Create a project (1-64 characters, no '/'). Returns its project_id."""
    return await _call("POST", "/projects", {"name": name})


# ------------------------------------------------------------------- scans ---


@tool(annotations=READ)
async def scanner_scans(project_id: str | None = None) -> list:
    """Scans, newest first: scan_id, kind (full_dome or area), station, mode,
    point count, parent station scan for area scans, and timings when the
    bridge ran them."""
    return await _call("GET", "/scans", params={"project_id": project_id} if project_id else None)


@tool(annotations=READ)
async def scanner_scan(scan_id: str) -> dict:
    """One scan's record."""
    return await _call("GET", f"/scans/{_seg(scan_id)}")


@tool(annotations=WRITE)
async def scanner_start_scan(
    project_id: str, mode: str, color: bool = False,
    start_ha: float | None = None, delta_ha: float | None = None,
    start_va: float | None = None, delta_va: float | None = None,
    note: str | None = None,
) -> dict:
    """Queue a scan. Without a window it is a full dome; give all four window
    angles (radians, VA from the zenith; get them from scanner_aim) for an area
    scan. Color adds ~150 s. The bridge checks the rules; with an agent token
    the job usually waits in `awaiting_approval` until a person approves it in
    the bridge app, unless a grant covers it. Returns the job: follow it with
    scanner_wait_for_job."""
    window = [start_ha, delta_ha, start_va, delta_va]
    if any(v is not None for v in window) and not all(v is not None for v in window):
        raise ToolError("give all four window angles for an area scan, or none for a full dome")
    body = {"project_id": project_id, "kind": "area" if window[0] is not None else "full_dome",
            "mode": mode, "color": color}
    if window[0] is not None:
        body["window"] = {"start_ha": start_ha % math.tau, "delta_ha": delta_ha,
                          "start_va": start_va, "delta_va": delta_va}
    if note:
        body["note"] = note
    return await _call("POST", "/scans", body)


# -------------------------------------------------------------------- jobs ---


@tool(annotations=READ)
async def scanner_job(job_id: str) -> dict:
    """One job: state (awaiting_approval, queued, running, succeeded, failed,
    cancelled), phase, progress 0-1, result and any problem."""
    return await _call("GET", f"/jobs/{_seg(job_id)}")


@tool(annotations=READ)
async def scanner_wait_for_job(ctx: Context, job_id: str, timeout_s: int = 900,
                               return_on_approval: bool = True) -> dict:
    """Wait for a job to finish, reporting progress when the client supports it.
    If the job is waiting for a person's approval and `return_on_approval` is
    true, returns at once so you can ask them; call again afterwards.
    Returns {job, done, timed_out, waiting_for_approval, elapsed_s}."""
    timeout_s = max(1, min(int(timeout_s), 7200))
    start = time.monotonic()
    while True:
        job = await _call("GET", f"/jobs/{_seg(job_id)}")
        elapsed = round(time.monotonic() - start, 1)
        state = job.get("state")
        if state in ("succeeded", "failed", "cancelled"):
            return {"job": job, "done": True, "timed_out": False, "waiting_for_approval": False, "elapsed_s": elapsed}
        if state == "awaiting_approval" and return_on_approval:
            return {"job": job, "done": False, "timed_out": False, "waiting_for_approval": True, "elapsed_s": elapsed,
                    "note": "Ask the operator to approve this job in the bridge app, then wait again."}
        if job.get("progress") is not None:
            try:
                await ctx.report_progress(round(job["progress"] * 100, 1), 100)
            except Exception:
                pass
        if elapsed >= timeout_s:
            return {"job": job, "done": False, "timed_out": True, "waiting_for_approval": state == "awaiting_approval",
                    "elapsed_s": elapsed}
        await asyncio.sleep(1.0)


@tool(annotations={"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True})
async def scanner_cancel_job(job_id: str) -> dict:
    """Cancel a job of yours. A running scan is stopped; the scanner still files
    the partial sweep, which the cancelled job names as partial_scan_id (bridge
    0.1.1+). Keep it or delete it."""
    return await _call("POST", f"/jobs/{_seg(job_id)}/cancel")


# ------------------------------------------------------------------ aiming ---


@tool(annotations=READ)
async def scanner_preview(scan_id: str) -> list:
    """Look at a scan's preview: a grey laser-intensity panorama (full dome)
    or close-up (area scan). Returns the image and its size in pixels; boxes
    for scanner_aim are given in these pixel coordinates (y down)."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=5.0)) as c:
        r = await c.get(f"{URL}/scans/{_seg(scan_id)}/preview", headers=_headers())
    _raise(r)
    w, h = _jpeg_dims(r.content)
    return [f"Preview of scan {scan_id}: {w} x {h} px. Give scanner_aim boxes in these pixels (y down; "
            f"x1 < x0 wraps across the panorama seam).", Image(data=r.content, format="jpeg")]


def _jpeg_dims(data: bytes) -> tuple[int, int]:
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            break
        marker, seglen = data[i + 1], int.from_bytes(data[i + 2:i + 4], "big")
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
        i += 2 + seglen
    return 0, 0


@tool(annotations=READ)
async def scanner_aim(reference_scan_id: str, x0: int, y0: int, x1: int, y1: int, margin_deg: float = 0.5) -> dict:
    """Turn a box drawn on a scan's preview (see scanner_preview) into an
    area-scan window. The reference scan must be from where the scanner stands
    now (the bridge refuses if it moved). Pass the returned window's four
    angles to scanner_start_scan."""
    return await _call("POST", "/aim", {"reference_scan_id": reference_scan_id,
                                        "box": {"x0": x0, "y0": y0, "x1": x1, "y1": y1},
                                        "margin_rad": math.radians(max(0.0, margin_deg))})


# ----------------------------------------------------------------- exports ---


@tool(annotations=WRITE)
async def scanner_export(scan_id: str, format: str = "ply", intensity: str = "auto") -> dict:
    """Decode a scan to levelled points (format `ply`, `las`, or `copc` for a
    Cloud Optimized Point Cloud, bridge 0.6.0+; intensity `auto`, `preview` or
    `none`). Returns a job; when it succeeds its result has an export_id for
    scanner_export_info and scanner_download_export."""
    return await _call("POST", f"/scans/{_seg(scan_id)}/exports", {"format": format, "intensity": intensity})


@tool(annotations=READ)
async def scanner_export_info(export_id: str) -> dict:
    """An export's point count, bounds and automatic checks (point count,
    levelled, coordinates, intensity source). Mention any failed check."""
    return await _call("GET", f"/exports/{_seg(export_id)}")


@tool(annotations=WRITE)
async def scanner_measure(scan_id: str, kind: str = "opening") -> dict:
    """Measure a feature in a scan on the bridge (bridge 0.4.0+). `opening`:
    a window's finished opening (the hole in the wall face) from a levelled
    close-up with wall on every side. Returns a job; follow it with
    scanner_wait_for_job, then read the result with scanner_measurement."""
    return await _call("POST", f"/scans/{_seg(scan_id)}/measurements", {"kind": kind})


@tool(annotations=READ)
async def scanner_measurement(measurement_id: str) -> list:
    """A measurement's values (metres, with uncertainties), checks, and its
    evidence image: the wall face (grey), the opening (green) and the
    measuring row and column (red). Look at the image before reporting: the
    green area should fill the window up to the wall. Mention failed checks."""
    m = await _call("GET", f"/measurements/{_seg(measurement_id)}")
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{URL}/measurements/{_seg(measurement_id)}/evidence", headers=_headers())
    if r.status_code >= 400:
        return [json.dumps(m, indent=2)]
    return [json.dumps(m, indent=2), Image(data=r.content, format="png")]


@tool(annotations=READ)
async def scanner_coverage(scan_id: str) -> list:
    """What a scan covered (bridge 0.6.0+): point spacing at full resolution
    (median, 90th percentile, and the share that is fine ≤ 6 mm, good ≤ 12,
    coarse ≤ 25, sparse beyond) and a plan from above in 10 cm cells: green
    seen, red shadow (floor nearer than the farthest return in that direction
    with no points at any height: something blocked it; full domes only, as
    `shadows_assessed` says), dark nothing; a white cross marks the scanner.
    Use it to decide whether a station saw enough or where the next station
    should go; a shadow behind furniture or a door is what another station
    fills. The first call on a scan decodes it (seconds)."""
    cov = await _call("GET", f"/scans/{_seg(scan_id)}/coverage")
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{URL}/scans/{_seg(scan_id)}/coverage/image", headers=_headers())
    if r.status_code >= 400:
        return [json.dumps(cov, indent=2)]
    return [json.dumps(cov, indent=2), Image(data=r.content, format="png")]


@tool(annotations=WRITE)
async def scanner_download_export(export_id: str, filename: str | None = None, overwrite: bool = False) -> dict:
    """Download an export into SCANNER_DOWNLOAD_DIR (plain file name; no paths)."""
    info = await _call("GET", f"/exports/{_seg(export_id)}")
    fmt = info.get("format") or "ply"
    default = direct._default_name(export_id, "laz" if fmt == "copc" else fmt)
    if fmt == "copc":
        default = default[: -len(".laz")] + ".copc.laz"
    dest = direct._dest(filename, default, overwrite)
    n = await _download(f"/exports/{_seg(export_id)}/file", dest)
    return {"export_id": export_id, "bytes": n, "path": dest}


@tool(annotations=WRITE)
async def scanner_download_scanfile(scan_id: str, filename: str | None = None, overwrite: bool = False) -> dict:
    """Download a scan's raw file (.tzf) into SCANNER_DOWNLOAD_DIR."""
    dest = direct._dest(filename, direct._default_name(scan_id, "tzf"), overwrite)
    n = await _download(f"/scans/{_seg(scan_id)}/tzf", dest)
    return {"scan_id": scan_id, "bytes": n, "path": dest}


# ----------------------------------------------------------------- recipes ---


@tool(annotations=READ)
async def scanner_recipes() -> list:
    """Installed recipes (recipe_id, title, version, summary)."""
    return await _call("GET", "/recipes")


@tool(annotations=READ)
async def scanner_recipe(recipe_id: str) -> dict:
    """A recipe's inputs and steps (action, person and check steps)."""
    return await _call("GET", f"/recipes/{_seg(recipe_id)}")


@tool(annotations=WRITE)
async def scanner_start_run(recipe_id: str, project_id: str, inputs: dict | None = None) -> dict:
    """Start running a recipe in a project. The bridge executes its steps; it
    pauses with `waiting_for: approval` (a person must approve a scan) or
    `waiting_for: input` (a person step: supply its `needs` with
    scanner_advance_run). Follow it with scanner_run."""
    return await _call("POST", "/runs", {"recipe_id": recipe_id, "project_id": project_id, "inputs": inputs or {}})


@tool(annotations=READ)
async def scanner_run(run_id: str) -> dict:
    """A run: state, what it is waiting for, the current step, and each step's
    state, job and output."""
    return await _call("GET", f"/runs/{_seg(run_id)}")


@tool(annotations=WRITE)
async def scanner_advance_run(run_id: str, step_id: str, input: dict) -> dict:
    """Complete a person step with its `needs` (e.g. {"box": {"x0":…, "y0":…,
    "x1":…, "y1":…}} drawn on the `preview_of` step's preview)."""
    return await _call("POST", f"/runs/{_seg(run_id)}/advance", {"step_id": step_id, "input": input})


@tool(annotations=WRITE)
async def scanner_propose(run_id: str, step_id: str, input: dict, note: str | None = None) -> dict:
    """Propose a waiting person step's answer for a person to confirm, e.g. the
    box around the window you found on the `preview_of` step's preview:
    {"box": {"x0":…, "y0":…, "x1":…, "y1":…}} (x1 < x0 crosses the panorama
    seam). Say in `note` what you found and how sure you are. The step keeps
    waiting until a person confirms or changes it in the bridge app (bridge
    0.3.0+); a newer proposal replaces an older one."""
    body = {"step_id": step_id, "input": input}
    if note:
        body["note"] = note
    return await _call("POST", f"/runs/{_seg(run_id)}/propose", body)


@tool(annotations={"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True})
async def scanner_cancel_run(run_id: str) -> dict:
    """Stop a run; a scan in flight is cancelled too."""
    return await _call("POST", f"/runs/{_seg(run_id)}/cancel")


# --------------------------------------------------------------------- log ---


@tool(annotations=READ)
async def scanner_log(project_id: str | None = None, since: str | None = None) -> list:
    """The field log, oldest first (filter by project or RFC 3339 time)."""
    params = {k: v for k, v in (("project_id", project_id), ("since", since)) if v}
    return await _call("GET", "/log", params=params or None)


@tool(annotations=WRITE)
async def scanner_add_note(text: str, project_id: str | None = None) -> dict:
    """Add an observation to the field log."""
    body = {"text": text}
    if project_id:
        body["project_id"] = project_id
    return await _call("POST", "/log", body)


# ------------------------------------------------------------------ prompt ---


@mcp.prompt()
async def run_recipe(recipe_id: str, project_id: str) -> str:
    """Walk a scanning recipe with the person, using the bridge tools."""
    r = await _call("GET", f"/recipes/{_seg(recipe_id)}")
    lines = [f"Run the recipe \"{r['title']}\" (v{r['version']}) in project {project_id}.", r.get("summary", ""), ""]
    if r.get("inputs"):
        lines.append("First ask the person for these inputs:")
        for i in r["inputs"]:
            choices = f" (one of: {', '.join(i['choices'])})" if i.get("choices") else ""
            lines.append(f"- {i['name']}: {i.get('prompt', '')}{choices}")
        lines.append("")
    lines.append(f"Then call scanner_start_run(recipe_id=\"{recipe_id}\", project_id=\"{project_id}\", inputs=…) "
                 "and follow it with scanner_run. The steps:")
    for n, s in enumerate(r["steps"], 1):
        how = {
            "action": f"the bridge calls {s.get('operation_id')} itself",
            "person": "work out " + ", ".join(s.get("needs", [])) + " yourself"
                      + (f" from scanner_preview of step {s['preview_of']}'s scan_id" if s.get("preview_of") else "")
                      + ", then scanner_propose it with a note; the person confirms or adjusts it in the bridge app",
            "check": f"the bridge checks {', '.join(s.get('expect', []))} of step {s.get('of')}",
        }[s["kind"]]
        title = s.get("title") or s["step_id"]
        extra = f" — {s['instructions']}" if s.get("instructions") else ""
        lines.append(f"{n}. {title} [{s['kind']}: {how}]{extra}")
    lines += ["", "AI-first: you find the window, choose the box and propose the measurement; the person only validates.",
              "Use scanner_advance_run yourself only if the person tells you to go ahead without confirming.",
              "When the run shows waiting_for = approval, ask the person to approve the scan in the bridge app.",
              "Report each measured value with its uncertainty, and say plainly if a check failed or a step was declined."]
    return "\n".join(lines)


def main() -> None:
    if not URL or not TOKEN:
        raise SystemExit("bridge mode needs SCANNER_BRIDGE_URL and SCANNER_BRIDGE_TOKEN")
    mcp.run()
