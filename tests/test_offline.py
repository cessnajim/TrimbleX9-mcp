"""Offline tests against a stub instrument. Run: pytest -q"""
import asyncio
import http.server
import json
import math
import os
import struct
import sys
import tempfile
import threading

P_UUID = "1a89f533-ea7f-429c-9ba5-54a1e11c6159"
MODES = [{"scanMode": "HS_L1"}, {"scanMode": "STD_L0"}]
DEFAULTS = {"scanMode": "STD_L0", "scanType": "FullDome", "colorMode": "ON", "fastMode": False,
            "projectName": "probe", "projectUUID": P_UUID,
            "startHA": -1, "deltaHA": 0, "startVA": -1, "deltaVA": 0}
STATE = {}


def reset():
    STATE.update(scans=[{"scanUUID": f"s-{i}", "tzf": {"file": f"probe/{i}.tzf"}} for i in range(4)],
                 tasks=[], cancelled=[], fallback=False)


class Stub(http.server.BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path.removeprefix("/api/v1/")
        if p == "projects":
            return self._send([{"id": 0, "projectName": "probe", "projectUUID": P_UUID}])
        if p == "scans/modes":
            return self._send(MODES)
        if p == "scans":  # ids are list positions, like the instrument
            return self._send([dict(s, id=i) for i, s in enumerate(STATE["scans"])])
        if p == "tasks":
            return self._send(STATE["tasks"])
        self._send({}, 404)

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        echo = dict(DEFAULTS) if STATE["fallback"] else dict(DEFAULTS, **req)
        task = {"id": 7, "taskUUID": "t-7", "state": "idle", "percentage": 0, "scanRequest": echo}
        STATE["tasks"] = [task]
        self._send(task, 202)

    def do_DELETE(self):
        kind, _, ident = self.path.removeprefix("/api/v1/").partition("/")
        if kind == "scans" and int(ident) < len(STATE["scans"]):
            STATE["scans"].pop(int(ident))
            return self._send({})
        if kind == "tasks":
            STATE["cancelled"].append(ident)
            STATE["tasks"] = []
            return self._send({})
        self._send({}, 404)

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stub)
threading.Thread(target=srv.serve_forever, daemon=True).start()
DL = tempfile.mkdtemp()
os.environ.update(SCANNER_BASE=f"http://127.0.0.1:{srv.server_address[1]}/api/v1",
                  SCANNER_ALLOW_ACTUATION="1", SCANNER_DOWNLOAD_DIR=DL)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import scanner_mcp as m  # noqa: E402


def call(name, **args):
    """Tool result as plain data (FastMCP returns content blocks, or a
    (content, structured) tuple in newer 1.x releases)."""
    r = asyncio.run(m.mcp.call_tool(name, args))
    if isinstance(r, tuple):
        return r[1].get("result", r[1]) if isinstance(r[1], dict) else r[1]
    return json.loads(r[0].text) if r else None


def raises(name, **args):
    try:
        call(name, **args)
    except Exception as e:  # noqa: BLE001
        return str(e)
    raise AssertionError(f"{name} did not raise")


def test_delete_scan_by_uuid_survives_renumbering():
    reset()
    call("scanner_delete_scan", scan_uuid="s-1")
    call("scanner_delete_scan", scan_uuid="s-2")  # was id 2, now id 1
    assert [s["scanUUID"] for s in STATE["scans"]] == ["s-0", "s-3"]
    assert "no scan" in raises("scanner_delete_scan", scan_uuid="s-1")


def test_launch_validates_locally():
    reset()
    ok = dict(project_name="probe", project_uuid=P_UUID)
    assert "unknown scan_mode" in raises("scanner_launch_scan", scan_mode="BOGUS", **ok)
    assert "no project" in raises("scanner_launch_scan", project_name="other",
                                  project_uuid=P_UUID, scan_mode="HS_L1")
    assert "delta_va" in raises("scanner_launch_area_scan", scan_mode="HS_L1", start_ha=0,
                                delta_ha=0.2, start_va=3.0, delta_va=0.5, **ok)
    assert STATE["tasks"] == []  # nothing was sent


def test_area_scan_echo_ok():
    reset()
    call("scanner_launch_area_scan", project_name="probe", project_uuid=P_UUID,
         scan_mode="HS_L1", start_ha=-0.1, delta_ha=0.2, start_va=1.47, delta_va=0.2)
    assert STATE["cancelled"] == []


def test_fallback_is_cancelled():
    reset()
    STATE["fallback"] = True  # instrument ignores the body and runs its defaults
    err = raises("scanner_launch_area_scan", project_name="probe", project_uuid=P_UUID,
                 scan_mode="HS_L1", start_ha=0, delta_ha=0.2, start_va=1.47, delta_va=0.2)
    assert "did not accept" in err and STATE["cancelled"] == ["7"]


def _fake_scan(name, rows, cols, step, start_va, start_ha):
    meta = 0x100
    data = bytearray(meta + 0x40)
    struct.pack_into("<Q", data, 0x10, meta)
    struct.pack_into("<4d2I", data, meta + 0x10, step, step, start_va, -start_ha, rows, cols)
    with open(os.path.join(DL, name), "wb") as fh:
        fh.write(data)


def _fake_jpeg(name, w, h):
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    with open(os.path.join(DL, name), "wb") as fh:
        fh.write(b"\xff\xd8\xff\xe0\x00\x04\x00\x00" + sof + b"\xff\xd9")


def test_preview_to_angles():
    step = 1.508612e-3
    _fake_scan("dome.tzf", 1631, 4165, step, 0.0, 3.57876)
    _fake_jpeg("dome.jpg", 2082, 815)
    r = call("scanner_preview_to_angles", scan_filename="dome.tzf", preview_filename="dome.jpg",
             x0=590, y0=390, x1=640, y1=440, margin_deg=0)
    sx = 4165 / 2082
    assert math.isclose(r["start_ha"], (3.57876 + 590 * sx * step) % math.tau, abs_tol=1e-5)
    assert math.isclose(r["start_va"], 390 * (1631 / 815) * step, abs_tol=1e-5)
    assert math.isclose(r["delta_ha"], 50 * sx * step, abs_tol=1e-5)
    wrap = call("scanner_preview_to_angles", scan_filename="dome.tzf", preview_filename="dome.jpg",
                x0=2070, y0=0, x1=10, y1=10, margin_deg=0)
    assert math.isclose(wrap["delta_ha"], 22 * sx * step, abs_tol=1e-5)
    assert "not found" in raises("scanner_preview_to_angles", scan_filename="nope.tzf",
                                 preview_filename="dome.jpg", x0=0, y0=0, x1=1, y1=1)
