# Provenance

This MCP server talks to a Trimble X9 laser scanner over the scanner's own
local HTTP API (or, in bridge mode, to a bridge service that does), for
interoperability with a scanner its author owns. This file records how the
knowledge of that API was obtained.

## What this repository does not contain

- No vendor source code, binaries, SDKs, firmware or documentation.
- No names of vendor-internal programs, libraries, classes or functions.

## How the API knowledge was obtained

**Every request the server makes was checked against our own scanner** by
sending it and reading the response (October 2026). The tests use a stub of
the scanner, not captured data.

**How it was first discovered.** The port, base path and route names were
first identified by reading text strings in the vendor's Windows field
software, installed on our own tablet (nothing was executed, decompiled or
copied). The area-scan value `AreaScan` was first seen the same way. Each was
treated as a hypothesis and kept only after the scanner confirmed it. This is
interoperability reverse engineering of our own equipment, not a two-team
clean-room process.

## Behavior the server relies on, as observed on the scanner

| Behavior | Observed |
| --- | --- |
| HTTP on port 34567, base `/api/v1`, over USB (192.168.42.1) or the scanner's Wi-Fi (192.168.43.1) | Requests answered on both links |
| `status`, `projects`, `scans`, `scans/modes`, `tasks`, `errors`, `tzfs/{id}`, `tzfs/{id}/file`, `tzfs/{id}/preview` | Responses read and used |
| `POST /projects` with `projectName` + `projectUUID`; both are needed for `POST /scans` to target a project | Projects created; scans landed in the intended project only with both |
| `POST /scans` answers 202 with a task echoing the request; for a request it can't parse it still answers 202 and runs a default full dome | Observed; hence the echo check that cancels a mismatch |
| `scanType` `FullDome` (the default in the echo) and `AreaScan` with radian windows, vertical angle from the zenith | Area scans covered exactly the requested windows |
| Scan ids are list positions that shift after a delete; `scanUUID` is stable | Observed after `DELETE /scans/{id}`; deletes resolve the id right before deleting |
| `DELETE /tasks/{id}` stops a running task | Used by `scanner_cancel_task`; confirm on your scanner before relying on it |

This describes the method; it is not legal advice.
