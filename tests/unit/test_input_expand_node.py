"""AgentCore Platform v1.0 — Inner ⓪ input_expand tests (docs/02_design.md §5-2/§5-3/§8)"""

import json

import pytest
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.security.credential_detector import detect_credentials
from shared.security.pii_detector import detect_pii

from src.nodes import input_expand_node as mod
from src.nodes.input_expand_node import (
    ERROR_INVALID_INNER_INPUT,
    ERROR_MASKED_CRITICAL_FIELD,
    ERROR_MISSING_INNER_INPUT,
    INNER_INPUT_FIELDS,
    INTAKE_SENTINEL,
    InputExpandNode,
    build_intake_payload,
    pack_intake,
    unpack_intake,
)
from src.schemas.state import State

# A realistic request: Title Case region and segment names are exactly what the
# S-2 `name` pattern destroys, which is the whole reason for packing (§5-3).
ANALYST_REQUEST = "Osaka Prefecture の Foreign Visitors 数を 2025 年度の月次で"
APPROVED_SCHEMA = json.dumps({"dimensions": ["period", "region", "segment"], "metrics": ["visitors"]})
RAW_REQUEST = json.dumps({"analyst_request": ANALYST_REQUEST, "approved_schema": APPROVED_SCHEMA})

DUMMY_OPENAI_KEY = "sk-abcdefghijklmnopqrstuvwx"


def packed(analyst_request=ANALYST_REQUEST, approved_schema=APPROVED_SCHEMA):
    return pack_intake({"analyst_request": analyst_request, "approved_schema": approved_schema})


def run(user_input):
    """Call execute() directly — __call__() would add the S-1 gate and history."""
    return InputExpandNode().execute({"user_input": user_input})


# ---------------------------------------------------------------- intake contract


def test_pack_unpack_round_trip_is_symmetric():
    """DB-6: asymmetry here fails every request, so it is asserted on real text."""
    assert unpack_intake(packed()) == {
        "analyst_request": ANALYST_REQUEST,
        "approved_schema": APPROVED_SCHEMA,
    }


def test_plain_json_really_is_destroyed_by_the_pii_detector():
    """The premise of §5-3. Without this, the packing tests prove nothing.

    If the `name` pattern ever stops matching Title Case runs, packing becomes
    unnecessary complexity and this test is the signal to revisit §5-3.
    """
    findings = detect_pii(RAW_REQUEST)
    assert [f["type"] for f in findings].count("name") >= 2, (
        f"plain JSON no longer trips `name`: {findings}"
    )


def test_packed_payload_is_invisible_to_both_detectors():
    """0 PII hits (so S-2 does not mask it) and 0 credential hits (so S-3 does not block it)."""
    assert detect_pii(packed()) == []
    assert detect_credentials(packed()) == []


def test_packed_body_is_hex_so_it_cannot_look_like_a_jwt():
    """base64 is prohibited: JSON's leading `{"` becomes `eyJ`, which the jwt
    pattern `eyJ[a-zA-Z0-9._\\-]{10,}` always matches."""
    body = packed()[len(INTAKE_SENTINEL):]
    assert body and all(c in "0123456789abcdef" for c in body)
    assert "eyJ" not in packed()


def test_sentinel_obeys_its_normative_constraints():
    """§5-2 requires structural immunity, not a measured 0 today.

    Lowercase alphanumerics with a trailing letter cannot match name/email/
    phone/ssn at all, so the sentinel survives new detector patterns.
    """
    assert INTAKE_SENTINEL.isalnum(), "must be [A-Za-z0-9] only — no whitespace, no `_`"
    assert INTAKE_SENTINEL == INTAKE_SENTINEL.lower(), "uppercase can start a Title Case run"
    assert INTAKE_SENTINEL[-1].isalpha(), "a trailing digit would join the hex body's \\b"
    assert detect_pii(INTAKE_SENTINEL) == []


def test_inner_input_fields_matches_the_state_channels():
    """Both intake keys must exist as State channels or the delta is dropped
    silently — LangGraph discards undeclared keys without raising (§3-1)."""
    assert INNER_INPUT_FIELDS == ("analyst_request", "approved_schema")
    for field in INNER_INPUT_FIELDS:
        assert field in State.__annotations__


# ---------------------------------------------------------------- build_intake_payload


def test_build_extracts_exactly_the_intake_fields():
    payload = build_intake_payload(json.dumps({
        "analyst_request": ANALYST_REQUEST,
        "approved_schema": APPROVED_SCHEMA,
        "unrelated": "dropped",
    }))
    assert payload == {"analyst_request": ANALYST_REQUEST, "approved_schema": APPROVED_SCHEMA}


@pytest.mark.parametrize("raw,expected", [
    (None, ERROR_MISSING_INNER_INPUT),
    ("", ERROR_MISSING_INNER_INPUT),
    ("   ", ERROR_MISSING_INNER_INPUT),
    (123, ERROR_MISSING_INNER_INPUT),
    ("not json at all", ERROR_INVALID_INNER_INPUT),
    ('["a", "list"]', ERROR_INVALID_INNER_INPUT),
    ('"a bare string"', ERROR_INVALID_INNER_INPUT),
    ('{"analyst_request": "x"}', ERROR_MISSING_INNER_INPUT),
    ('{"approved_schema": "{}"}', ERROR_MISSING_INNER_INPUT),
    ('{"analyst_request": "x", "approved_schema": ""}', ERROR_MISSING_INNER_INPUT),
    ('{"analyst_request": "x", "approved_schema": 42}', ERROR_MISSING_INNER_INPUT),
    ('{"analyst_request": "[MASKED] 数", "approved_schema": "{}"}', ERROR_MASKED_CRITICAL_FIELD),
])
def test_build_reports_failures_as_values_never_exceptions(raw, expected):
    assert build_intake_payload(raw) == {"_intake_error": expected}


def test_build_reports_masking_before_blaming_the_json():
    """A request the S-2 gate damaged must not be reported as malformed JSON —
    the analyst would go debugging a request that was well-formed on send."""
    assert build_intake_payload('{"analyst_request": "[MASKED]", broken') == {
        "_intake_error": ERROR_MASKED_CRITICAL_FIELD
    }


# ---------------------------------------------------------------- unpack_intake


@pytest.mark.parametrize("packed_value", [
    None,
    "",
    123,
    "no sentinel here",
    json.dumps({"analyst_request": "x"}),          # plain JSON, sentinel missing
    INTAKE_SENTINEL,                                # sentinel, empty body
    INTAKE_SENTINEL + "zzzz",                       # not hex
    INTAKE_SENTINEL + "abc",                        # odd-length hex
    INTAKE_SENTINEL + b'["a"]'.hex(),               # decodes to JSON, but not an object
    INTAKE_SENTINEL + b"\xff\xfe".hex(),            # not valid utf-8
])
def test_unpack_collapses_every_structural_failure_to_invalid(packed_value):
    assert unpack_intake(packed_value) == {"_intake_error": ERROR_INVALID_INNER_INPUT}


def test_a_valid_hex_body_without_the_sentinel_is_still_rejected():
    """The sentinel is a requirement, not decoration.

    A decoy the same length as the sentinel would otherwise be sliced off and the
    remaining hex would decode to a perfectly good payload — meaning anything a
    caller passed could be mistaken for packed intake.
    """
    decoy = "x" * len(INTAKE_SENTINEL) + json.dumps(
        {"analyst_request": ANALYST_REQUEST, "approved_schema": APPROVED_SCHEMA}
    ).encode("utf-8").hex()
    assert unpack_intake(decoy) == {"_intake_error": ERROR_INVALID_INNER_INPUT}


def test_pack_survives_an_unserializable_payload():
    """pack_intake must not raise: extract_input() has no error channel of its own."""
    result = pack_intake({"analyst_request": {1, 2, 3}})
    assert result.startswith(INTAKE_SENTINEL)
    assert unpack_intake(result) == {"_intake_error": ERROR_INVALID_INNER_INPUT}


def test_non_ascii_request_survives_the_hex_round_trip():
    assert unpack_intake(packed(analyst_request="大阪府の外国人客数"))["analyst_request"] == (
        "大阪府の外国人客数"
    )


# ---------------------------------------------------------------- node ⓪


def test_declares_the_trust_level_in_the_class_body():
    """gate-trust-level-check reads the class body, not an instance attribute."""
    assert InputExpandNode.__dict__["required_trust_level"] is TrustLevel.VERIFIED_EXTERNAL


def test_happy_path_restores_both_fields_with_no_error():
    delta = run(packed())
    assert delta["analyst_request"] == ANALYST_REQUEST
    assert delta["approved_schema"] == APPROVED_SCHEMA
    assert delta["error_code"] is None
    assert "exception_report" not in delta


def test_status_is_success_even_when_the_intake_failed():
    """§7-6: a domain exception must not set ERROR. GraphNode.execute() skips
    merge_output() on Inner ERROR, and the exception report is lost."""
    delta = run("garbage")
    assert delta["status"] == AgentStatus.SUCCESS.value
    assert delta["error_code"] == ERROR_INVALID_INNER_INPUT
    assert delta["exception_report"]


@pytest.mark.parametrize("user_input,expected", [
    ("garbage", ERROR_INVALID_INNER_INPUT),
    (None, ERROR_INVALID_INNER_INPUT),
    (pack_intake({"_intake_error": ERROR_MISSING_INNER_INPUT}), ERROR_MISSING_INNER_INPUT),
    (pack_intake({"_intake_error": ERROR_MASKED_CRITICAL_FIELD}), ERROR_MASKED_CRITICAL_FIELD),
    (packed(analyst_request="[MASKED] 数"), ERROR_MASKED_CRITICAL_FIELD),
    (packed(approved_schema="not json"), ERROR_INVALID_INNER_INPUT),
    (pack_intake({"analyst_request": "x"}), ERROR_MISSING_INNER_INPUT),
])
def test_error_codes_follow_the_fixed_decision_order(user_input, expected):
    assert run(user_input)["error_code"] == expected


def test_a_build_stage_error_is_surfaced_verbatim_not_relabelled():
    """The caller sees one taxonomy whichever side detected the problem, so
    Outer's MISSING_INNER_INPUT must not arrive as ⓪'s INVALID_INNER_INPUT."""
    outer = build_intake_payload("")
    assert run(pack_intake(outer))["error_code"] == ERROR_MISSING_INNER_INPUT


def test_masking_outranks_unparseable_schema():
    """Both faults present: the mask is the cause, the bad JSON is its symptom."""
    delta = run(packed(analyst_request="[MASKED] 数", approved_schema="not json"))
    assert delta["error_code"] == ERROR_MASKED_CRITICAL_FIELD


def test_unparseable_schema_outranks_a_missing_field():
    """§8 puts `json.loads()` *before* the required-field check, and this is the
    only input shape that tells the two apart. Without it, dropping ⓪'s schema
    validation entirely would still look correct: execute()'s post-removal
    re-validation reports the same code for a well-formed-but-unparseable schema,
    so only the ordering distinguishes the two checks."""
    delta = run(packed(analyst_request="", approved_schema="{not json"))
    assert delta["error_code"] == ERROR_INVALID_INNER_INPUT


def test_fields_are_cleared_when_the_intake_failed():
    """Fail-closed. Non-reducer channels are last-write-wins, so omitting the
    keys would leave a previous checkpoint's values for ① to normalize."""
    delta = run("garbage")
    assert delta["analyst_request"] is None
    assert delta["approved_schema"] is None


# ---------------------------------------------------------------- S-3 / S-4 / delta shape


def test_credentials_in_the_request_are_removed_before_the_delta():
    delta = run(packed(analyst_request=f"visitors for {DUMMY_OPENAI_KEY}"))
    assert DUMMY_OPENAI_KEY not in delta["analyst_request"]
    assert "[REDACTED:" in delta["analyst_request"]


def test_the_real_final_s3_gate_accepts_the_delta():
    """End-to-end proof rather than a restatement of the redaction unit test:
    the @final gate raises RuntimeError on any credential left in the delta,
    which BaseNode turns into status=error and the report is lost."""
    node = InputExpandNode()
    for hostile in (
        f"q {DUMMY_OPENAI_KEY}",
        # A key inside a connection string: two overlapping findings, one of which
        # the earlier implementation dropped, leaving text the gate rejected.
        f"postgresql://u:{DUMMY_OPENAI_KEY}@db.example.com:5432/visitors",
        f"Authorization: Bearer {DUMMY_OPENAI_KEY}",
    ):
        delta = node.execute({"user_input": packed(analyst_request=hostile)})
        # Would raise RuntimeError if any credential survived into the delta.
        assert node._security_gate_output(delta) is not None
        assert detect_credentials(delta["analyst_request"]) == []


def test_a_credential_in_the_schema_leaves_it_parseable_for_the_next_node():
    """§7-3 leaf-wise removal. A flat replacement swallows the closing quote and
    the following key, and ② would receive a broken schema with error_code None —
    the fail-open §8 assigns INVALID_INNER_INPUT to prevent."""
    node = InputExpandNode()
    schema = json.dumps({
        "source": f"postgresql://u:{DUMMY_OPENAI_KEY}@host/visitors",
        "dimensions": ["region"],
        "metrics": ["visitors"],
    })
    delta = node.execute({"user_input": packed(approved_schema=schema)})

    assert delta["error_code"] is None, "a redactable schema is not a bad request"
    parsed = json.loads(delta["approved_schema"])  # raises if removal broke it
    assert parsed["dimensions"] == ["region"]
    assert parsed["source"].startswith("[REDACTED:")
    assert node._security_gate_output(delta) is not None


@pytest.mark.parametrize("source", ["mysql://dw_prod", "mysql://dw_pro", "redis://cache1"])
def test_a_benign_data_source_does_not_trip_the_gate(source):
    """No credential anywhere in this request — but `mysql://` plus 6-7 characters
    matches conn_string once serialization appends `",`, and it is the serialized
    string the @final gate scans. Judging leaves in isolation cost the exception
    report on an entirely ordinary schema."""
    node = InputExpandNode()
    schema = json.dumps({"source": source, "dimensions": ["region"], "metrics": ["visitors"]})
    delta = node.execute({"user_input": packed(approved_schema=schema)})

    assert delta["error_code"] is None, "a redactable identifier is not a bad request"
    assert node._security_gate_output(delta) is not None  # raises if not clean
    assert json.loads(delta["approved_schema"])["dimensions"] == ["region"]


def test_a_field_the_redactor_could_not_clean_fails_closed(monkeypatch):
    """The mirror of the parseability check, on the property the gate tests.

    Parseable-but-dirty is the failure mode a leaf-wise redactor produces, so the
    boundary asserts cleanliness too. Reporting INVALID_INNER_INPUT keeps §7-6's
    report; letting the @final gate raise turns it into status="error" and
    GraphNode then skips merge_output(), discarding the report entirely.
    """
    monkeypatch.setattr(mod, "redact_credentials", lambda text: (text, 0))
    node = InputExpandNode()
    delta = node.execute({"user_input": packed(analyst_request=f"q {DUMMY_OPENAI_KEY}")})

    assert delta["error_code"] == ERROR_INVALID_INNER_INPUT
    assert delta["analyst_request"] is None
    assert delta["approved_schema"] is None
    assert delta["exception_report"]
    assert node._security_gate_output(delta) is not None


def test_a_schema_the_redactor_could_not_clean_fails_closed(monkeypatch):
    """Parseable but still dirty — the schema half of the cleanliness mirror.

    Distinct from the request half: the schema goes through redact_json_document,
    so a leaf judged out of context leaves the document perfectly valid JSON and
    only the gate notices. Checking one field and not the other passes every
    parseability test while still losing the report.
    """
    dirty = json.dumps({"source": f"mysql://{DUMMY_OPENAI_KEY}"})
    monkeypatch.setattr(mod, "redact_json_document", lambda raw: (dirty, 0))
    node = InputExpandNode()
    delta = node.execute({"user_input": packed()})

    assert json.loads(dirty), "precondition: the mocked schema is valid JSON"
    assert delta["error_code"] == ERROR_INVALID_INNER_INPUT
    assert delta["approved_schema"] is None
    assert delta["exception_report"]
    assert node._security_gate_output(delta) is not None


def test_a_schema_broken_by_removal_fails_closed(monkeypatch):
    """The post-removal re-validation is a boundary assertion, not decoration.

    Leaf-wise removal preserves the document today. If it ever stops doing so,
    ⓪ must report INVALID_INNER_INPUT and clear the fields rather than hand ②
    an unparseable schema — so the guard is tested against a broken redactor.
    """
    monkeypatch.setattr(mod, "redact_json_document", lambda raw: ('{"broken": ', 1))
    delta = InputExpandNode().execute({"user_input": packed()})

    assert delta["error_code"] == ERROR_INVALID_INNER_INPUT
    assert delta["approved_schema"] is None
    assert delta["analyst_request"] is None
    assert delta["exception_report"]


def test_the_exception_report_quotes_neither_request_text_nor_credential():
    """§7-3: a quoted credential makes the report itself trip the S-3 gate."""
    delta = run(packed(analyst_request=f"[MASKED] {DUMMY_OPENAI_KEY}"))
    report = delta["exception_report"]

    assert delta["error_code"] == ERROR_MASKED_CRITICAL_FIELD
    assert DUMMY_OPENAI_KEY not in report
    assert ANALYST_REQUEST not in report
    assert detect_credentials(report) == []
    assert ERROR_MASKED_CRITICAL_FIELD in report


def test_the_audit_event_is_domain_specific_and_emitted_once(monkeypatch):
    """TC-05 wants one domain event per execute(); node_start / node_complete are
    BaseNode's and re-emitting them would corrupt the audit trail (§7-4)."""
    seen = []
    monkeypatch.setattr(mod, "emit_trace_event", lambda t, p, s: seen.append((t, p)))
    run(packed())

    assert len(seen) == 1
    event_type, payload = seen[0]
    assert event_type not in ("node_start", "node_complete", "node_error")
    assert payload["fields_restored"] == list(INNER_INPUT_FIELDS)


def test_the_audit_payload_carries_counts_not_content(monkeypatch):
    """An audit log is not a secret store: names and counts only."""
    seen = []
    monkeypatch.setattr(mod, "emit_trace_event", lambda t, p, s: seen.append((t, p)))
    run(packed(analyst_request=f"visitors for {DUMMY_OPENAI_KEY}"))

    payload = seen[0][1]
    assert payload["credentials_redacted"] == 1
    assert DUMMY_OPENAI_KEY not in json.dumps(payload)
    assert ANALYST_REQUEST not in json.dumps(payload)


def test_the_delta_writes_only_declared_channels():
    """LangGraph drops undeclared keys silently, so a typo shows up as a missing
    field far downstream rather than an error here (§3-1)."""
    for delta in (run(packed()), run("garbage")):
        assert set(delta) <= set(State.__annotations__), (
            f"undeclared channels: {set(delta) - set(State.__annotations__)}"
        )


def test_the_delta_never_spreads_the_reducer_channels():
    """`{**state, ...}` would re-concatenate node_history and error_log on every
    node, because both are Annotated[list[str], operator.add] (§6)."""
    delta = InputExpandNode().execute({
        "user_input": packed(),
        "node_history": ["Upstream"],
        "error_log": ["previous"],
        "query_result": "stale",
    })
    assert "node_history" not in delta
    assert "error_log" not in delta
    assert "query_result" not in delta


@pytest.mark.parametrize("hostile", [
    None, "", 0, [], {}, INTAKE_SENTINEL, INTAKE_SENTINEL + "ff",
    "\x00\x01", "𝕊𝕥𝕣𝕒𝕟𝕘𝕖", INTAKE_SENTINEL + b'{"analyst_request": null}'.hex(),
    # Credentials in both channels, and non-str leaves that must not reach an
    # Optional[str] channel (§6 holds list/dict as JSON strings).
    packed(analyst_request=f"postgresql://u:{DUMMY_OPENAI_KEY}@h/db"),
    pack_intake({"analyst_request": 42, "approved_schema": {"a": 1}}),
    pack_intake({"analyst_request": ANALYST_REQUEST,
                 "approved_schema": f'{{"source": "redis://u:{DUMMY_OPENAI_KEY}@h/0"}}'}),
])
def test_execute_never_raises(hostile):
    """A raise here is caught by BaseNode as status=error and the exception
    report — the artifact this template exists to produce — is discarded (§7-6)."""
    delta = run(hostile)
    assert delta["status"] == AgentStatus.SUCCESS.value

    # Both channels are Optional[str]. The payload is caller-controlled, so a
    # non-str leaf must never be written through: §6 holds list/dict as JSON
    # strings and §11 TC-01 asserts every post-invoke State value is primitive.
    for field in INNER_INPUT_FIELDS:
        assert delta[field] is None or isinstance(delta[field], str), (
            f"{field} carried a {type(delta[field]).__name__} into an Optional[str] channel"
        )
    if delta["error_code"]:
        # Fail-closed: a partially-restored request must not reach ①.
        assert delta["analyst_request"] is None
        assert delta["approved_schema"] is None
        assert delta["exception_report"]
