"""Bridge-mode tests: the MCP tools against a real x9-bridge and x9-mock.

Skipped unless X9_BRIDGE_BIN points at a directory with the `x9-bridge` and
`x9-mock` binaries (from x9-controller: cargo build --release). With
X9_FIXTURES (dome/area .tzf + .jpg, as for the bridge's e2e test) the preview,
aim and export tools are exercised too.

Run: X9_BRIDGE_BIN=…/target/release pytest -q tests/test_bridge_mode.py
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

import pytest

BIN = os.environ.get("X9_BRIDGE_BIN")
FIX = os.environ.get("X9_FIXTURES")
MOCK, BRIDGE = "127.0.0.1:34611", "127.0.0.1:8811"
AGENT, OPERATOR = "agent-token-0123456789abcdef", "operator-token-0123456789abcdef"

pytestmark = pytest.mark.skipif(not BIN, reason="set X9_BRIDGE_BIN to run bridge-mode tests")


def _up(url):
    for _ in range(100):
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError(f"{url} did not come up")


def _operator(method, path, body=None):
    req = urllib.request.Request(f"http://{BRIDGE}/v1{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {OPERATOR}", "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req).read() or b"null")


class _Ctx:
    def __init__(self):
        self.progress = []

    async def report_progress(self, done, total):
        self.progress.append(done)


@pytest.fixture(scope="module")
def tools():
    tmp = tempfile.mkdtemp()
    tok = os.path.join(tmp, "tokens")
    with open(tok, "w") as f:
        f.write(f"claude agent {AGENT}\njim operator {OPERATOR}\n")
    recipes = os.path.join(tmp, "recipes")
    os.makedirs(recipes)
    env_m = {**os.environ, "X9_MOCK_ADDR": MOCK, "X9_MOCK_SPEED": "20"}
    if FIX:
        env_m["X9_MOCK_FIXTURES"] = FIX
    procs = [subprocess.Popen([f"{BIN}/x9-mock"], env=env_m, stderr=subprocess.DEVNULL)]
    _up(f"http://{MOCK}/api/v1/status")
    recipe = os.path.join(os.path.dirname(BIN.rstrip("/")), "..", "crates", "x9-bridge", "tests", "recipes", "window-measurement.toml")
    if os.path.exists(recipe):
        with open(recipe) as src, open(os.path.join(recipes, "window-measurement.toml"), "w") as dst:
            dst.write(src.read())
    procs.append(subprocess.Popen([f"{BIN}/x9-bridge"], stderr=subprocess.DEVNULL, env={
        **os.environ, "X9_BASE": f"http://{MOCK}/api/v1", "BRIDGE_ADDR": BRIDGE, "BRIDGE_TOKENS": tok,
        "BRIDGE_POLL_MS": "200", "BRIDGE_DATA": os.path.join(tmp, "data"), "BRIDGE_RECIPES": recipes}))
    _up(f"http://{BRIDGE}/v1/health")
    os.environ.update(SCANNER_BRIDGE_URL=f"http://{BRIDGE}/v1", SCANNER_BRIDGE_TOKEN=AGENT,
                      SCANNER_DOWNLOAD_DIR=os.path.join(tmp, "downloads"))
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    for m in ("scanner_bridge", "scanner_mcp"):
        sys.modules.pop(m, None)
    import scanner_bridge
    yield scanner_bridge
    for p in procs:
        p.terminate()


def run(coro):
    return asyncio.run(coro)


def fn(tool):
    return getattr(tool, "fn", tool)


def test_tools_are_listed_without_operator_tools(tools):
    names = {t.name for t in run(tools.mcp.list_tools())}
    assert {"scanner_status", "scanner_start_scan", "scanner_preview", "scanner_aim", "scanner_export",
            "scanner_start_run", "scanner_advance_run", "scanner_propose", "scanner_measure", "scanner_measurement"} <= names
    assert not any("approve" in n or "grant" in n or "delete" in n for n in names)


def test_scan_waits_for_approval_then_succeeds(tools):
    st = run(fn(tools.scanner_status)())
    assert st["scanner"]["connected"]
    p = run(fn(tools.scanner_create_project)("mcp-bridge-test"))
    job = run(fn(tools.scanner_start_scan)(p["project_id"], "STD_L0", note="station 1"))
    assert job["state"] == "awaiting_approval"
    w = run(fn(tools.scanner_wait_for_job)(_Ctx(), job["job_id"]))
    assert w["waiting_for_approval"] and not w["done"]
    _operator("POST", f"/jobs/{job['job_id']}/approve")
    ctx = _Ctx()
    w = run(fn(tools.scanner_wait_for_job)(ctx, job["job_id"], timeout_s=120))
    assert w["done"] and w["job"]["state"] == "succeeded"
    assert ctx.progress, "progress was reported while scanning"
    scans = run(fn(tools.scanner_scans)(p["project_id"]))
    assert scans[0]["scan_id"] == w["job"]["result"]["scan_id"] and scans[0]["kind"] == "full_dome"
    tools._state = {"project_id": p["project_id"], "station": scans[0]["scan_id"]}


def test_errors_carry_the_bridge_code(tools):
    with pytest.raises(tools.BridgeError, match="invalid_request"):
        run(fn(tools.scanner_start_scan)(tools._state["project_id"], "STD_L9"))
    with pytest.raises(tools.ToolError):
        run(fn(tools.scanner_start_scan)(tools._state["project_id"], "STD_L2", start_ha=1.0))
    with pytest.raises(tools.BridgeError, match="not_found"):  # one escaped path segment
        run(fn(tools.scanner_scan)("../status"))


@pytest.mark.skipif(not FIX, reason="needs X9_FIXTURES")
def test_preview_aim_export(tools):
    station = tools._state["station"]
    out = run(fn(tools.scanner_preview)(station))
    assert "2082 x 815 px" in out[0] and out[1].data[:2] == b"\xff\xd8"
    a = run(fn(tools.scanner_aim)(station, 523, 418, 674, 564, margin_deg=0))
    assert abs(a["window"]["start_ha"] - 5.157144) < 1e-4
    ej = run(fn(tools.scanner_export)(station, "las", "none"))
    w = run(fn(tools.scanner_wait_for_job)(_Ctx(), ej["job_id"], timeout_s=180))
    info = run(fn(tools.scanner_export_info)(w["job"]["result"]["export_id"]))
    assert info["point_count"] == 6281859 and all(c["ok"] for c in info["checks"])
    d = run(fn(tools.scanner_download_export)(info["export_id"]))
    assert d["bytes"] == 227 + 20 * 6281859 and os.path.exists(d["path"])


def test_recipe_prompt_and_run(tools):
    recipes = run(fn(tools.scanner_recipes)())
    if not recipes:
        pytest.skip("window recipe not found next to the binaries")
    text = run(fn(tools.run_recipe)("window-measurement", tools._state["project_id"]))
    assert "Mark the window" in text and "scanner_propose" in text and "approve" in text
    r = run(fn(tools.scanner_start_run)("window-measurement", tools._state["project_id"], {"purpose": "blinds"}))
    for _ in range(100):
        r = run(fn(tools.scanner_run)(r["run_id"]))
        if r["waiting_for"] == "approval":
            break
        time.sleep(0.1)
    assert r["waiting_for"] == "approval"
    r = run(fn(tools.scanner_cancel_run)(r["run_id"]))
    for _ in range(50):
        r = run(fn(tools.scanner_run)(r["run_id"]))
        if r["state"] == "cancelled":
            break
        time.sleep(0.1)
    assert r["state"] == "cancelled"


def test_log_and_note(tools):
    run(fn(tools.scanner_add_note)("Tripod on the rug; slight sway", tools._state["project_id"]))
    log = run(fn(tools.scanner_log)(tools._state["project_id"]))
    assert any(e["text"].startswith("Tripod on the rug") and e["author"] == "claude" for e in log)
