# TrimbleX9-mcp

An [MCP](https://modelcontextprotocol.io) server for the **Trimble X9**
terrestrial laser scanner: control and data‑retrieval tools over the
instrument's local HTTP/REST API (`/api/v1`) on its USB or WiFi link. An agent
can query status, list projects/scans, read the scan‑mode menu, and download
point‑cloud files — and, behind an explicit opt‑in, create projects and start
scans.

**Clean‑room** interoperability: implemented purely against the instrument's
*observable* HTTP/JSON network behavior. Independent project, **not affiliated
with or endorsed by Trimble Inc.**; "Trimble" and "X9" are trademarks of their
respective owners.

## Tools

**Always available (read / query / download):**
- `scanner_status` — battery, leveling, storage, wifi, errors
- `scanner_projects` — list projects
- `scanner_scans` — list scans (compact: id, mode, points, file, project)
- `scanner_scan_modes` — the scan‑mode menu (density / range / time)
- `scanner_errors` — current error list
- `scanner_scanfile_info(id)` — point‑cloud file metadata
- `scanner_download_scanfile(id, dest_path)` — download a point cloud
- `scanner_download_preview(id, dest_path)` — download the preview JPEG

**Only when `SCANNER_ALLOW_ACTUATION` is truthy (moves the instrument / destructive):**
- `scanner_launch_scan(project_name, project_uuid, scan_mode)` — start a scan
- `scanner_create_project(project_name, project_uuid)`
- `scanner_delete_scan(scan_id)` · `scanner_delete_project(project_id)`

By default these are **not registered**, so an agent sees a read‑only server.
`scanner_launch_scan` requires BOTH `project_name` and `project_uuid` to target
a project, and `scan_mode` must be one of the names from `scanner_scan_modes`.

## Config (env)

- `SCANNER_BASE` — API base URL. Default `http://192.168.42.1:34567/api/v1`
  (the instrument over its USB network link; use its WiFi‑AP address otherwise).
- `SCANNER_ALLOW_ACTUATION` — set to `1` to enable the actuating/destructive tools.

## Setup & run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scanner_mcp.py                              # read-only
SCANNER_ALLOW_ACTUATION=1 .venv/bin/python scanner_mcp.py    # + actuation
```

Register with an MCP client (read‑only):

```bash
claude mcp add x9 -- /path/to/.venv/bin/python /path/to/scanner_mcp.py
```

Add `--env SCANNER_ALLOW_ACTUATION=1` (and `--env SCANNER_BASE=...`) to enable
scanning. Run it on the host the instrument is connected to; a remote agent
reaches it by running it there or over SSH.

## License

MIT (see `LICENSE`). Trademarks are the property of their respective owners.
