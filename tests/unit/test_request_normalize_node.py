"""AgentCore Platform v1.0 — Inner ① request_normalize tests (docs/02_design.md §7-2/§7-6/§8)"""

import json

import pytest
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.security.credential_detector import detect_credentials

from src.nodes import request_normalize_node as mod
from src.nodes.request_normalize_node import (
    ERROR_CREDENTIAL_IN_REQUEST,
    ERROR_NORMALIZE,
    NORMALIZED_QUERY_KEYS,
    QUERY_DIMENSIONS,
    RequestNormalizeNode,
)
from src.security.credential_redaction import REDACTION_MARKER_PREFIX

ANALYST_REQUEST = "Osaka Prefecture の Foreign Visitors 数を 2025 年度の月次で"
APPROVED_SCHEMA = json.dumps(
    {"dimensions": ["period", "region", "segment"], "metrics": ["visitors"]},
    ensure_ascii=False,
)

# Never a real credential (§7-5 / project rule): these are shaped to match
# credential_detector patterns and nothing else.
DUMMY_OPENAI_KEY = "sk-abcdefghijklmnopqrstuvwx"
FAKE_JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6"

NORMALIZED = {
    "period": "2025年度 月次",
    "region": "Osaka Prefecture",
    "segment": "Foreign Visitors",
    "metric": "visitors",
    "filters": {},
}


_DEFAULT_RESPONSE = object()


class _MockClient:
    """Records what it was asked, answers with whatever the test supplies.

    `response` needs a sentinel default rather than None, because "complete()
    returned None" is itself one of the cases under test.
    """

    def __init__(self, response=_DEFAULT_RESPONSE, raises=None):
        self._response = (
            {"content": json.dumps(NORMALIZED, ensure_ascii=False)}
            if response is _DEFAULT_RESPONSE else response
        )
        self._raises = raises
        self.calls: list[list] = []

    def complete(self, messages):
        self.calls.append(messages)
        if self._raises is not None:
            raise self._raises
        return self._response


def _state(request=ANALYST_REQUEST, schema=APPROVED_SCHEMA, **extra):
    """A state InvocationContext.from_state() can actually read.

    All four correlation fields are required: from_state() indexes them directly
    (invocation_context.py:33-37), so omitting one turns every secret-provider
    assertion below into an assertion about a KeyError instead.
    """
    state = {
        "analyst_request": request,
        "approved_schema": schema,
        "session_id": "s",
        "correlation_id": "c",
        "thread_id": "t",
        "trace_id": "tr",
    }
    state.update(extra)
    return state


def run(node=None, **kwargs):
    """execute() directly — __call__() would add the S-1 gate and node_history."""
    return (node or RequestNormalizeNode(llm=_MockClient())).execute(_state(**kwargs))


def gated(node, state):
    """The production order: S-2 hook, then execute() on what the hook returned."""
    return node.execute(node._extra_security_gate_input(state))


# ---------------------------------------------------------------- happy path


def test_normalizes_to_four_dimensions_and_filters():
    result = run()
    query = json.loads(result["normalized_query"])
    assert query["period"] == "2025年度 月次"
    assert query["region"] == "Osaka Prefecture"
    assert query["segment"] == "Foreign Visitors"
    assert query["metric"] == "visitors"
    assert query["filters"] == {}
    assert result["error_code"] is None
    assert result["status"] == AgentStatus.SUCCESS.value
    assert "exception_report" not in result


def test_the_output_carries_exactly_the_five_contract_keys():
    """② is written against this key set; an extra key would be silently ignored
    there and a missing one would read as "undetermined" (§6)."""
    query = json.loads(run()["normalized_query"])
    assert sorted(query) == sorted(NORMALIZED_QUERY_KEYS)


def test_the_prompt_carries_both_the_request_and_the_approved_schema():
    """Without the schema in the prompt the LLM cannot avoid inventing
    dimensions, which is what §4-2 makes ② responsible for catching."""
    llm = _MockClient()
    run(RequestNormalizeNode(llm=llm))
    sent = json.dumps(llm.calls[0], ensure_ascii=False)
    assert ANALYST_REQUEST in sent
    assert '\\"dimensions\\"' in sent  # the schema, quoted as JSON inside the prompt
    assert "visitors" in sent


def test_the_prompt_states_the_five_key_contract_and_the_null_rule():
    """The output contract is enforced downstream by ②, but asking for it is
    what makes the happy path the common case rather than the exception. The
    null rule matters just as much: a model that lists two candidate regions
    instead of answering null has widened the query (§8 AMBIGUOUS_QUERY)."""
    llm = _MockClient()
    run(RequestNormalizeNode(llm=llm))
    system = [m for m in llm.calls[0] if m["role"] == "system"]
    assert len(system) == 1
    assert all(key in system[0]["content"] for key in NORMALIZED_QUERY_KEYS)
    assert "null" in system[0]["content"]

    user = [m for m in llm.calls[0] if m["role"] == "user"]
    assert all(key in user[0]["content"] for key in NORMALIZED_QUERY_KEYS)


def test_the_raw_llm_response_never_reaches_a_state_channel():
    """§6: free text with Title Case names would be [MASKED] by a later node's
    @final S-2 scan, and nobody re-reads the pre-parse response anyway."""
    prose = "Here is the JSON you asked for:\n" + json.dumps(NORMALIZED, ensure_ascii=False)
    result = run(RequestNormalizeNode(llm=_MockClient(response={"content": prose})))
    assert all(
        not (isinstance(value, str) and "Here is the JSON" in value)
        for value in result.values()
    )
    assert json.loads(result["normalized_query"])["metric"] == "visitors"


@pytest.mark.parametrize("response", [
    {"content": "```json\n" + json.dumps(NORMALIZED, ensure_ascii=False) + "\n```"},
    {"content": "```\n" + json.dumps(NORMALIZED, ensure_ascii=False) + "\n```"},
    json.dumps(NORMALIZED, ensure_ascii=False),  # bare str instead of the canonical dict
])
def test_common_response_wrappers_are_unwrapped(response):
    """A fenced or bare-string response is a well-formed answer wearing a
    wrapper; reporting NORMALIZE_ERROR for it would fail requests the model
    answered correctly."""
    result = run(RequestNormalizeNode(llm=_MockClient(response=response)))
    assert result["error_code"] is None
    assert json.loads(result["normalized_query"])["region"] == "Osaka Prefecture"


# ---------------------------------------------------------------- dimension coercion


@pytest.mark.parametrize("raw,expected", [
    ("2025", "2025"),
    ("  2025  ", "2025"),
    (2025, "2025"),
    (2025.0, "2025.0"),
    ("", None),
    ("   ", None),
    (None, None),
    (True, None),          # a boolean dimension is meaningless, not "yes"
    (["Osaka", "Kyoto"], None),   # two regions = not uniquely determined
    ({"from": "2025"}, None),
])
def test_a_dimension_value_is_coerced_to_one_string_or_left_undetermined(raw, expected):
    """② reports AMBIGUOUS_QUERY for an undetermined dimension (§8). Collapsing
    a multi-value dimension to None routes it there instead of letting ② query
    on a list it has no contract for."""
    payload = dict(NORMALIZED, period=raw)
    result = run(RequestNormalizeNode(llm=_MockClient(response={"content": json.dumps(payload, ensure_ascii=False)})))
    assert json.loads(result["normalized_query"])["period"] == expected
    assert result["error_code"] is None


def test_a_missing_dimension_is_undetermined_rather_than_absent():
    payload = {key: value for key, value in NORMALIZED.items() if key != "segment"}
    result = run(RequestNormalizeNode(llm=_MockClient(response={"content": json.dumps(payload, ensure_ascii=False)})))
    query = json.loads(result["normalized_query"])
    assert query["segment"] is None
    assert sorted(query) == sorted(NORMALIZED_QUERY_KEYS)


def test_undetermined_dimensions_are_named_in_the_audit_metadata():
    payload = dict(NORMALIZED, segment=None, metric="")
    result = run(RequestNormalizeNode(llm=_MockClient(response={"content": json.dumps(payload, ensure_ascii=False)})))
    audit = json.loads(result["audit_log"])
    assert audit["undetermined_dimensions"] == ["segment", "metric"]


@pytest.mark.parametrize("filters,expected", [
    ({"purpose": "leisure"}, {"purpose": "leisure"}),
    (None, {}),
])
def test_filters_default_to_empty_and_pass_through_otherwise(filters, expected):
    payload = dict(NORMALIZED, filters=filters)
    result = run(RequestNormalizeNode(llm=_MockClient(response={"content": json.dumps(payload, ensure_ascii=False)})))
    assert json.loads(result["normalized_query"])["filters"] == expected


def test_a_missing_filters_key_defaults_to_empty():
    payload = {key: value for key, value in NORMALIZED.items() if key != "filters"}
    result = run(RequestNormalizeNode(llm=_MockClient(response={"content": json.dumps(payload, ensure_ascii=False)})))
    assert json.loads(result["normalized_query"])["filters"] == {}


@pytest.mark.parametrize("filters", ["leisure", ["leisure"], 3])
def test_a_non_object_filters_fails_closed(filters):
    """Unlike a dimension, `filters` has no downstream "undetermined" code —
    dropping a malformed restriction would widen the query silently, so ① fails
    closed instead."""
    payload = dict(NORMALIZED, filters=filters)
    result = run(RequestNormalizeNode(llm=_MockClient(response={"content": json.dumps(payload, ensure_ascii=False)})))
    assert result["error_code"] == ERROR_NORMALIZE
    assert result["normalized_query"] is None
    assert result["exception_report"]


def test_keys_outside_the_contract_are_dropped_and_counted():
    payload = dict(NORMALIZED, nationality="Taiwan", notes="reasoning")
    result = run(RequestNormalizeNode(llm=_MockClient(response={"content": json.dumps(payload, ensure_ascii=False)})))
    query = json.loads(result["normalized_query"])
    assert sorted(query) == sorted(NORMALIZED_QUERY_KEYS)
    assert json.loads(result["audit_log"])["unexpected_keys"] == 2


def test_the_audit_metadata_never_quotes_model_produced_text():
    """audit_log is a str in the delta, so the @final S-3 gate scans it. A
    dropped key name is LLM-controlled: quoting it would let a credential in a
    key trip the gate and destroy the exception report (§7-3)."""
    payload = dict(NORMALIZED, **{f"leak_{DUMMY_OPENAI_KEY}": "x"})
    node = RequestNormalizeNode(llm=_MockClient(response={"content": json.dumps(payload, ensure_ascii=False)}))
    result = node.execute(_state())
    assert DUMMY_OPENAI_KEY not in result["audit_log"]
    assert node._security_gate_output(result) is not None


# ---------------------------------------------------------------- LLM failure


def test_an_llm_exception_becomes_normalize_error_without_raising():
    """§7-6: raising here would reach BaseNode's boundary as status="error",
    GraphNode would skip merge_output() and the report would be lost."""
    result = run(RequestNormalizeNode(llm=_MockClient(raises=RuntimeError("llm down"))))
    assert result["error_code"] == ERROR_NORMALIZE
    assert result["normalized_query"] is None
    assert result["status"] == AgentStatus.SUCCESS.value
    assert ERROR_NORMALIZE in result["exception_report"]


def test_the_exception_type_is_audited_but_its_message_is_not():
    """A provider error message can echo request headers; the type name cannot."""
    result = run(RequestNormalizeNode(llm=_MockClient(raises=RuntimeError("key=" + DUMMY_OPENAI_KEY))))
    audit = json.loads(result["audit_log"])
    assert audit["llm_error"] == "RuntimeError"
    assert DUMMY_OPENAI_KEY not in result["audit_log"]


@pytest.mark.parametrize("response", [
    {"content": "no json here"},
    {"content": ""},
    {"content": "[1, 2, 3]"},
    {"content": "{not json}"},
    {},                       # no content key
    {"content": 42},          # content is not text
    None,                     # complete() returned nothing
    42,
])
def test_an_unusable_response_becomes_normalize_error(response):
    result = run(RequestNormalizeNode(llm=_MockClient(response=response)))
    assert result["error_code"] == ERROR_NORMALIZE
    assert result["normalized_query"] is None
    assert result["status"] == AgentStatus.SUCCESS.value


def test_a_pathologically_nested_response_fails_closed_instead_of_raising():
    """json.loads() raises RecursionError — not ValueError — past its stack depth.
    Letting it out of execute() would cost the exception report (§7-6)."""
    nested = '{"a":' * 5000 + "1" + "}" * 5000
    with pytest.raises(RecursionError):
        json.loads(nested)  # premise: the parser really does blow the stack here

    result = run(RequestNormalizeNode(llm=_MockClient(response=nested)))
    assert result["error_code"] == ERROR_NORMALIZE
    assert result["normalized_query"] is None
    assert result["status"] == AgentStatus.SUCCESS.value


def test_a_missing_secret_fails_closed_instead_of_raising():
    """llm=None is the production path: no provider is configured in a unit
    test, so require() raises MissingSecret. That must not escape execute()."""
    result = run(RequestNormalizeNode())
    assert result["error_code"] == ERROR_NORMALIZE
    assert result["normalized_query"] is None
    assert result["status"] == AgentStatus.SUCCESS.value
    assert json.loads(result["audit_log"])["llm_error"] == "MissingSecret"


def test_the_client_is_never_built_without_the_declared_secret():
    """require() must fail *before* the provider client is constructed (§12).

    Asserted on the exception type rather than through execute(), because every
    failure there collapses to the same NORMALIZE_ERROR: a node that passed an
    empty api_key to AnthropicClient would look identical from the outside while
    turning a missing-secret misconfiguration into a provider 401 at runtime.

    No provider is bound in a unit test, so ctx.secrets is the NullProvider
    whose require() always raises — this does not depend on the environment.
    """
    with pytest.raises(ValueError) as excinfo:
        RequestNormalizeNode()._build_client(_state())
    assert type(excinfo.value).__name__ == "MissingSecret"


def test_a_blank_request_never_reaches_the_llm():
    """Unreachable by contract — ⓪ nulls both fields whenever it sets an
    error_code — so this asserts the fail-closed backstop, not a live path."""
    llm = _MockClient()
    result = run(RequestNormalizeNode(llm=llm), request="   ")
    assert result["error_code"] == ERROR_NORMALIZE
    assert llm.calls == []


# ---------------------------------------------------------------- S-2 (§7-2)


def test_a_credential_in_the_request_is_removed_and_reported():
    node = RequestNormalizeNode(llm=_MockClient())
    state = node._extra_security_gate_input(_state(request=f"token {FAKE_JWT} で 2025 年度"))
    assert state["error_code"] == ERROR_CREDENTIAL_IN_REQUEST
    assert FAKE_JWT not in state["analyst_request"]
    assert REDACTION_MARKER_PREFIX in state["analyst_request"]
    assert "2025 年度" in state["analyst_request"]


def test_a_credential_in_the_schema_is_removed_leaf_wise():
    """A flat replacement would swallow the closing quote and the next key, and
    ② would receive an unparseable schema (§7-3)."""
    dirty = json.dumps({"dimensions": ["region"], "source": f"mysql://user:{DUMMY_OPENAI_KEY}@db/x"})
    node = RequestNormalizeNode(llm=_MockClient())
    state = node._extra_security_gate_input(_state(schema=dirty))
    assert state["error_code"] == ERROR_CREDENTIAL_IN_REQUEST
    assert DUMMY_OPENAI_KEY not in state["approved_schema"]
    assert json.loads(state["approved_schema"])["dimensions"] == ["region"]


def test_the_gate_never_raises_and_never_sets_error_status():
    """status = ERROR makes __call__() skip execute() (base_node.py:207-220) —
    and execute() is the only place the exception report is built."""
    node = RequestNormalizeNode(llm=_MockClient())
    state = node._extra_security_gate_input(_state(request=FAKE_JWT))
    assert state.get("status") != AgentStatus.ERROR.value


def test_the_gate_keeps_an_upstream_error_code_rather_than_relabelling_it():
    """§7-6 fail-closed pass-through: the first error wins. Relabelling ⓪'s
    MASKED_CRITICAL_FIELD as CREDENTIAL_IN_REQUEST would misattribute the
    failure to the analyst's text and send them to fix the wrong thing."""
    node = RequestNormalizeNode(llm=_MockClient())
    state = node._extra_security_gate_input(
        _state(request=f"token {FAKE_JWT}", error_code="MASKED_CRITICAL_FIELD")
    )
    assert state["error_code"] == "MASKED_CRITICAL_FIELD"
    assert FAKE_JWT not in state["analyst_request"], "removal must happen either way"


def test_the_gate_never_mutates_the_state_it_was_handed():
    """The hook's return value is what __call__() passes to execute()
    (base_node.py:206); the argument itself belongs to the framework. Mutating
    it in place would write the private carrier key into a dict LangGraph may
    still read, and would make the redaction unobservable to this assertion."""
    node = RequestNormalizeNode(llm=_MockClient())
    original = _state(request=f"token {FAKE_JWT} で 2025 年度")
    before = dict(original)

    node._extra_security_gate_input(original)

    assert original == before
    assert mod._S2_REDACTED_KEY not in original


def test_a_clean_request_passes_the_gate_untouched():
    node = RequestNormalizeNode(llm=_MockClient())
    original = _state()
    state = node._extra_security_gate_input(original)
    assert state["analyst_request"] == ANALYST_REQUEST
    assert state["approved_schema"] == APPROVED_SCHEMA
    assert state.get("error_code") is None
    assert original.get("error_code") is None, "the gate mutated the caller's dict"


def test_a_benign_data_source_survives_the_real_two_node_path():
    """The composition is what the analyst experiences. `mysql://dw_prod` is a
    benign identifier the conservative serialization-context probe redacts
    anyway (§7-3 `_serialization_tail`); ⓪ removes it and proceeds without an
    error_code, so by the time ① scans there is nothing left to find and the
    request must still be normalized."""
    from src.nodes.input_expand_node import InputExpandNode, pack_intake

    schema = json.dumps({"source": "mysql://dw_prod", "dimensions": ["region"]})
    expanded = InputExpandNode().execute({
        "user_input": pack_intake({"analyst_request": ANALYST_REQUEST, "approved_schema": schema}),
    })
    assert expanded["error_code"] is None

    node = RequestNormalizeNode(llm=_MockClient())
    result = gated(node, _state(request=expanded["analyst_request"], schema=expanded["approved_schema"]))
    assert result["error_code"] is None
    assert json.loads(result["normalized_query"])["region"] == "Osaka Prefecture"
    assert node._security_gate_output(result) is not None


def test_any_detector_hit_fails_closed_even_when_conservative():
    """The defence-in-depth branch: reachable only if ⓪'s removal regresses, so
    ① reports rather than second-guesses the detector. Over-reporting here costs
    one request; under-reporting puts the value in the checkpoint DB (§7-2)."""
    schema = json.dumps({"source": "mysql://dw_prod", "dimensions": ["region"]})
    state = RequestNormalizeNode(llm=_MockClient())._extra_security_gate_input(_state(schema=schema))
    assert state["error_code"] == ERROR_CREDENTIAL_IN_REQUEST
    assert json.loads(state["approved_schema"])["dimensions"] == ["region"]


def test_the_credential_path_skips_the_llm_and_keeps_the_redacted_text():
    """§7-2 ④: the redacted fields must be written to the channel, or ⓪'s
    original text stays in State — the checkpoint DB — and in get_output()."""
    llm = _MockClient()
    node = RequestNormalizeNode(llm=llm)
    result = gated(node, _state(request=f"token {FAKE_JWT}"))
    assert llm.calls == [], "the LLM saw a request that failed the S-2 gate"
    assert result["error_code"] == ERROR_CREDENTIAL_IN_REQUEST
    assert result["normalized_query"] is None
    assert FAKE_JWT not in result["analyst_request"]
    assert REDACTION_MARKER_PREFIX in result["analyst_request"]
    assert result["approved_schema"] == APPROVED_SCHEMA
    assert result["status"] == AgentStatus.SUCCESS.value


def test_the_credential_report_names_the_code_but_quotes_no_detected_text():
    """§7-3: quoting the value would make the report itself trip the @final S-3
    gate — and the report is the one artifact that has to survive."""
    node = RequestNormalizeNode(llm=_MockClient())
    result = gated(node, _state(request=f"token {FAKE_JWT}"))
    assert ERROR_CREDENTIAL_IN_REQUEST in result["exception_report"]
    assert FAKE_JWT not in result["exception_report"]
    assert detect_credentials(result["exception_report"]) == []
    assert node._security_gate_output(result) is not None


def test_the_redaction_count_reaches_the_audit_metadata():
    node = RequestNormalizeNode(llm=_MockClient())
    result = gated(node, _state(request=f"{FAKE_JWT} and {DUMMY_OPENAI_KEY}"))
    assert json.loads(result["audit_log"])["credentials_redacted"] == 2


def test_a_field_the_redactor_could_not_clean_is_nulled_not_written():
    """The mirror of ⓪'s post-removal re-scan, on a reachable case rather than a
    hypothetical one: `_redact_leaf` judges each leaf *before* JSON escaping, so
    this data source is clean as a leaf — the newline terminates conn_string's
    `[^\\s]{8,}` — and matches once json.dumps() writes the newline as the two
    non-space characters `\\n`. Writing that text to the channel would make the
    @final S-3 gate raise and take the exception report with it."""
    dirty = json.dumps({"key": DUMMY_OPENAI_KEY, "source": "mysql://a\nbcdefghij"})
    assert detect_credentials(dirty), "premise: the serialized form is detectable"

    node = RequestNormalizeNode(llm=_MockClient())
    result = gated(node, _state(schema=dirty))

    assert result["error_code"] == ERROR_CREDENTIAL_IN_REQUEST
    assert result["approved_schema"] is None
    assert json.loads(result["audit_log"])["unredactable_fields"] == ["approved_schema"]
    assert node._security_gate_output(result) is not None, "the @final gate must accept the delta"


def test_a_cleanable_field_is_still_written_back_rather_than_nulled():
    """The fail-closed branch must not swallow the normal case: §7-2 ④ exists so
    the redacted text replaces ⓪'s credential-bearing copy in the channel."""
    node = RequestNormalizeNode(llm=_MockClient())
    result = gated(node, _state(request=f"token {FAKE_JWT} で 2025 年度"))
    assert isinstance(result["analyst_request"], str)
    assert REDACTION_MARKER_PREFIX in result["analyst_request"]
    assert "unredactable_fields" not in json.loads(result["audit_log"])


def test_a_malformed_carrier_value_does_not_raise():
    """Only this node's own hook writes the carrier, so this is unreachable by
    contract — but "nothing in this module raises" is the contract that keeps the
    exception report alive, and int("x") would be the one place it broke."""
    result = run(**{mod._S2_REDACTED_KEY: "not-a-count"})
    assert result["status"] == AgentStatus.SUCCESS.value
    assert json.loads(result["audit_log"])["credentials_redacted"] == 0


def test_the_gate_to_execute_carrier_never_reaches_the_delta():
    """It is not a declared State key: LangGraph drops undeclared keys silently,
    so a leak here would be invisible until something depended on it."""
    node = RequestNormalizeNode(llm=_MockClient())
    result = gated(node, _state(request=f"token {FAKE_JWT}"))
    assert all(not key.startswith("_") for key in result)


# ---------------------------------------------------------------- S-3 removal on output


def test_a_credential_in_the_llm_output_is_removed_not_reported():
    """①'s removal duty (§7-3 適用ノード). The output is still usable, so this
    is a redaction — the clearance verdict belongs to ④, not here."""
    payload = dict(NORMALIZED, region=f"Osaka {DUMMY_OPENAI_KEY}")
    node = RequestNormalizeNode(llm=_MockClient(response={"content": json.dumps(payload, ensure_ascii=False)}))
    result = node.execute(_state())
    assert result["error_code"] is None
    assert DUMMY_OPENAI_KEY not in result["normalized_query"]
    assert REDACTION_MARKER_PREFIX in json.loads(result["normalized_query"])["region"]
    assert json.loads(result["audit_log"])["credentials_redacted"] == 1
    assert node._security_gate_output(result) is not None


def test_a_credential_in_a_filter_value_is_removed_and_the_document_survives():
    payload = dict(NORMALIZED, filters={"note": f"mysql://user:{DUMMY_OPENAI_KEY}@db/x"})
    node = RequestNormalizeNode(llm=_MockClient(response={"content": json.dumps(payload, ensure_ascii=False)}))
    result = node.execute(_state())
    query = json.loads(result["normalized_query"])
    assert DUMMY_OPENAI_KEY not in result["normalized_query"]
    assert query["period"] == "2025年度 月次"
    assert node._security_gate_output(result) is not None


def test_an_output_the_redactor_could_not_clean_fails_closed(monkeypatch):
    """Mirror of ⓪'s check on the property the gate actually tests. If the
    redactor regresses, reporting NORMALIZE_ERROR keeps the exception report;
    letting the @final gate raise loses it."""
    monkeypatch.setattr(mod, "redact_json_document", lambda raw: (raw, 0))
    payload = dict(NORMALIZED, region=f"Osaka {DUMMY_OPENAI_KEY}")
    node = RequestNormalizeNode(llm=_MockClient(response={"content": json.dumps(payload, ensure_ascii=False)}))
    result = node.execute(_state())
    assert result["error_code"] == ERROR_NORMALIZE
    assert result["normalized_query"] is None
    assert node._security_gate_output(result) is not None


def test_an_output_the_redactor_left_unparseable_fails_closed(monkeypatch):
    """② parses normalized_query. Handing it a broken document with no
    error_code set is the fail-open §8 exists to prevent."""
    monkeypatch.setattr(mod, "redact_json_document", lambda raw: ('{"period": ', 1))
    node = RequestNormalizeNode(llm=_MockClient())
    result = node.execute(_state())
    assert result["error_code"] == ERROR_NORMALIZE
    assert result["normalized_query"] is None


# ---------------------------------------------------------------- fail-closed pass-through


def test_an_upstream_error_code_skips_normalization_entirely():
    """§7-6: once a node has failed, every later node passes through."""
    llm = _MockClient()
    result = run(RequestNormalizeNode(llm=llm),
                 request=None, schema=None,
                 error_code="MISSING_INNER_INPUT",
                 exception_report="## 依頼を処理できませんでした\n\n- エラーコード: `MISSING_INNER_INPUT`\n")
    assert llm.calls == []
    assert result["error_code"] == "MISSING_INNER_INPUT"
    assert result["normalized_query"] is None
    assert result["status"] == AgentStatus.SUCCESS.value


def test_an_upstream_report_is_never_overwritten():
    """⓪'s report names ⓪ as the node that detected the problem. Rewriting it
    here would misattribute the failure."""
    upstream = "## 依頼を処理できませんでした\n\n- 発生ノード: `input_expand`\n"
    result = run(request=None, schema=None,
                 error_code="MASKED_CRITICAL_FIELD", exception_report=upstream)
    assert "exception_report" not in result or result["exception_report"] == upstream


def test_an_upstream_code_with_no_report_still_produces_one():
    """Reviewability is the deliverable (§7-6): an error_code with no report
    would leave the caller nothing to act on."""
    result = run(request=None, schema=None, error_code="MASKED_CRITICAL_FIELD")
    assert "MASKED_CRITICAL_FIELD" in result["exception_report"]


def test_a_report_for_a_code_this_node_does_not_own_still_states_a_reason():
    """The report ① writes for an upstream code cannot explain that code's
    cause — only ⓪ knows it — but it must still say something actionable.
    A code line with an empty reason is a report in name only."""
    report = mod.build_exception_report("MASKED_CRITICAL_FIELD")
    assert "MASKED_CRITICAL_FIELD" in report
    assert mod._UPSTREAM_REASON in report
    for reason in mod._REASONS.values():
        assert reason not in report, "an owned reason would misdescribe the cause"


def test_the_upstream_fields_are_not_rewritten_when_nothing_was_redacted():
    """The delta is what LangGraph writes. Re-writing analyst_request here would
    put this node's (unchanged) copy into the channel for no reason; the §7-2 ④
    obligation applies to the fields *this* node redacted."""
    result = run()
    assert "analyst_request" not in result
    assert "approved_schema" not in result


# ---------------------------------------------------------------- contract / wiring


def test_the_trust_level_is_declared_on_the_class():
    """gate-trust-level-check inspects the class body, not an inherited value."""
    assert RequestNormalizeNode.__dict__["required_trust_level"] is TrustLevel.VERIFIED_EXTERNAL


def test_every_path_emits_exactly_one_domain_audit_event(monkeypatch):
    """§7-4: node_start / node_complete / node_error belong to BaseNode.
    Emitting them here would duplicate them and break the audit trail."""
    events: list[str] = []
    monkeypatch.setattr(mod, "emit_trace_event", lambda event, payload, state: events.append(event))
    run()
    run(RequestNormalizeNode(llm=_MockClient(raises=RuntimeError("x"))))
    run(request=None, error_code="MISSING_INNER_INPUT")
    assert events == ["request_normalized"] * 3


def test_the_delta_carries_only_declared_state_keys():
    """LangGraph drops undeclared keys without raising, so a typo shows up as a
    missing field far downstream (§6)."""
    from src.schemas.state import State

    declared = set(State.__annotations__) | set(mod.AgentState.__annotations__)
    for result in (run(), run(RequestNormalizeNode(llm=_MockClient(raises=RuntimeError("x")))), gated(
        RequestNormalizeNode(llm=_MockClient()), _state(request=f"token {FAKE_JWT}")
    )):
        assert set(result) <= declared, set(result) - declared


def test_the_full_node_call_passes_the_s1_gate_and_records_history():
    node = RequestNormalizeNode(llm=_MockClient())
    result = node(_state(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL.value))
    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["node_history"] == ["RequestNormalizeNode"]
    assert json.loads(result["normalized_query"])["metric"] == "visitors"


def test_an_anonymous_caller_is_denied_before_the_llm_is_reached():
    llm = _MockClient()
    result = RequestNormalizeNode(llm=llm)(_state(caller_trust_level=TrustLevel.ANONYMOUS.value))
    assert result["status"] == AgentStatus.ERROR.value
    assert llm.calls == []


def test_no_module_constant_is_jwt_shaped():
    """S-5 scans class bodies and module constants at import time (§7-5)."""
    for name, value in vars(mod).items():
        if isinstance(value, str):
            assert not value.startswith("eyJ"), name


@pytest.mark.parametrize("dimension", QUERY_DIMENSIONS)
def test_each_declared_dimension_is_present_in_the_output(dimension):
    assert dimension in json.loads(run()["normalized_query"])
