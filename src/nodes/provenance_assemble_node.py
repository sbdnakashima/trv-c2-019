"""AgentCore Platform v1.0 — Inner ④ provenance_assemble (docs/02_design.md §7-3 / §7-6 / §8 / DB-1・DB-4).

The last node in the Inner graph, and the S-3 *deciding* node. ③ has read the
approved aggregate off the ingested dataset and redacted any credential shape it
carried; ④ wraps that value in provenance — the dataset name, version, ingested
date, and the four-dimension query interpretation — so no aggregate ever leaves
without its source (DB-4). It also makes the S-3 output decision (§7-3).

Four obligations shape everything below, each guarding a distinct failure:

*Nothing here raises (§7-6).* A raise inside execute() is caught by
BaseNode.__call__() → status="error" → GraphNode skips merge_output() and the
record is lost. And the framework's @final `_security_gate_output` raises
RuntimeError if any string in the returned delta trips `detect_credentials` — so
the delta ④ returns must already be credential-free, and the S3_BLOCKED decision
is made *here in execute()*, not by letting the gate throw. `blocked` is a value.

*Upstream errors are carried fail-closed.* If ①〜③ set an `error_code`
(DISCLOSURE_RESTRICTED, UNSUPPORTED_QUERY, DATASET_UNAVAILABLE …), ④ builds no
provenance and carries the anomaly through untouched — the report included. A
provenance document assembled for a query the gate rejected is exactly the leak
this template exists to prevent.

*S-3 decision (§7-3, DB-1).* Redaction by replacement always succeeds, so the
`redacted`/`blocked` boundary is defined normatively. ④ re-scans after ③, and
blocks only when the removal reached the *spine* of the provenance:
  - condition ② (prod path, DB-1): a `[REDACTED:` marker sits in the
    dataset_version or the citation — ③ redacted a credential out of the source
    identity itself, so the provenance can no longer stand behind the number;
  - condition ③: the aggregate value itself is gone;
  - condition ① (fail-safe, unreachable from prod invoke, unit-only): a raw
    credential shape still trips `detect_credentials` on the assembled text —
    only if ③'s redaction broke.
A marker that lives only in free-text (a filter note) is `redacted`: the output
is returned. Nothing removed → `clear`.

*Blocked abandons the output as a value, not by omission.* `provenance_output`
and `query_result` are overwritten with `None` — LangGraph's non-reducer
channels are last-write-wins, so omitting the key would leave ③'s `query_result`
in State and leak it to Outer. The exception_report is kept, and it names only
types/fields/counts — never the detected string (quoting it would trip the
@final gate and take the report down with it).
"""

import json
from typing import Any, ClassVar, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.security.credential_detector import detect_credentials
from shared.utils.audit_logger import emit_trace_event

from src.security.credential_redaction import REDACTION_MARKER_PREFIX

# §8 assigns ④ exactly one code of its own; everything else arrived from ⓪〜③.
ERROR_S3_BLOCKED = "S3_BLOCKED"

_NODE_LABEL = "provenance_assemble"

_CLEAR = "clear"
_REDACTED = "redacted"
_BLOCKED = "blocked"

_QUERY_DIMENSIONS = ("period", "region", "segment", "metric")


def build_exception_report(error_code: str, reason: str) -> str:
    """Reviewable Markdown report — the one artifact that must survive (§7-3).

    Credential-free by construction: callers pass only a code and a fixed reason
    string, never a dataset-derived value or a detected credential fragment.
    """
    return (
        f"# 来歴組立て例外レポート\n\n"
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


def _has_marker(text: Any) -> bool:
    return isinstance(text, str) and REDACTION_MARKER_PREFIX in text


def _format_value(value: Any) -> Optional[str]:
    """Render the aggregate for the document; thousands-separated if numeric."""
    if isinstance(value, bool):  # bool is an int subclass — never an aggregate.
        return None
    if isinstance(value, (int, float)):
        return f"{value:,}"
    if isinstance(value, str) and value.strip():
        return value
    return None


class ProvenanceAssembleNode(FunctionNode):
    """Inner ④. Assembles provenance and makes the S-3 output decision (§7-3)."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> dict[str, Any]:
        data = state if isinstance(state, dict) else {}

        # 1. Fail-closed on anything upstream flagged. Build no provenance.
        upstream_error = data.get("error_code")
        if upstream_error:
            return self._carry_upstream(upstream_error, data)

        # 2. Read ③'s result. Garbage in → fail closed, never raise (§7-6).
        result = _parse_object(data.get("query_result"))
        if result is None:
            return self._blocked(data, reason="③ の query_result が読み取り可能な JSON でない（fail-closed）")

        dataset_version = data.get("dataset_version")
        query = _parse_object(data.get("normalized_query")) or {}

        # 3. S-3 decision — re-scan after ③ (§7-3). Order matters: spine breaches
        #    (② version/citation, ③ missing value, ① raw residue) → blocked.
        citation = result.get("citation")
        value_str = _format_value(result.get("value"))

        # condition ③ — removal (or absence) cost us the aggregate value itself.
        if value_str is None:
            return self._blocked(data, reason="集計値が来歴に存在しない（§7-3 条件③）")

        # condition ② — a redaction marker reached the source spine (DB-1 prod path).
        if _has_marker(dataset_version) or _has_marker(citation):
            return self._blocked(data, reason="除去マーカーがデータセット版または引用に及んでいる（§7-3 条件②）")

        # condition ① — fail-safe: raw credential shape still present (③ broke).
        #    Assemble the spine text and re-scan before it can reach the delta.
        spine = "\n".join(str(x) for x in (value_str, result.get("unit"), citation, dataset_version) if x)
        if detect_credentials(spine):
            return self._blocked(data, reason="除去後も credential 形が来歴に残存（§7-3 条件①・fail-safe）")

        # Not on the spine — a marker only in a free-text note is `redacted`.
        clearance = _REDACTED if self._marker_in_free_text(query) else _CLEAR
        return self._success(result, dataset_version, query, value_str, clearance, data)

    # -- outcomes -------------------------------------------------------------

    def _success(
        self,
        result: dict[str, Any],
        dataset_version: Any,
        query: dict[str, Any],
        value_str: str,
        clearance: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        markdown = self._render(result, dataset_version, query, value_str)
        audit = self._audit(error_code=None, clearance=clearance)
        emit_trace_event("provenance_assembled", audit, data)
        # Delta only — never {**state, ...}: node_history / error_log are
        # Annotated[list, operator.add] channels; a spread re-concatenates the
        # whole history every node (§6). query_result is NOT lifted (§5-4).
        return {
            "status": AgentStatus.SUCCESS.value,
            "provenance_output": markdown,
            "s3_clearance": clearance,
            "error_code": None,
            "audit_log": json.dumps(audit, ensure_ascii=False),
        }

    def _carry_upstream(self, error_code: str, data: dict[str, Any]) -> dict[str, Any]:
        audit = self._audit(error_code=error_code, clearance=None)
        emit_trace_event("provenance_assembled", audit, data)
        # Carry the upstream code; do not overwrite its report (it names the node
        # that detected the problem), and build no provenance.
        return {
            "status": AgentStatus.SUCCESS.value,
            "error_code": error_code,
            "provenance_output": None,
            "audit_log": json.dumps(audit, ensure_ascii=False),
        }

    def _blocked(self, data: dict[str, Any], *, reason: str) -> dict[str, Any]:
        # §7-3: overwrite provenance_output AND query_result with None (not omit
        # — last-write-wins channels would keep ③'s query_result and leak it to
        # Outer via get_output). Keep the report.
        audit = self._audit(error_code=ERROR_S3_BLOCKED, clearance=_BLOCKED)
        emit_trace_event("provenance_assembled", audit, data)
        delta: dict[str, Any] = {
            "status": AgentStatus.SUCCESS.value,
            "error_code": ERROR_S3_BLOCKED,
            "s3_clearance": _BLOCKED,
            "provenance_output": None,
            "query_result": None,
            "audit_log": json.dumps(audit, ensure_ascii=False),
        }
        if not data.get("exception_report"):
            delta["exception_report"] = build_exception_report(ERROR_S3_BLOCKED, reason)
        return delta

    def _extra_security_gate_output(self, result: dict[str, Any]) -> dict[str, Any]:
        """Defence-in-depth only (§7-3). The S3_BLOCKED decision was made in
        execute(); this hook just emits an audit event if any residue survived,
        and never raises here (the @final gate above already scanned strings)."""
        for key in ("provenance_output", "query_result"):
            value = result.get(key)
            if isinstance(value, str) and (REDACTION_MARKER_PREFIX in value or detect_credentials(value)):
                emit_trace_event(
                    "s3_residue_detected",
                    {"node": _NODE_LABEL, "field": key},
                    result,
                )
        return result

    # -- rendering ------------------------------------------------------------

    def _render(self, result: dict[str, Any], dataset_version: Any, query: dict[str, Any], value_str: str) -> str:
        # DB-4: never a citation-less aggregate — dataset name/version/ingested
        # date and the four-dimension interpretation always accompany the number.
        unit = result.get("unit")
        citation = result.get("citation")
        headline = f"{value_str} {unit}".strip() if unit else value_str

        lines = ["# 集計結果（来歴付き）", "", f"- **集計値**: {headline}"]
        if citation:
            lines.append(f"- **出典**: {citation}")
        if dataset_version:
            lines.append(f"- **データセット版**: {dataset_version}")

        lines += ["", "## クエリ来歴"]
        labels = {
            "period": "期間",
            "region": "地域",
            "segment": "セグメント",
            "metric": "指標",
        }
        for dim in _QUERY_DIMENSIONS:
            val = query.get(dim)
            if val:
                lines.append(f"- **{labels[dim]}**: {val}")
        filters = query.get("filters")
        lines.append(f"- **フィルター解釈**: {self._describe_filters(filters)}")
        return "\n".join(lines)

    @staticmethod
    def _describe_filters(filters: Any) -> str:
        if not filters:
            return "追加フィルターなし"
        if isinstance(filters, dict):
            return "、".join(f"{k}={v}" for k, v in filters.items()) or "追加フィルターなし"
        return str(filters)

    @staticmethod
    def _marker_in_free_text(query: dict[str, Any]) -> bool:
        """A redaction marker anywhere in the normalized query's free-text
        (used only to distinguish `redacted` from `clear`; spine breaches were
        already ruled out above)."""
        return _has_marker(json.dumps(query, ensure_ascii=False))

    def _audit(self, *, error_code: Optional[str], clearance: Optional[str]) -> dict[str, Any]:
        # S-4 (§7-4): our own field names, the code, and the clearance state
        # only. Never a dataset value, a citation, or an aggregate number.
        return {
            "node": _NODE_LABEL,
            "error_code": error_code,
            "s3_clearance": clearance,
        }
