"""Every question the bridge-mode server asks the person (MCP elicitation)
must be a form chat apps accept. The MCP spec allows only a flat object of
primitive fields (string, number, integer, boolean, or an array of string
choices), and a default only as a real value of the field's type; the Claude
app rejects the whole question otherwise ("Invalid elicitation request").
Run: pytest -q"""
import asyncio
import importlib
from types import SimpleNamespace

import pytest


def bridge_module():
    # Imported late: test_offline sets the environment scanner_mcp reads at import.
    return importlib.import_module("scanner_bridge")

PRIMITIVES = {"string": str, "number": (int, float), "integer": int, "boolean": bool}
FIELD_KEYS = {"type", "title", "description", "default", "enum", "enumNames", "oneOf", "anyOf",
              "minimum", "maximum", "minLength", "maxLength", "format", "items", "minItems", "maxItems"}


def form_problems(schema: dict) -> list:
    """What makes `schema` (a requestedSchema) break the MCP form rules."""
    bad = []
    if schema.get("type") != "object":
        bad.append("the form is not an object")
    props = schema.get("properties") or {}
    if not props:
        bad.append("the form has no fields")
    for k in schema.get("required", []):
        if k not in props:
            bad.append(f"required field {k!r} is not in properties")
    for name, p in props.items():
        where = f"properties.{name}"
        t = p.get("type")
        if "$ref" in p or "allOf" in p or "anyOf" in p:
            bad.append(f"{where}: not a plain type ({sorted(k for k in p if k in ('$ref', 'allOf', 'anyOf'))})")
        if extra := set(p) - FIELD_KEYS:
            bad.append(f"{where}: unexpected keys {sorted(extra)}")
        if t == "array":
            items = p.get("items") or {}
            if items.get("type") != "string" or not (items.get("enum") or items.get("anyOf")):
                bad.append(f"{where}: an array field must be a list of string choices")
        elif t not in PRIMITIVES:
            bad.append(f"{where}: type {t!r} is not one of {sorted(PRIMITIVES)} or array")
        if "default" in p:
            d = p["default"]
            if d is None:
                bad.append(f"{where}: default is null")
            elif t in PRIMITIVES and (not isinstance(d, PRIMITIVES[t]) or (t != "boolean" and isinstance(d, bool))):
                bad.append(f"{where}: default {d!r} is not a {t}")
            elif t == "array" and not (isinstance(d, list) and all(isinstance(x, str) for x in d)):
                bad.append(f"{where}: default {d!r} is not a list of strings")
    return bad


def test_form_rules_catch_the_0_7_0_sign_off_form():
    """The checker itself: 0.7.0's nullable correction fields fail it."""
    from pydantic import Field, create_model
    old = create_model("SignOff", confirm=(bool, Field(True)),
                       opening_height_mm=(float | None, Field(None, description="Correct opening height mm")))
    problems = form_problems(old.model_json_schema())
    assert any("default is null" in p for p in problems)
    assert any("not a plain type" in p for p in problems)


PROPOSAL = {"opening_width_mm": 1180, "opening_height_mm": 1145, "uncertainty_mm": 9,
            "box": {"x0": 1, "y0": 2, "x1": 3, "y1": 4}}


class Chat:
    """A chat that can ask: records each form and answers it like a person
    who presses Accept, after changing the fields in `edits`."""

    def __init__(self, action="accept", **edits):
        self.forms, self.action, self.edits = [], action, edits
        self.session = SimpleNamespace(check_client_capability=lambda caps: True)

    async def elicit(self, message, schema):
        self.forms.append((message, schema.model_json_schema()))
        data = schema(**{k: f.default for k, f in schema.model_fields.items()} | self.edits)
        return SimpleNamespace(action=self.action, data=data if self.action == "accept" else None)


@pytest.fixture
def bridge(monkeypatch):
    b = bridge_module()
    calls = []

    async def call(method, path, body=None):
        calls.append(("agent", method, path, body))
        if path == "/grants":
            return []
        if path == "/projects":
            return [{"project_id": "p1", "name": "Front window"}]
        if path == "/runs" and method == "POST":
            return {"run_id": "run_1", "state": "running"}
        if path == "/runs/run_1":
            return {"run_id": "run_1", "recipe_id": "window", "waiting_for": "input", "current_step_id": "sign",
                    "steps": [{"step_id": "sign", "proposal": {"input": dict(PROPOSAL), "note": "clear frame"}}]}
        if path == "/recipes/window":
            return {"steps": [{"step_id": "sign", "title": "Sign off the measurement",
                               "needs": ["opening_width_mm", "opening_height_mm", "uncertainty_mm"]}]}
        raise AssertionError(f"unexpected call {method} {path}")

    async def call_as_person(method, path, body=None):
        calls.append(("person", method, path, body))
        return {"ok": True}

    monkeypatch.setattr(b, "_call", call)
    monkeypatch.setattr(b, "_call_as_person", call_as_person)
    monkeypatch.setattr(b, "PERSON_TOKEN", "operator")
    return calls


def run(name, **kw):
    fn = getattr(bridge_module(), name)
    return asyncio.run(getattr(fn, "fn", fn)(**kw))


def test_start_run_permission_question_is_a_valid_form(bridge):
    chat = Chat()
    r = run("scanner_start_run", recipe_id="window", project_id="p1", ctx=chat)
    assert r["permission"] == "given in the chat"
    assert len(chat.forms) == 1
    assert form_problems(chat.forms[0][1]) == []


def test_sign_off_question_is_a_valid_form(bridge):
    chat = Chat()
    r = run("scanner_sign_off", run_id="run_1", summary="Checked the box.", ctx=chat)
    assert len(chat.forms) == 1
    message, form = chat.forms[0]
    assert form_problems(form) == []
    props = form["properties"]
    assert set(props) == {"confirm", "opening_width_mm", "opening_height_mm", "uncertainty_mm"}
    # Each number opens prefilled with the proposal.
    assert props["opening_width_mm"]["default"] == 1180 and props["opening_height_mm"]["default"] == 1145
    assert "1180" in message and "1145" in message
    # Accepting it unchanged signs off the proposal with nothing corrected.
    assert r["signed_off"] and r["corrected"] == {}
    who, method, path, body = bridge[-1]
    assert (who, method, path) == ("person", "POST", "/runs/run_1/advance")
    assert body["input"]["opening_height_mm"] == 1145 and body["input"]["confirmed_via"] == "asked in the chat"


def test_sign_off_with_a_corrected_number(bridge):
    chat = Chat(opening_height_mm=1150)
    r = run("scanner_sign_off", run_id="run_1", ctx=chat)
    assert r["signed_off"] and r["corrected"] == {"opening_height_mm": 1150}
    assert isinstance(r["corrected"]["opening_height_mm"], int)
    assert bridge[-1][3]["input"]["opening_height_mm"] == 1150
    assert bridge[-1][3]["input"]["opening_width_mm"] == 1180


def test_sign_off_declined_or_unconfirmed_signs_nothing(bridge):
    r = run("scanner_sign_off", run_id="run_1", ctx=Chat(action="decline"))
    assert not r["signed_off"] and r["action"] == "decline"
    r = run("scanner_sign_off", run_id="run_1", ctx=Chat(confirm=False))
    assert not r["signed_off"]
    assert not any(c[0] == "person" for c in bridge)
