"""AgentCore Platform v1.0 — Inner ⓪ input_expand (docs/02_design.md §5-2 / §5-3)

This module is the single canonical home of the intake contract. Outer
`extract_input()` and Inner ⓪ both import from here; the pack and unpack halves
must never be duplicated, or they drift apart and every request fails.

    Outer.extract_input(state) = pack_intake(build_intake_payload(user_input))
        ↓ inner user_input (packed string)
    Inner ⓪ execute() = unpack_intake(...) → analyst_request / approved_schema

Nothing in this module raises. An exception inside execute() is caught by
BaseNode.__call__() and turned into status="error", which makes
GraphNode.execute() skip merge_output() — the exception report never reaches the
caller (§7-6). Every intake anomaly is therefore carried as a *value*:
`_intake_error` inside the payload, then `error_code` on the state.
"""

import json
from typing import Any, ClassVar, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.security.credential_detector import detect_credentials
from shared.utils.audit_logger import emit_trace_event

from src.security.credential_redaction import redact_credentials, redact_json_document

# The intake key set. Both fields are required; `approved_schema` is a JSON
# string (§6 — msgpack cannot carry native containers through a checkpoint).
INNER_INPUT_FIELDS = ("analyst_request", "approved_schema")

# Prefix of the packed string (§5-3). Normative constraints: [A-Za-z0-9_] only,
# no whitespace, no run of Title Case words, and it must end in a letter.
#
# The reason is the @final S-2 gate: it runs detect_pii() on `user_input` and
# replaces hits with [MASKED] before execute() sees them. The `name` pattern
# \b[A-Z][a-z]{1,20}(?:\s[A-Z][a-z]{1,20})+\b matches *any* two Title Case words,
# so a plain-JSON request loses its region and segment names ("Osaka Prefecture",
# "Foreign Visitors"). Lowercase alphanumerics cannot match `name`/`email`/
# `phone`/`ssn` structurally — not merely "measured as 0 hits today" — so the
# sentinel keeps working if the detector gains patterns later.
INTAKE_SENTINEL = "trvc2019intake"

# What the S-2 gate leaves behind. Detected before json.loads() so a masked
# request is not misreported as malformed JSON (§8 判定順序).
MASK_MARKER = "[MASKED]"

_INTAKE_ERROR_KEY = "_intake_error"

ERROR_MISSING_INNER_INPUT = "MISSING_INNER_INPUT"
ERROR_INVALID_INNER_INPUT = "INVALID_INNER_INPUT"
ERROR_MASKED_CRITICAL_FIELD = "MASKED_CRITICAL_FIELD"

_REASONS = {
    ERROR_MISSING_INNER_INPUT: (
        "依頼本文または承認済みスキーマが指定されていません。"
        f"`{'` / `'.join(INNER_INPUT_FIELDS)}` の両方を指定してください。"
    ),
    ERROR_INVALID_INNER_INPUT: "依頼を JSON として解釈できませんでした。",
    ERROR_MASKED_CRITICAL_FIELD: (
        "必須項目にマスク処理が及んだため復元できませんでした。" "個人情報に該当しうる表現を含めずに再送してください。"
    ),
}

_NODE_LABEL = "input_expand"


def _error(code: str) -> dict[str, Any]:
    return {_INTAKE_ERROR_KEY: code}


def build_intake_payload(raw_user_input: Any) -> dict[str, Any]:
    """Parse the caller's JSON request into the intake payload. Never raises.

    Called by Outer `extract_input()`. Failure is reported as `_intake_error`,
    which ⓪ surfaces verbatim as `error_code` — the caller sees the same
    taxonomy whichever side detected the problem.
    """
    if not isinstance(raw_user_input, str) or not raw_user_input.strip():
        return _error(ERROR_MISSING_INNER_INPUT)
    if MASK_MARKER in raw_user_input:
        return _error(ERROR_MASKED_CRITICAL_FIELD)

    try:
        parsed = json.loads(raw_user_input)
    except ValueError:
        return _error(ERROR_INVALID_INNER_INPUT)
    if not isinstance(parsed, dict):
        return _error(ERROR_INVALID_INNER_INPUT)

    payload = {}
    for field in INNER_INPUT_FIELDS:
        value = parsed.get(field)
        if not isinstance(value, str) or not value.strip():
            return _error(ERROR_MISSING_INNER_INPUT)
        payload[field] = value
    return payload


def pack_intake(payload: dict[str, Any]) -> str:
    """``INTAKE_SENTINEL + utf-8(json).hex()``. Never raises.

    hex, not base64: JSON's leading `{"` encodes to `eyJ`, which the
    credential_detector `jwt` pattern (`eyJ[a-zA-Z0-9._\\-]{10,}`) always
    matches. hex output cannot contain `eyJ` at all.
    """
    try:
        body = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        # Unserializable payload: still return a well-formed packed string so ⓪
        # reports INVALID_INNER_INPUT rather than choking on a bare sentinel.
        body = json.dumps(_error(ERROR_INVALID_INNER_INPUT))
    return INTAKE_SENTINEL + body.encode("utf-8").hex()


def unpack_intake(packed: Any) -> dict[str, Any]:
    """Inverse of :func:`pack_intake`. Never raises.

    Any structural failure collapses to INVALID_INNER_INPUT: a packed string
    that will not decode carries no information about *why*, and guessing would
    put untrusted bytes into the exception report.
    """
    if not isinstance(packed, str) or not packed.startswith(INTAKE_SENTINEL):
        return _error(ERROR_INVALID_INNER_INPUT)
    try:
        # UnicodeDecodeError and JSONDecodeError are both ValueError subclasses,
        # as is bytes.fromhex()'s error on a non-hex or odd-length body.
        parsed = json.loads(bytes.fromhex(packed[len(INTAKE_SENTINEL) :]).decode("utf-8"))
    except ValueError:
        return _error(ERROR_INVALID_INNER_INPUT)
    if not isinstance(parsed, dict):
        return _error(ERROR_INVALID_INNER_INPUT)
    return parsed


def _classify(payload: dict[str, Any]) -> Optional[str]:
    """Fixed decision order of §8. Returns an error_code, or None when valid.

    Order matters: the [MASKED] check precedes json.loads() so a request the S-2
    gate damaged is reported as MASKED_CRITICAL_FIELD instead of "invalid JSON",
    which would send the analyst off debugging their own well-formed request.
    """
    carried = payload.get(_INTAKE_ERROR_KEY)
    if carried:
        return carried if isinstance(carried, str) else ERROR_INVALID_INNER_INPUT

    for field in INNER_INPUT_FIELDS:
        value = payload.get(field)
        if isinstance(value, str) and MASK_MARKER in value:
            return ERROR_MASKED_CRITICAL_FIELD

    schema = payload.get("approved_schema")
    if isinstance(schema, str) and schema.strip():
        try:
            json.loads(schema)
        except ValueError:
            return ERROR_INVALID_INNER_INPUT

    for field in INNER_INPUT_FIELDS:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            return ERROR_MISSING_INNER_INPUT
    return None


def build_exception_report(error_code: str) -> str:
    """Reviewable Markdown report. Quotes no request text and no detected value.

    §7-3: quoting a detected credential would make the report itself trip the
    @final S-3 gate, and the report is the one artifact that must survive.
    """
    return (
        "## 依頼を処理できませんでした\n\n"
        f"- エラーコード: `{error_code}`\n"
        f"- 発生ノード: `{_NODE_LABEL}`\n"
        f"- 理由: {_REASONS.get(error_code, '内部エラーが発生しました。')}\n"
    )


class InputExpandNode(FunctionNode):
    """Inner ⓪ — restore analyst_request / approved_schema from packed intake.

    Security: S-1 (trust level below) / S-3 removal / S-4.

    S-3 removal applies here because the restored fields are caller-derived and
    go straight into the return delta, where the @final gate would raise on a
    credential. Per §8 the CREDENTIAL_IN_REQUEST code belongs to ① rather than
    ⓪, so removal is recorded in the audit event and the request proceeds on
    redacted text — the credential itself never reaches the output.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> dict[str, Any]:
        payload = unpack_intake(state.get("user_input"))
        error_code = _classify(payload)

        analyst_request, removed_request = redact_credentials(payload.get("analyst_request"))
        # Leaf-wise for the schema (§7-3): a flat replacement would swallow the
        # closing quote and the following key, and ② would then receive an
        # unparseable schema with no error_code set.
        approved_schema, removed_schema = redact_json_document(payload.get("approved_schema"))
        removed = removed_request + removed_schema

        # Re-validate after removal, never before. Leaf-wise redaction preserves
        # the document, but that must be asserted at the boundary rather than
        # assumed: an unparseable schema reaching ② is exactly the fail-open
        # §8 assigns INVALID_INNER_INPUT to prevent.
        if not error_code and isinstance(approved_schema, str) and approved_schema.strip():
            try:
                json.loads(approved_schema)
            except ValueError:
                error_code = ERROR_INVALID_INNER_INPUT

        # The mirror of that check, on the property the gate actually tests. The
        # gate scans for *cleanliness*, not parseability, and it scans the whole
        # serialized string — so a leaf-wise redactor that judged a leaf in
        # isolation, or a removal loop that gave up at its cap, would hand it
        # something it rejects. Reporting INVALID_INNER_INPUT here keeps §7-6's
        # exception report; letting the @final gate raise loses it (a RuntimeError
        # becomes status="error" and GraphNode skips merge_output()).
        if not error_code and any(
            detect_credentials(value) for value in (analyst_request, approved_schema) if isinstance(value, str)
        ):
            error_code = ERROR_INVALID_INNER_INPUT

        # S-4 (§7-4). Field names and counts only — an audit log is not a secret
        # store, and node_start/node_complete are BaseNode's job, not ours.
        emit_trace_event(
            "intake_expanded",
            {
                "node": _NODE_LABEL,
                "error_code": error_code,
                "credentials_redacted": removed,
                "fields_restored": [field for field in INNER_INPUT_FIELDS if isinstance(payload.get(field), str)],
            },
            state,
        )

        # Delta only — never {**state, ...}: node_history and error_log are
        # Annotated[list[str], operator.add] channels and a spread re-concatenates
        # the whole history on every node (§6).
        #
        # On error both fields are cleared to None, unconditionally. Two reasons:
        # a partially-restored request must not reach ①, and the payload's values
        # are caller-controlled — a non-str leaf would otherwise be written into
        # an Optional[str] channel, violating §6 (list/dict are held as JSON
        # strings) and failing §11 TC-01. Writing None explicitly rather than
        # omitting the key is the fail-closed part: LangGraph's non-reducer
        # channels are last-write-wins, so an omitted key leaves whatever a
        # previous run's checkpoint held.
        delta: dict[str, Any] = {
            "status": AgentStatus.SUCCESS.value,
            "analyst_request": None if error_code else analyst_request,
            "approved_schema": None if error_code else approved_schema,
            "error_code": error_code,
        }
        if error_code:
            delta["exception_report"] = build_exception_report(error_code)
        return delta
