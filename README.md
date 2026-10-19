# TrimbleX9-mcp

An [MCP](https://modelcontextprotocol.io) server for the **Trimble X9**
terrestrial laser scanner: control and data‑retrieval tools over the
instrument's local HTTP/REST API (`/api/v1`) on its USB or WiFi link. An agent
can query status, list projects/scans, read the scan‑mode menu, and download
point‑cloud files — and, behind an explicit opt‑in, create projects and start
full‑dome or area scans.

**Clean‑room** interoperability: written from scratch against the instrument's
network API, with behavior verified on a live instrument. No vendor code,
firmware or vendor documentation is included or redistributed. Independent
project, **not affiliated with or endorsed by Trimble Inc.**; "Trimble" and
"X9" are trademarks of their respective owners.

## Install

```bash
uvx --from git+https://github.com/cessnajim/TrimbleX9-mcp@v0.1.2 trimblex9-mcp
# or
pipx install git+https://github.com/cessnajim/TrimbleX9-mcp@v0.1.2
# no git on the host (e.g. a minimal Raspberry Pi image):
pip install https://github.com/cessnajim/TrimbleX9-mcp/archive/refs/tags/v0.1.2.tar.gz
```

Register with an MCP client (read‑only):

```bash
claude mcp add trimblex9 -- uvx --from git+https://github.com/cessnajim/TrimbleX9-mcp@v0.1.2 trimblex9-mcp
```

Add `--env SCANNER_ALLOW_ACTUATION=1` (and `--env SCANNER_BASE=...`,
`--env SCANNER_DOWNLOAD_DIR=...`) as needed. Run it on the host the instrument
is connected to; a remote agent reaches it by running it there or over SSH.

## Tools

**Always available (read / query / download):**
- `scanner_status` — battery, leveling, storage, wifi (passphrase masked), errors
- `scanner_projects` — list projects
- `scanner_scans` — list scans (compact: id, scanUUID, mode, points, file, project)
- `scanner_scan_modes` — the scan‑mode menu (density / range / time)
- `scanner_errors` — current error list
- `scanner_tasks` — running tasks with state and **percent complete**; empty = idle
- `scanner_wait_for_scan(timeout_s=900, poll_s=2)` — block until the instrument
  is idle, reporting progress to the client when supported
- `scanner_scanfile_info(id)` — point‑cloud file metadata
- `scanner_download_scanfile(id, filename=None, overwrite=False)` — download a point cloud
- `scanner_download_preview(id, filename=None, overwrite=False)` — download the preview JPEG
- `scanner_preview_to_angles(scan_filename, preview_filename, x0, y0, x1, y1, margin_deg=0.5)`
  — turn a box on a downloaded preview into area‑scan angles (local, no instrument call)

**Only when `SCANNER_ALLOW_ACTUATION` is truthy (moves the instrument / destructive):**
- `scanner_launch_scan(project_name, project_uuid, scan_mode, color=False)` — full‑dome scan
- `scanner_launch_area_scan(project_name, project_uuid, scan_mode, start_ha, delta_ha, start_va, delta_va, color=False)`
- `scanner_cancel_task(task_uuid)` — stop a running scan (the partial scan is discarded)
- `scanner_create_project(project_name, project_uuid)`
- `scanner_delete_scan(scan_uuid)` · `scanner_delete_project(project_uuid)`

By default these are **not registered**, so an agent sees a read‑only server.
A typical scan is `scanner_launch_scan` → `scanner_wait_for_scan` →
`scanner_scans` → `scanner_download_scanfile`.

### Launch safety

Launches are refused while any task is running or queued, since another
client may be using the instrument. The instrument accepts (HTTP 202) requests it cannot parse and then runs a
default full‑dome scan, and it ignores `projectUUID` unless `projectName` also
matches. So the launch tools check the project (name **and** UUID), the scan
mode and the angles before sending anything, then compare the request the
instrument echoes back with what was sent; on any mismatch the task is
cancelled immediately and the tool returns an error.

### Area scans

Angles are radians: HA is horizontal (0..2π), VA is measured down from the
zenith (0..π). A full‑dome scan file records the angles of its first row and
column, so a region picked on its preview maps straight to instrument angles
while the instrument stays on the same station:

1. `scanner_launch_scan` (any mode) → `scanner_wait_for_scan`
2. `scanner_download_scanfile` + `scanner_download_preview` of that scan
3. pick a box on the preview → `scanner_preview_to_angles`
4. `scanner_launch_area_scan` with those angles

Area scans are saved as `<station>_<n>.tzf` next to the station's full‑dome
scan. A small window without color takes about a minute, mostly leveling.

### Deleting by UUID

Scan and project `id`s are list positions and are renumbered after every
delete, so the delete tools take the stable `scanUUID` / `projectUUID`, resolve
the current id immediately before deleting, and confirm afterwards that exactly
that record is gone.

### Downloads stay in one directory

Download tools write **only** inside `SCANNER_DOWNLOAD_DIR`. They take a plain
file name (letters, digits, `.`, `_`, `-`; default `scan-<id>.tzf` /
`scan-<id>-preview.jpg`). Paths, `..`, leading dots and symlinks that escape the
directory are rejected, and existing files are kept unless `overwrite=true`.
Files are written to a `.part` temp file and renamed when complete.

## Config (env)

- `SCANNER_BASE` — API base URL. Default `http://192.168.42.1:34567/api/v1`
  (the instrument over its USB network link; use its WiFi‑AP address otherwise).
- `SCANNER_ALLOW_ACTUATION` — set to `1` to enable the actuating/destructive tools.
- `SCANNER_DOWNLOAD_DIR` — where downloads go. Default `~/scanner-downloads`.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e . pytest
.venv/bin/pytest -q                      # offline tests against a stub instrument
.venv/bin/trimblex9-mcp                  # run (stdio)
```

Requires `mcp>=1.2,<2`: the 2.x SDK renamed `FastMCP`; a port is planned.

## License

MIT (see `LICENSE`).
