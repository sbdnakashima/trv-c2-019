"""Inner ② query_validate — docs/02_design.md §7-6 / §8, DB-2 / DB-3.

The properties under test, in the order they matter:

1. Nothing is approved unless the schema approves it. An absent allowlist means
   "nothing of that kind is approved", never "anything goes".
2. A disclosure-restricted request stays DISCLOSURE_RESTRICTED. It cannot be
   relabelled into a milder code by adding an unrelated defect (DB-2).
3. The node never widens the request: no substring matching, no case folding,
   no metric substitution (DB-3).
4. status is always SUCCESS; anomalies travel as error_code + exception_report,
   because a raise or an ERROR status makes GraphNode skip merge_output() and
   the exception report never reaches the caller (§7-6).
"""
import json

import pytest
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.security.credential_detector import detect_credentials

from src.nodes.query_validate_node import (
    ERROR_AMBIGUOUS,
    ERROR_DISCLOSURE_RESTRICTED,
    ERROR_UNSUPPORTED,
    QueryValidateNode,
    build_exception_report,
)

APPROVED_SCHEMA = {
    "dimensions": ["period", "region", "segment", "metric"],
    "allowed_metrics": ["延べ宿泊者数", "訪問者数"],
    "allowed_filters": ["purpose"],
    "disclosure_restricted": ["個人識別", "市区町村"],
}

VALID_QUERY = {
    "period": "2023",
    "region": "東京都",
    "segment": "外国人",
    "metric": "延べ宿泊者数",
    "filters": {},
}


def _state(query=None, schema=None, **extra):
    """A state InvocationContext.from_state() can actually read.

    All four correlation fields are supplied: from_state() indexes them directly
    (invocation_context.py:33-37), so omitting one would turn assertions about
    this node into assertions about a KeyError.
    """
    state = {
        "normalized_query": None if query is None else json.dumps(query, ensure_ascii=False),
        "approved_schema": (
            json.dumps(APPROVED_SCHEMA if schema is None else schema, ensure_ascii=False)
            if schema is not False
            else None
        ),
        "session_id": "s",
        "correlation_id": "c",
        "thread_id": "t",
        "trace_id": "tr",
    }
    state.update(extra)
    return state


def gated(node, state):
    """execute() behind the framework's @final security gates, as __call__ runs it."""
    return node._security_gate_output(node.execute(node._security_gate_input(state)))


def _q(**overrides):
    query = dict(VALID_QUERY)
    query.update(overrides)
    return query


# --------------------------------------------------------------- happy path


def test_a_fully_approved_query_validates():
    result = gated(QueryValidateNode(), _state(VALID_QUERY))
    assert json.loads(result["validation_result"])["valid"] is True
    assert result["error_code"] is None
    assert result["status"] == AgentStatus.SUCCESS.value


def test_a_valid_query_gets_no_exception_report():
    result = gated(QueryValidateNode(), _state(VALID_QUERY))
    assert "exception_report" not in result


def test_an_approved_filter_key_is_accepted():
    result = gated(QueryValidateNode(), _state(_q(filters={"purpose": "観光"})))
    assert json.loads(result["validation_result"])["valid"] is True


# ------------------------------------------------------------------- status


def test_every_rejection_still_reports_success_so_the_report_survives():
    # §7-6: an ERROR status makes GraphNode.execute() skip merge_output(), which
    # discards the exception report this node exists to produce.
    for query in (
        _q(metric="消費額"),
        _q(period=None),
        _q(segment="個人識別"),
    ):
        result = gated(QueryValidateNode(), _state(query))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["error_code"] is not None
        assert result["exception_report"]


def test_the_node_never_raises_on_any_shape_the_channel_can_hold():
    node = QueryValidateNode()
    malformed_schemas = (None, False, [], "", {"dimensions": "period"}, {"allowed_metrics": {}})
    # The two filter shapes are built on VALID_QUERY on purpose: a bare
    # {"filters": ...} is undetermined in every dimension and would be answered
    # by the ambiguity check long before the filter branch ever ran.
    malformed_queries = (
        None,
        [],
        "",
        0,
        dict(VALID_QUERY, filters=[]),
        dict(VALID_QUERY, filters={"a": [{"b": None}]}),
    )
    for query in malformed_queries:
        # APPROVED_SCHEMA is in the list on purpose: paired only with unusable
        # schemas, every iteration would short-circuit on the schema and the
        # malformed *queries* would never actually be evaluated.
        for schema in malformed_schemas + (None,):
            state = _state(schema=APPROVED_SCHEMA if schema is None else schema)
            state["normalized_query"] = query if query is None else json.dumps(query)
            result = gated(node, state)
            assert result["status"] == AgentStatus.SUCCESS.value, (query, schema)
            # Not just "it did not raise": none of these shapes may be approved.
            assert result["error_code"] is not None, (query, schema)


# ---------------------------------------------------------------- ambiguity


def test_an_undetermined_dimension_is_ambiguous_not_guessed():
    result = gated(QueryValidateNode(), _state(_q(period=None)))
    assert result["error_code"] == ERROR_AMBIGUOUS
    assert json.loads(result["validation_result"])["valid"] is False


def test_a_blank_dimension_counts_as_undetermined():
    result = gated(QueryValidateNode(), _state(_q(region="   ")))
    assert result["error_code"] == ERROR_AMBIGUOUS


def test_every_undetermined_dimension_is_named_not_only_the_first():
    result = gated(QueryValidateNode(), _state(_q(period=None, segment=None)))
    report = result["exception_report"]
    assert "period" in report and "segment" in report


def test_a_missing_normalized_query_is_ambiguous_rather_than_approved():
    # ① nulls normalized_query only when it also sets an error_code, so this
    # means ① regressed. All four dimensions are undetermined — fail closed.
    result = gated(QueryValidateNode(), _state(query=None))
    assert result["error_code"] == ERROR_AMBIGUOUS


def test_an_unparseable_normalized_query_is_ambiguous():
    state = _state(VALID_QUERY)
    state["normalized_query"] = "{not json"
    result = gated(QueryValidateNode(), state)
    assert result["error_code"] == ERROR_AMBIGUOUS


def test_a_pathologically_nested_document_is_ambiguous_rather_than_fatal():
    # json.loads() blows the C stack rather than reporting on deep nesting, and
    # it raises RecursionError — not a ValueError. Letting it out of execute()
    # would be turned into status="error" by BaseNode.__call__(), and
    # GraphNode.execute() would then skip merge_output(): the caller would get
    # no exception report at all (§7-6).
    state = _state(VALID_QUERY)
    state["normalized_query"] = "[" * 5000 + "]" * 5000
    with pytest.raises(RecursionError):
        json.loads(state["normalized_query"])
    result = gated(QueryValidateNode(), state)
    assert result["error_code"] == ERROR_AMBIGUOUS


def test_a_json_array_is_not_accepted_as_a_query():
    state = _state(VALID_QUERY)
    state["normalized_query"] = json.dumps([VALID_QUERY])
    result = gated(QueryValidateNode(), state)
    assert result["error_code"] == ERROR_AMBIGUOUS


# --------------------------------------------------------- schema boundary


def test_a_metric_outside_the_allowlist_is_unsupported():
    result = gated(QueryValidateNode(), _state(_q(metric="消費額")))
    assert result["error_code"] == ERROR_UNSUPPORTED


def test_a_dimension_the_schema_does_not_approve_is_unsupported():
    schema = dict(APPROVED_SCHEMA, dimensions=["period", "region", "metric"])
    result = gated(QueryValidateNode(), _state(VALID_QUERY, schema=schema))
    assert result["error_code"] == ERROR_UNSUPPORTED
    assert "segment" in result["exception_report"]


def test_a_filter_key_outside_the_allowlist_is_unsupported():
    result = gated(QueryValidateNode(), _state(_q(filters={"nationality": "KR"})))
    assert result["error_code"] == ERROR_UNSUPPORTED


def test_filters_are_unapproved_by_default_rather_than_unrestricted():
    # The whole fail-closed rule in one case: a schema that says nothing about
    # filters approves no filter, instead of approving every filter.
    schema = {k: v for k, v in APPROVED_SCHEMA.items() if k != "allowed_filters"}
    result = gated(QueryValidateNode(), _state(_q(filters={"purpose": "観光"}), schema=schema))
    assert result["error_code"] == ERROR_UNSUPPORTED


@pytest.mark.parametrize("filters", [["nationality", "個人所得"], "nationality=KR", 7])
def test_a_filter_set_that_is_not_an_object_is_rejected_not_forgotten(filters):
    # Coercing it to {} would skip the filter allowlist entirely — and because ②
    # never rewrites normalized_query (DB-3), ③ would then read the very filter
    # set ② declined to look at. ① rejects a non-object `filters` upstream, which
    # is the point: this node's contract is that it does not trust ①.
    result = gated(QueryValidateNode(), _state(_q(filters=filters)))
    assert result["error_code"] == ERROR_UNSUPPORTED
    assert json.loads(result["audit_log"])["malformed"] == "filters"


def test_an_empty_filter_object_needs_no_filter_allowlist():
    schema = {k: v for k, v in APPROVED_SCHEMA.items() if k != "allowed_filters"}
    result = gated(QueryValidateNode(), _state(VALID_QUERY, schema=schema))
    assert json.loads(result["validation_result"])["valid"] is True


@pytest.mark.parametrize(
    "schema",
    [
        False,                                          # the field is absent
        {},                                             # an empty object
        {"dimensions": [], "allowed_metrics": []},      # empty allowlists
        {"allowed_metrics": ["延べ宿泊者数"]},            # no approved dimensions
        {"dimensions": ["period", "region", "segment", "metric"]},   # no approved metrics
        {"dimensions": "period", "allowed_metrics": ["延べ宿泊者数"]},  # wrong type
        [APPROVED_SCHEMA],                              # not an object
    ],
)
def test_a_schema_that_approves_nothing_approves_nothing(schema):
    result = gated(QueryValidateNode(), _state(VALID_QUERY, schema=schema))
    assert result["error_code"] == ERROR_UNSUPPORTED, schema


def test_an_unparseable_schema_is_unsupported_not_waved_through():
    state = _state(VALID_QUERY)
    state["approved_schema"] = "{not json"
    result = gated(QueryValidateNode(), state)
    assert result["error_code"] == ERROR_UNSUPPORTED


def test_non_string_entries_in_an_allowlist_do_not_approve_anything():
    schema = dict(APPROVED_SCHEMA, allowed_metrics=[None, 1, ["延べ宿泊者数"]])
    result = gated(QueryValidateNode(), _state(VALID_QUERY, schema=schema))
    assert result["error_code"] == ERROR_UNSUPPORTED


def test_one_bad_entry_makes_the_whole_allowlist_unusable():
    # Not "skip the bad entry and keep approving the rest": a partly-corrupt
    # allowlist is a schema whose author cannot be assumed to have reviewed what
    # survived. It approves nothing until someone fixes it.
    schema = dict(APPROVED_SCHEMA, dimensions=["period", "region", "segment", "metric", 123])
    result = gated(QueryValidateNode(), _state(VALID_QUERY, schema=schema))
    assert result["error_code"] == ERROR_UNSUPPORTED


def test_a_mapping_is_not_accepted_where_a_list_of_approved_values_belongs():
    # Iterating a dict yields its keys, so a lenient reading would quietly treat
    # {"period": ...} as an approval of "period".
    schema = dict(APPROVED_SCHEMA, dimensions={d: True for d in ("period", "region", "segment", "metric")})
    result = gated(QueryValidateNode(), _state(VALID_QUERY, schema=schema))
    assert result["error_code"] == ERROR_UNSUPPORTED


def test_an_unusable_schema_is_reported_as_a_schema_problem_not_a_query_problem():
    # Both routes end in UNSUPPORTED_QUERY, so the code alone cannot tell an
    # operator whether to fix the schema or the analyst to fix the request.
    # The audit trail has to say which one it was.
    schema = dict(APPROVED_SCHEMA, dimensions=[])
    result = gated(QueryValidateNode(), _state(VALID_QUERY, schema=schema))
    assert json.loads(result["audit_log"])["missing"] == "approved_schema"


# ------------------------------------------------- no implicit widening (DB-3)


def test_a_prefix_of_an_approved_metric_is_not_an_approved_metric():
    result = gated(QueryValidateNode(), _state(_q(metric="延べ")))
    assert result["error_code"] == ERROR_UNSUPPORTED


def test_an_approved_metric_with_extra_text_is_not_approved():
    result = gated(QueryValidateNode(), _state(_q(metric="延べ宿泊者数（推計）")))
    assert result["error_code"] == ERROR_UNSUPPORTED


def test_case_is_not_folded_when_matching_an_allowlist():
    schema = dict(APPROVED_SCHEMA, allowed_metrics=["Overnight"])
    result = gated(QueryValidateNode(), _state(_q(metric="overnight"), schema=schema))
    assert result["error_code"] == ERROR_UNSUPPORTED


def test_surrounding_whitespace_is_not_silently_accepted_into_an_allowlist():
    result = gated(QueryValidateNode(), _state(_q(metric=" 延べ宿泊者数 ")))
    assert result["error_code"] == ERROR_UNSUPPORTED


def test_the_node_never_rewrites_the_query_it_was_asked_to_validate():
    state = _state(_q(metric="消費額"))
    original = state["normalized_query"]
    result = gated(QueryValidateNode(), state)
    assert "normalized_query" not in result
    assert state["normalized_query"] == original


# ------------------------------------------------------- disclosure (DB-2)


def test_a_restricted_dimension_value_is_disclosure_restricted():
    result = gated(QueryValidateNode(), _state(_q(segment="個人識別")))
    assert result["error_code"] == ERROR_DISCLOSURE_RESTRICTED


def test_a_restricted_value_hidden_in_a_filter_value_is_still_caught():
    # Otherwise the dimension allowlist is a front door with the back door open.
    result = gated(QueryValidateNode(), _state(_q(filters={"purpose": "個人識別"})))
    assert result["error_code"] == ERROR_DISCLOSURE_RESTRICTED


def test_a_restricted_value_used_as_a_filter_key_is_still_caught():
    result = gated(QueryValidateNode(), _state(_q(filters={"個人識別": "1"})))
    assert result["error_code"] == ERROR_DISCLOSURE_RESTRICTED


def test_a_restricted_value_nested_inside_a_filter_is_still_caught():
    result = gated(
        QueryValidateNode(),
        _state(_q(filters={"purpose": {"detail": ["市区町村"]}})),
    )
    assert result["error_code"] == ERROR_DISCLOSURE_RESTRICTED


def test_a_restricted_value_inside_a_list_of_objects_is_still_caught():
    # A filter is free-form JSON, so the restricted term can sit behind any
    # mixture of lists and objects. A walk that recursed through objects but
    # only skimmed the strings out of lists would leave that path open.
    result = gated(
        QueryValidateNode(),
        _state(_q(filters={"purpose": [{"detail": "市区町村"}]})),
    )
    assert result["error_code"] == ERROR_DISCLOSURE_RESTRICTED


def test_a_restricted_value_parked_under_an_unlisted_key_is_still_caught():
    # The sweep must cover the document, not the five keys this module knows
    # about. §8 hands ③ the whole normalized_query dict and ② never rewrites it
    # (DB-3), so a term the sweep skipped would travel into the dataset query
    # intact — the same smuggling route as the filters case, through a different
    # door.
    query = dict(VALID_QUERY, nationality="個人識別")
    result = gated(QueryValidateNode(), _state(query))
    assert result["error_code"] == ERROR_DISCLOSURE_RESTRICTED


def test_a_restricted_term_used_as_an_unlisted_key_name_is_still_caught():
    query = dict(VALID_QUERY, **{"個人識別": "1"})
    result = gated(QueryValidateNode(), _state(query))
    assert result["error_code"] == ERROR_DISCLOSURE_RESTRICTED


def test_an_unlisted_key_is_rejected_even_when_what_it_holds_is_harmless():
    # ② approves a query it has inspected, and it has no rule for this field.
    result = gated(QueryValidateNode(), _state(dict(VALID_QUERY, group_by="月次")))
    assert result["error_code"] == ERROR_UNSUPPORTED
    assert json.loads(result["validation_result"])["valid"] is False
    assert json.loads(result["audit_log"])["unknown_keys"] == 1


def test_the_five_structural_key_names_are_not_themselves_swept():
    # Restricting a term must not depend on this module's own vocabulary: a
    # schema that happens to restrict "metric" should not reject every query.
    schema = dict(APPROVED_SCHEMA, disclosure_restricted=["metric", "filters"])
    result = gated(QueryValidateNode(), _state(VALID_QUERY, schema=schema))
    assert result["error_code"] is None


def test_a_disclosure_restriction_outranks_an_unrelated_defect():
    # DB-2: the restriction must not be suppressible. If an unsupported metric
    # were reported first, adding one would relabel a restricted request.
    result = gated(QueryValidateNode(), _state(_q(segment="個人識別", metric="消費額")))
    assert result["error_code"] == ERROR_DISCLOSURE_RESTRICTED


def test_a_disclosure_restriction_outranks_an_undetermined_dimension():
    result = gated(QueryValidateNode(), _state(_q(segment="個人識別", period=None)))
    assert result["error_code"] == ERROR_DISCLOSURE_RESTRICTED


def test_a_disclosure_restriction_is_checked_even_in_an_unapproved_filter():
    # The filter key is unapproved *and* the value is restricted; the restriction
    # is the finding that must survive.
    result = gated(QueryValidateNode(), _state(_q(filters={"nationality": "個人識別"})))
    assert result["error_code"] == ERROR_DISCLOSURE_RESTRICTED


def test_a_restricted_value_matches_exactly_and_not_by_substring():
    # "市区町村別の傾向" merely contains "市区町村". A denylist that matched substrings
    # would report a disclosure restriction the policy never made — and, worse,
    # would make the restriction list's blast radius impossible to review, since
    # every value containing a listed term would be caught by it.
    result = gated(QueryValidateNode(), _state(_q(region="市区町村別の傾向")))
    assert result["error_code"] is None
    assert json.loads(result["validation_result"])["valid"] is True


def test_a_value_that_merely_contains_a_restricted_term_is_judged_on_its_own_merits():
    # Same substring, but this time the query is independently unsupported. The
    # finding must be the real defect, not a disclosure restriction invented by
    # a loose match.
    result = gated(QueryValidateNode(), _state(_q(region="市区町村別の傾向", metric="未承認指標")))
    assert result["error_code"] == ERROR_UNSUPPORTED


def test_an_absent_restriction_list_restricts_nothing():
    schema = {k: v for k, v in APPROVED_SCHEMA.items() if k != "disclosure_restricted"}
    result = gated(QueryValidateNode(), _state(VALID_QUERY, schema=schema))
    assert json.loads(result["validation_result"])["valid"] is True


@pytest.mark.parametrize(
    "restricted",
    [
        "個人識別",              # a bare string where a one-element list belongs
        {"個人識別": True},       # an object
        ["個人識別", 123],        # a list with one bad entry
    ],
)
def test_a_malformed_restriction_list_is_a_broken_schema_not_an_absent_one(restricted):
    # Absent means "restricts nothing"; malformed must not. The allowlists bound
    # the dimension *names* and the *metric* value only — a period / region /
    # segment value is bounded by nothing but this list, so failing open here
    # turns the whole disclosure policy off, and a query naming a restricted
    # segment would validate. DB-2 requires the restriction to be neither
    # suppressible nor evadable; a typo in the schema must not suppress it.
    schema = dict(APPROVED_SCHEMA, disclosure_restricted=restricted)
    result = gated(QueryValidateNode(), _state(_q(segment="個人識別"), schema=schema))
    assert result["error_code"] == ERROR_UNSUPPORTED
    assert json.loads(result["audit_log"])["missing"] == "approved_schema"


# ------------------------------------------------------ upstream pass-through


def test_an_upstream_error_code_is_carried_not_relabelled():
    result = gated(
        QueryValidateNode(),
        _state(VALID_QUERY, error_code="NORMALIZE_ERROR"),
    )
    assert result["error_code"] == "NORMALIZE_ERROR"


def test_an_upstream_exception_report_is_not_overwritten():
    # The upstream report names the node that detected the problem; rewriting it
    # would misattribute the failure.
    result = gated(
        QueryValidateNode(),
        _state(
            _q(metric="消費額"),
            error_code="NORMALIZE_ERROR",
            exception_report="## upstream\n",
        ),
    )
    assert "exception_report" not in result


def test_validation_is_skipped_entirely_when_an_error_is_already_set():
    # Fail closed (§7-6): a query that never reached this node's contract must
    # not be reported as valid, and must not be re-diagnosed either.
    result = gated(
        QueryValidateNode(),
        _state(VALID_QUERY, error_code="MISSING_INNER_INPUT"),
    )
    parsed = json.loads(result["validation_result"])
    assert parsed["valid"] is False
    assert parsed["reason"] == "upstream_error"


# --------------------------------------------------------------- S-3 / audit


def test_the_reason_names_the_value_that_was_rejected():
    result = gated(QueryValidateNode(), _state(_q(metric="消費額")))
    assert "消費額" in json.loads(result["validation_result"])["reason"]


def test_a_credential_shaped_value_is_not_quoted_back_into_the_artifacts():
    # The @final S-3 output gate scans every string in the delta and raises on a
    # hit — which would cost the exception report (§7-6). ① guarantees a clean
    # normalized_query, so this is the fail-safe for ① regressing, and it is
    # checked on the assembled artifact rather than on the value, because
    # quoting a clean value can still complete a pattern across the join.
    dirty = "mysql://" + "a" * 40
    assert detect_credentials(dirty), "premise: the value is detectable"
    node = QueryValidateNode()
    result = gated(node, _state(_q(metric=dirty)))
    assert result["error_code"] == ERROR_UNSUPPORTED
    assert dirty not in result["validation_result"]
    assert dirty not in result["exception_report"]
    assert node._security_gate_output(result) is result, "the @final gate must accept the delta"


def test_a_value_that_only_turns_credential_shaped_once_serialized_is_still_caught():
    # The leaf is clean: a real newline is whitespace, which breaks the
    # `mysql://[^\s]{8,}` run. json.dumps() then writes that newline as the two
    # non-space characters \n and closes the run again — so a node that judged
    # the value instead of the document it writes would hand the @final gate a
    # string the gate rejects, and the RuntimeError would cost the exception
    # report (§7-6). This is the Issue #4 root cause in its second location.
    sneaky = "mysql://abc\ndefghij"
    assert not detect_credentials(sneaky), "premise: the leaf is clean"
    assert detect_credentials(json.dumps({"reason": sneaky})), "premise: the document is not"
    node = QueryValidateNode()
    result = gated(node, _state(_q(metric=sneaky)))
    assert result["error_code"] == ERROR_UNSUPPORTED
    assert not detect_credentials(result["validation_result"])
    assert not detect_credentials(result["exception_report"])
    assert node._security_gate_output(result) is result, "the @final gate must accept the delta"


def test_a_value_that_is_credential_shaped_only_before_serialization_is_still_caught():
    # The mirror of the case above, and the reason both artifacts are judged
    # rather than one standing in for the other. `Bearer\s+[\w.\-]{16,}` is the
    # one pattern that *requires* whitespace, and json.dumps() does not merely
    # insert characters around a tab — it replaces the tab with the two
    # non-space characters \t, removing the whitespace the pattern needs. So
    # here the raw report matches and the serialized validation_result does not.
    sneaky = "Bearer\t" + "A" * 20
    assert detect_credentials(f"- 理由: {sneaky}"), "premise: the raw report is dirty"
    assert not detect_credentials(json.dumps({"reason": sneaky})), "premise: the JSON is not"
    node = QueryValidateNode()
    result = gated(node, _state(_q(metric=sneaky)))
    assert result["error_code"] == ERROR_UNSUPPORTED
    assert not detect_credentials(result["exception_report"])
    # Both artifacts are withheld together, so the clean one is scrubbed too:
    # the flag proves the withholding branch ran rather than the report simply
    # never quoting the value.
    assert json.loads(result["audit_log"])["reason_withheld"] is True
    assert sneaky not in result["validation_result"]
    assert sneaky not in result["exception_report"]
    assert node._security_gate_output(result) is result, "the @final gate must accept the delta"


def test_a_withheld_reason_is_recorded_in_the_audit_trail():
    # The report says why the request failed but not what was in it. Without
    # this flag nothing tells a reviewer that a reason was suppressed at all.
    result = gated(QueryValidateNode(), _state(_q(metric="mysql://" + "a" * 40)))
    assert json.loads(result["audit_log"])["reason_withheld"] is True


def test_a_clean_rejection_is_not_marked_as_withheld():
    result = gated(QueryValidateNode(), _state(_q(metric="消費額")))
    assert "reason_withheld" not in json.loads(result["audit_log"])


def test_the_audit_payload_carries_names_and_counts_but_no_analyst_values():
    # error_code and audit_log are scanned by the @final gate like every other
    # string in the delta, but neither can be withheld: ③/④ and merge_output()
    # read the code, and the audit line embeds it. What keeps them safe is an
    # invariant — codes are module constants, audit payloads carry field names
    # and counts — so the invariant is asserted rather than assumed. A distinct
    # sentinel per field means no field can leak under another one's cover.
    sentinels = {
        "period": "SENTINEL-PERIOD",
        "region": "SENTINEL-REGION",
        "segment": "SENTINEL-SEGMENT",
        "metric": "SENTINEL-METRIC",
    }
    query = dict(VALID_QUERY, **sentinels, filters={"SENTINEL-FILTER": "SENTINEL-FILTER-VALUE"})
    result = gated(QueryValidateNode(), _state(query))
    audit = result["audit_log"]
    for field, sentinel in sentinels.items():
        assert sentinel not in audit, field
    assert "SENTINEL-FILTER" not in audit
    assert json.loads(audit)["error_code"] == ERROR_UNSUPPORTED


def test_a_domain_audit_event_is_emitted(caplog):
    with caplog.at_level("INFO", logger="agentcore.audit"):
        gated(QueryValidateNode(), _state(VALID_QUERY))
    events = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "agentcore.audit"
    ]
    assert any(event.get("event_type") == "query_validated" for event in events)


def test_the_delta_is_a_delta_and_not_the_whole_state():
    # node_history and error_log are Annotated[list[str], operator.add] channels;
    # spreading state re-concatenates the whole history at every node (§6).
    result = gated(QueryValidateNode(), _state(VALID_QUERY))
    assert "node_history" not in result
    assert "session_id" not in result
    assert "approved_schema" not in result


def test_the_node_does_not_mutate_the_state_it_was_handed():
    state = _state(_q(metric="消費額"))
    before = json.dumps(state, ensure_ascii=False, sort_keys=True)
    gated(QueryValidateNode(), state)
    assert json.dumps(state, ensure_ascii=False, sort_keys=True) == before


# ------------------------------------------------------------------ S-1 / S-5


def test_the_node_requires_a_verified_caller():
    assert QueryValidateNode.required_trust_level is TrustLevel.VERIFIED_EXTERNAL


def test_a_rejection_survives_the_real_call_boundary_not_just_execute():
    # gated() hand-rolls the gate order; this drives the framework's own
    # __call__, which is where a RuntimeError from the @final output gate would
    # be turned into status="error" and the exception report lost (§7-6).
    node = QueryValidateNode()
    state = _state(_q(metric="Bearer\t" + "A" * 20))
    state["caller_trust_level"] = TrustLevel.VERIFIED_EXTERNAL.value
    result = node(state)
    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["error_code"] == ERROR_UNSUPPORTED
    assert result["exception_report"]


def test_an_approved_query_survives_the_real_call_boundary():
    node = QueryValidateNode()
    state = _state(VALID_QUERY)
    state["caller_trust_level"] = TrustLevel.VERIFIED_EXTERNAL.value
    result = node(state)
    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["error_code"] is None
    assert json.loads(result["validation_result"])["valid"] is True


def test_an_anonymous_caller_is_denied_before_execute_runs():
    node = QueryValidateNode()
    state = _state(VALID_QUERY)
    state["caller_trust_level"] = TrustLevel.ANONYMOUS.value
    result = node(state)
    assert result["status"] == AgentStatus.ERROR.value
    assert "S-1 trust gate denied" in result["error_log"][0]


# ----------------------------------------------------------------- reporting


def test_the_report_states_the_code_and_the_node_that_found_it():
    report = build_exception_report(ERROR_DISCLOSURE_RESTRICTED, "理由")
    assert ERROR_DISCLOSURE_RESTRICTED in report
    assert "query_validate" in report
    assert "理由" in report


def test_a_code_this_node_does_not_own_still_gets_a_readable_report():
    report = build_exception_report("SOMETHING_ELSE", "")
    assert "SOMETHING_ELSE" in report
    assert report.strip().endswith(("。", "`"))
