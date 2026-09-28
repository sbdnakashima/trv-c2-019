"""AgentCore Platform v1.0 — Inner ③ readonly_query (docs/02_design.md §8 / §7-3 / §4-3).

The one node that touches data. ① proposes, ② approves, and only here does an
approved query actually reach the ingested dataset — read-only, no live API
(§1-2). It lifts the aggregate value and its dataset version onto the state so ④
can wrap them in provenance.

Three obligations shape everything below, each guarding a distinct failure:

*It must not query on behalf of a request the gate rejected.* An upstream
`error_code` — or a `validation_result` that is not `valid` — means the dataset
is never touched; the anomaly is carried through untouched, report included. A
query that runs when ② said no is exactly the boundary this template exists to
hold.

*Dataset strings are redacted before they reach the delta (§7-3, "適用ノード").*
The approved dataset is trusted, but "trusted" is a claim about intent, not
content: a citation or version string could still carry a credential shape. So
every dataset-derived string is run through the shared redactor first, leaving
`[REDACTED:<type>]` markers that ④'s S-3 gate can see. ③ is the node whose
redaction makes ④'s condition-① re-scan provably empty (§7-3).

*Nothing here raises.* Dataset unavailability is a value (`DATASET_UNAVAILABLE`),
not an exception — the design's §4-3 has `VisitorStatsDataset` return
`available=False` rather than throw, and even if it threw, a raise inside
execute() is caught by BaseNode.__call__() → status="error" → GraphNode skips
merge_output() and the record is lost (§7-6). So the dataset call is guarded and
every anomaly travels as `error_code` + `exception_report`, status = SUCCESS.
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

# §8 assigns ③ exactly one code of its own. Anything else on the state arrived
# from ⓪〜② and is carried through untouched.
ERROR_DATASET_UNAVAILABLE = "DATASET_UNAVAILABLE"

_NODE_LABEL = "readonly_query"


def build_exception_report(error_code: str, reason: str) -> str:
    """Reviewable Markdown report — the one artifact that must survive (§7-3).

    Kept credential-free by construction: callers pass only a code and a fixed
    reason string, never a dataset-derived value.
    """
    return (
        f"# 読み取り専用クエリ例外レポート\n\n"
        f"- **error_code**: `{error_code}`\n"
        f"- **検出ノード**: `{_NODE_LABEL}`\n"
        f"- **理由**: {reason}\n"
    )


def _parse_object(raw: Optional[str]) -> Optional[dict[str, Any]]:
    """Parse a JSON object string; anything that is not an object → None."""
    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _redact(value: Optional[str]) -> tuple[Optional[str], int]:
    """Redact a dataset-derived string, returning (text, redaction_count)."""
    return redact_credentials(value)


_LEAF_WITHHELD = "[WITHHELD:credential detected in serialisation]"


def _redact_document(obj: dict[str, Any]) -> tuple[str, int]:
    """Serialise *obj* to credential-free, always-parseable JSON, returning (json, count).

    Two passes, because neither alone closes both failure modes:

    * :func:`redact_json_document` redacts *every* string leaf (not a hand-listed
      subset) judged in serialisation context, and keeps the JSON parseable so ④
      can re-scan it (§7-3 condition ③). It covers `unit` — or any leaf — not only
      `citation`. But it judges each leaf's *own* text plus the closing
      quotes/braces/comma; it cannot see a `conn_string` that only re-forms because
      an *interior* character escapes — the Issue #4 shape, where a leaf's real
      newline becomes the two non-space chars ``\\n`` in the serialised form and
      splices `mysql://ab` into a full run.

    * A flat :func:`detect_credentials` on the assembled blob *does* see that
      re-form. So after the leaf pass, the blob is re-scanned; if a credential
      still shows, it is a serialisation-boundary re-form living inside some
      string leaf, and every such leaf is replaced wholesale with a marker and the
      object re-serialised — credential-free *and* still parseable (the marker has
      no `conn_string` run to swallow a structural quote).
    """
    redacted, leaf_count = redact_json_document(json.dumps(obj, ensure_ascii=False))
    # `redact_json_document` returns Optional only to pass None/empty input
    # through; the input here is always a non-empty object serialisation, so the
    # result is a non-empty str. Fall back defensively rather than assume.
    leaf_pass = redacted if isinstance(redacted, str) else "{}"
    if not detect_credentials(leaf_pass):
        return leaf_pass, leaf_count

    # Survived only in the serialised form (interior-escape re-form). Withhold each
    # string leaf wholesale so no greedy run can reach a structural quote. The
    # document ③ builds is flat (§5-4), so a single top-level pass covers it.
    parsed = json.loads(leaf_pass)
    withheld = 0
    for key in list(parsed.keys()):
        if isinstance(parsed[key], str):
            parsed[key] = _LEAF_WITHHELD
            withheld += 1
    return json.dumps(parsed, ensure_ascii=False), leaf_count + withheld


class ReadonlyQueryNode(FunctionNode):
    """Inner ③. Runs the approved query against the ingested dataset, read-only.

    The dataset is injected (§4-3): the node never constructs `VisitorStatsDataset`
    itself, so tests inject a Fake and DB-5 / PB-3 stay verifiable.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, dataset: Any = None) -> None:
        # `dataset` defaults to None so the node is constructible with no
        # arguments, exactly as ① request_normalize takes `llm=None` — PB-6
        # discovers every node and instantiates it argument-free
        # (test_pb_invoke_order.py). The real dataset is injected in production
        # via the 4-hop wiring (§4-3) and a Fake in tests; if neither happened,
        # `_resolve_dataset()` builds the default one lazily, which still returns
        # `available=False` on a missing root rather than raising.
        super().__init__()
        self._dataset = dataset

    def _resolve_dataset(self) -> Any:
        """The injected dataset, or a lazily-built default (§4-3 fail-safe)."""
        if self._dataset is not None:
            return self._dataset
        from src.services.visitor_stats_dataset import (
            DEFAULT_DATASET_ROOT,
            VisitorStatsDataset,
        )

        self._dataset = VisitorStatsDataset(DEFAULT_DATASET_ROOT)
        return self._dataset

    def execute(self, state: AgentState) -> dict[str, Any]:
        data = state if isinstance(state, dict) else {}

        # 1. Fail-closed on anything upstream flagged. Do not touch the dataset.
        upstream_error = data.get("error_code")
        if upstream_error:
            return self._carry_upstream(upstream_error, data)

        # 2. The gate (②) must have said valid. A missing/invalid verdict, or a
        #    valid verdict with no query behind it, is an internal inconsistency
        #    — fail closed rather than query on a guess.
        verdict = _parse_object(data.get("validation_result"))
        query = _parse_object(data.get("normalized_query"))
        if verdict is None or verdict.get("valid") is not True or query is None:
            return self._error(
                ERROR_DATASET_UNAVAILABLE
                if verdict is not None and verdict.get("valid") is True
                else "UNSUPPORTED_QUERY",
                "検証済みクエリが読み取り可能な形で存在しない（fail-closed）",
                data,
            )

        # 3. Query the dataset. Guarded: a raise here would cost the record (§7-6).
        try:
            result = self._resolve_dataset().query(query)
        except Exception:  # noqa: BLE001 — §7-6: no anomaly may escape as a raise.
            return self._error(
                ERROR_DATASET_UNAVAILABLE,
                "データセットクエリが例外を送出した",
                data,
            )

        if not isinstance(result, dict) or not result.get("available"):
            return self._error(
                ERROR_DATASET_UNAVAILABLE,
                "承認済みデータセットが未取り込み / 参照不能",
                data,
            )
        if not result.get("matched"):
            return self._error(
                ERROR_DATASET_UNAVAILABLE,
                "承認済みデータセットに該当する集計行がない",
                data,
            )

        return self._success(result, data)

    # -- outcomes -------------------------------------------------------------

    def _success(self, result: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
        # §7-3: every dataset-derived string is redacted before it reaches the
        # delta. The aggregate value is numeric; the citation and version are the
        # free-text carriers a credential could ride in on.
        citation, r1 = _redact(_as_str(result.get("citation")))
        version_raw = _compose_version(result.get("dataset_version"), result.get("ingested_date"))
        version, r2 = _redact(version_raw)

        # `value`/`unit` are non-string (a number and a short fixed unit); only
        # `citation` is a free-text carrier, and it is already leaf-redacted above.
        query_result, r3 = _redact_document(
            {"value": result.get("value"), "unit": result.get("unit"), "citation": citation}
        )

        redactions = r1 + r2 + r3
        audit = self._audit(error_code=None, redactions=redactions, matched=True)
        emit_trace_event("readonly_queried", audit, data)

        # Delta only — never {**state, ...}: node_history / error_log are
        # Annotated[list, operator.add] channels; a spread re-concatenates the
        # whole history every node (§6).
        return {
            "status": AgentStatus.SUCCESS.value,
            "query_result": query_result,
            "dataset_version": version,
            "error_code": None,
            "audit_log": json.dumps(audit, ensure_ascii=False),
        }

    def _carry_upstream(self, error_code: str, data: dict[str, Any]) -> dict[str, Any]:
        audit = self._audit(error_code=error_code, redactions=0, matched=False)
        emit_trace_event("readonly_queried", audit, data)
        # Carry the upstream code; do not touch query_result and do not overwrite
        # the upstream report (it names the node that detected the problem).
        return {
            "status": AgentStatus.SUCCESS.value,
            "error_code": error_code,
            "audit_log": json.dumps(audit, ensure_ascii=False),
        }

    def _error(self, error_code: str, reason: str, data: dict[str, Any]) -> dict[str, Any]:
        audit = self._audit(error_code=error_code, redactions=0, matched=False)
        emit_trace_event("readonly_queried", audit, data)
        delta: dict[str, Any] = {
            "status": AgentStatus.SUCCESS.value,
            "error_code": error_code,
            "audit_log": json.dumps(audit, ensure_ascii=False),
        }
        # Only write a report if no upstream node already did — never misattribute.
        if not data.get("exception_report"):
            delta["exception_report"] = build_exception_report(error_code, reason)
        return delta

    def _audit(self, *, error_code: Optional[str], redactions: int, matched: bool) -> dict[str, Any]:
        # S-4 (§7-4): our own field names, counts, and the error code only. Never
        # a dataset value, a citation, or an aggregate number.
        return {
            "node": _NODE_LABEL,
            "error_code": error_code,
            "credentials_redacted": redactions,
            "matched": matched,
        }


def _as_str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else (None if value is None else str(value))


def _compose_version(version: Any, ingested: Any) -> Optional[str]:
    """`<version>（取り込み: <date>）`, or whichever half exists."""
    v = _as_str(version)
    d = _as_str(ingested)
    if v and d:
        return f"{v}（取り込み: {d}）"
    return v or d
