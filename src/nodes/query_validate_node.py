"""AgentCore Platform v1.0 — Inner ② query_validate (docs/02_design.md §7-6 / §8)

The deterministic boundary between what the analyst asked for and what the
organisation approved. ① is an LLM and can propose anything; nothing it proposes
reaches the dataset until this node has matched it, exactly, against the approved
schema (§5 "ハルシネーション対策").

`approved_schema` is opaque in the design — it is whatever the calling system
declares — so its shape is defined here, and this module is its only reader:

    {
      "dimensions":           ["period", "region", "segment", "metric"],
      "allowed_metrics":      ["延べ宿泊者数", "訪問者数"],
      "allowed_filters":      ["purpose"],          # optional
      "disclosure_restricted": ["個人識別", "市区町村"]  # optional
    }

Two rules govern how it is read, and both exist because the failure mode worth
preventing is a query that runs when it should not have:

*Nothing is approved unless the schema approves it.* An absent, empty or
wrongly-typed `dimensions` / `allowed_metrics` approves nothing at all rather
than everything, and an absent `allowed_filters` approves no filter. The two
optional keys are optional in opposite directions on purpose: `allowed_filters`
is an allowlist, so its absence must deny; `disclosure_restricted` is a denylist
layered *on top* of allowlists that already bound the query, so its absence
simply adds no further restriction.

*Matching is exact.* No substring, no case folding, no trimming. Absorbing
spelling variation would silently approve a value the schema never listed, which
is the same thing as having no allowlist.

Nothing here raises. An exception inside execute() is caught by
BaseNode.__call__() and turned into status="error", which makes
GraphNode.execute() skip merge_output() — the exception report never reaches the
caller (§7-6). Every anomaly is therefore carried as a value: `error_code` +
`exception_report`, with `status = SUCCESS`.
"""

import json
from typing import Any, ClassVar, Iterator, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.security.credential_detector import detect_credentials
from shared.utils.audit_logger import emit_trace_event

from src.nodes.request_normalize_node import (
    FILTERS_KEY,
    NORMALIZED_QUERY_KEYS,
    QUERY_DIMENSIONS,
)

# The three codes §8 assigns to ②. Anything else on the state arrived from ⓪ or
# ① and is carried through untouched, report included.
ERROR_AMBIGUOUS = "AMBIGUOUS_QUERY"
ERROR_UNSUPPORTED = "UNSUPPORTED_QUERY"
ERROR_DISCLOSURE_RESTRICTED = "DISCLOSURE_RESTRICTED"

# Schema keys. `_SCHEMA_DIMENSIONS` / `_SCHEMA_METRICS` are required allowlists;
# the query is unsupported without them.
_SCHEMA_DIMENSIONS = "dimensions"
_SCHEMA_METRICS = "allowed_metrics"
_SCHEMA_FILTERS = "allowed_filters"
_SCHEMA_RESTRICTED = "disclosure_restricted"

_METRIC = "metric"
_NODE_LABEL = "query_validate"

_UPSTREAM_REASON = "upstream_error"

# Used when a reason built from the query would itself be unsafe to record, and
# for a code this node does not own.
_REASON_WITHHELD = "詳細は監査ログを参照してください（値の引用は安全上の理由で省略しました）。"
_UPSTREAM_REPORT_REASON = "上流ノードで検出された異常を引き継ぎ、検証は実施していません。"


def build_exception_report(error_code: str, reason: str) -> str:
    """Reviewable Markdown report.

    §7-3: the report is the one artifact that must survive, so it must not
    contain anything the @final S-3 output gate would reject. execute() checks
    the assembled text and falls back to a value-free reason if it does.
    """
    return (
        "## 依頼を処理できませんでした\n\n"
        f"- エラーコード: `{error_code}`\n"
        f"- 発生ノード: `{_NODE_LABEL}`\n"
        f"- 理由: {reason or _UPSTREAM_REPORT_REASON}\n"
    )


def _allowlist(schema: dict[str, Any], key: str) -> Optional[frozenset[str]]:
    """The approved values under *key*, or None when the schema approves none.

    A non-list, an empty list, or a list holding a non-string all mean "this
    schema does not carry a usable allowlist" rather than "no restriction": a
    malformed allowlist must not read as an open door. Non-string entries make
    the whole list unusable rather than being skipped, because skipping would
    let a partly-corrupt schema keep approving its surviving entries.
    """
    values = schema.get(key)
    if not isinstance(values, list) or not values:
        return None
    if not all(isinstance(value, str) for value in values):
        return None
    return frozenset(values)


def _denylist(schema: dict[str, Any], key: str) -> Optional[frozenset[str]]:
    """The restricted values under *key*: empty when absent, None when unusable.

    Absent restricts nothing. *Present but malformed* is a broken schema, and it
    fails closed like an allowlist does — a non-list, or a list holding a
    non-string, yields None.

    The docstring rule "a denylist only layers on top of allowlists that already
    bound the query" does not license failing open here: the allowlists bound the
    four dimension *names* and the *metric* value, so a period / region / segment
    value is bounded by nothing but this list. Writing `"個人識別"` where a
    one-element list belongs would otherwise turn the whole disclosure policy off
    and still validate, which is exactly the suppression DB-2 forbids.
    """
    values = schema.get(key)
    if values is None:
        return frozenset()
    if not isinstance(values, list):
        return None
    if not all(isinstance(value, str) for value in values):
        return None
    return frozenset(values)


def _string_leaves(value: Any) -> Iterator[str]:
    """Every string reachable in *value*, including dict keys.

    A filter is free-form, so a restricted term can sit at any depth and on
    either side of a mapping. Walking only the top level would leave the
    denylist enforced at the front door and open at the back.
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from _string_leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from _string_leaves(item)


def _determined(value: Any) -> Optional[str]:
    """The dimension's value, or None when ① could not determine it.

    Blank counts as undetermined: ① writes None for that, but a whitespace-only
    string would otherwise be matched against the allowlist and reported as an
    unsupported value, which misnames the problem.
    """
    if isinstance(value, str) and value.strip():
        return value
    return None


class QueryValidateNode(FunctionNode):
    """Match the normalized query against the approved schema. Deterministic."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> dict[str, Any]:
        data: dict[str, Any] = dict(state)
        upstream_code: Optional[str] = data.get("error_code")

        if upstream_code:
            # Fail closed (§7-6): a query that never reached this node's contract
            # is not validated here, and is not re-diagnosed either — the
            # upstream report names the node that actually detected the problem.
            return self._delta(
                state,
                error_code=upstream_code,
                reason=_UPSTREAM_REASON,
                report_reason="",
                write_report=not data.get("exception_report"),
                audit={"skipped": "upstream_error"},
            )

        query = _parse_object(data.get("normalized_query"))
        if query is None:
            # ① nulls normalized_query only when it also sets an error_code, so
            # reaching here means ① regressed. Every dimension is undetermined,
            # which is exactly what AMBIGUOUS_QUERY means (§8).
            return self._reject(
                state,
                ERROR_AMBIGUOUS,
                "正規化クエリを取得できませんでした。",
                {"missing": "normalized_query"},
            )

        schema = _parse_object(data.get("approved_schema")) or {}
        approved_dimensions = _allowlist(schema, _SCHEMA_DIMENSIONS)
        approved_metrics = _allowlist(schema, _SCHEMA_METRICS)
        restricted = _denylist(schema, _SCHEMA_RESTRICTED)
        if approved_dimensions is None or approved_metrics is None or restricted is None:
            return self._reject(
                state,
                ERROR_UNSUPPORTED,
                "承認済みクエリスキーマが取得できないか、承認対象の次元・指標・開示制限の宣言が不正です。",
                {"missing": "approved_schema"},
            )

        # Disclosure first, and over the whole request at once. Checked per
        # dimension in query order it would be suppressible: an unrelated
        # undetermined dimension or unapproved metric would be reported instead,
        # and DB-2 requires the restriction to be neither suppressible nor
        # evadable. Filters are included because a restricted term is otherwise
        # smuggled past the dimension allowlists by moving it into a filter.
        #
        # The sweep covers every leaf of the document, not the five keys this
        # module happens to know about. Keying it on NORMALIZED_QUERY_KEYS would
        # leave a term parked under any other key invisible to it — and §8's
        # data-access contract hands ③ the whole dict, so an unswept key travels
        # into the dataset query intact. Unknown key *names* are swept too, since
        # they are analyst-controlled; the five structural names are not, so that
        # restricting a term never accidentally depends on our own vocabulary.
        if restricted:
            scanned: list[Any] = list(query.values())
            scanned += [key for key in query if key not in NORMALIZED_QUERY_KEYS]
            hits = sorted({leaf for value in scanned for leaf in _string_leaves(value)} & restricted)
            if hits:
                return self._reject(
                    state,
                    ERROR_DISCLOSURE_RESTRICTED,
                    f"開示ポリシーにより指定できない値が含まれています: {'、'.join(hits)}",
                    {"restricted_hits": len(hits)},
                )

        # A key outside the normalized contract is a defect in its own right,
        # separate from what it holds: ② approves a query it has inspected, and
        # it cannot inspect a field it has no rule for. Since ② never rewrites
        # normalized_query (DB-3), waving one through would hand ③ a field that
        # nothing approved.
        unknown = sorted(key for key in query if key not in NORMALIZED_QUERY_KEYS)
        if unknown:
            return self._reject(
                state,
                ERROR_UNSUPPORTED,
                f"正規化クエリに未知の項目が含まれています: {'、'.join(unknown)}",
                {"unknown_keys": len(unknown)},
            )

        undetermined = [dim for dim in QUERY_DIMENSIONS if _determined(query.get(dim)) is None]
        if undetermined:
            return self._reject(
                state,
                ERROR_AMBIGUOUS,
                f"一意に決まらない次元があります: {'、'.join(undetermined)}",
                {"undetermined_dimensions": undetermined},
            )

        unapproved_dimensions = [dim for dim in QUERY_DIMENSIONS if dim not in approved_dimensions]
        if unapproved_dimensions:
            return self._reject(
                state,
                ERROR_UNSUPPORTED,
                f"承認済みスキーマに存在しない次元です: {'、'.join(unapproved_dimensions)}",
                {"unapproved_dimensions": unapproved_dimensions},
            )

        metric = query.get(_METRIC)
        if metric not in approved_metrics:
            return self._reject(
                state,
                ERROR_UNSUPPORTED,
                f"承認済みスキーマに存在しない指標です: {metric}",
                {"unapproved": _METRIC},
            )

        filters = query.get(FILTERS_KEY)
        if not isinstance(filters, dict):
            # Not coerced to {} and waved through. ② never rewrites
            # normalized_query (DB-3), so ③ would read the very filter set this
            # node had decided not to look at — the widening ② exists to stop.
            # ① rejects a non-object `filters` before ever reaching here, which
            # is exactly why this is worth stating: the node's contract is that
            # it does not trust ①.
            return self._reject(
                state,
                ERROR_UNSUPPORTED,
                "フィルターの形式が不正です。",
                {"malformed": FILTERS_KEY},
            )
        if filters:
            # None, not frozenset(): a schema that declares no filter allowlist
            # approves no filter. Defaulting to "unrestricted" here would make
            # the one field that can widen the query the one field nothing
            # bounds.
            approved_filters = _allowlist(schema, _SCHEMA_FILTERS)
            unapproved_filters = sorted(
                key for key in filters if approved_filters is None or key not in approved_filters
            )
            if unapproved_filters:
                return self._reject(
                    state,
                    ERROR_UNSUPPORTED,
                    f"承認済みスキーマに存在しないフィルターです: {'、'.join(unapproved_filters)}",
                    {"unapproved_filters": len(unapproved_filters)},
                )

        return self._delta(
            state,
            error_code=None,
            reason="ok",
            report_reason="",
            write_report=False,
            audit={"valid": True},
        )

    # -------------------------------------------------------------- helpers

    def _reject(self, state: AgentState, error_code: str, reason: str, audit: dict[str, Any]) -> dict[str, Any]:
        return self._delta(
            state,
            error_code=error_code,
            reason=reason,
            report_reason=reason,
            write_report=True,
            audit={"valid": False, **audit},
        )

    def _delta(
        self,
        state: AgentState,
        *,
        error_code: Optional[str],
        reason: str,
        report_reason: str,
        write_report: bool,
        audit: dict[str, Any],
    ) -> dict[str, Any]:
        # The reason quotes values that came from the analyst by way of ①. ①
        # guarantees a credential-free normalized_query, so this is the fail-safe
        # for ① regressing.
        #
        # Judged on the *assembled* artifacts, never on the values that went into
        # them. A value can be clean on its own and dirty once written down:
        # json.dumps() renders a real newline as the two non-space characters
        # \n, which closes a `mysql://[^\s]{8,}` run that the newline had broken
        # (the Issue #4 root cause, same shape here). The @final S-3 gate scans
        # the assembled strings, so this is where the judgement has to be made —
        # and it has to be made here, because the gate's RuntimeError costs the
        # exception report entirely (§7-6: status="error" → GraphNode skips
        # merge_output()). Withholding the reason keeps the report.
        # Both artifacts are judged, because neither one's verdict implies the
        # other's — escaping moves the answer in *both* directions:
        #
        #   json.dumps() renders a real newline as the two non-space characters
        #   \n, closing a `mysql://[^\s]{8,}` run that the newline had broken:
        #   clean raw, dirty serialized (the Issue #4 root cause).
        #
        #   The same rewrite *removes* whitespace, and `Bearer\s+[\w.\-]{16,}`
        #   is the one pattern that requires whitespace: dirty raw, clean
        #   serialized.
        #
        # So the report has to be assembled before the judgement, not after.
        #
        # The delta's other two strings, error_code and audit_log, are scanned by
        # the gate as well, and withholding structurally cannot rescue either:
        # error_code is a contract value ③/④ and merge_output() depend on, and
        # audit_log embeds it. They are safe by an invariant instead — every
        # error code is a module constant, and every audit payload built here
        # carries field names, QUERY_DIMENSIONS entries and integer counts, never
        # an analyst value. That invariant is what keeps them out of this check,
        # so it is asserted in the tests rather than left to inspection.
        validation_result = _json({"valid": error_code is None, "reason": reason})
        report = build_exception_report(error_code or "", report_reason) if write_report else None

        withheld = bool(detect_credentials(validation_result)) or bool(
            report is not None and detect_credentials(report)
        )
        if withheld:
            reason = report_reason = _REASON_WITHHELD
            validation_result = _json({"valid": error_code is None, "reason": reason})
            report = build_exception_report(error_code or "", report_reason) if write_report else None

        # S-4 (§7-4). Our own field names and counts only: the audit payload is a
        # State channel too, and analyst values belong in the reviewable report,
        # not in the audit trail.
        payload_audit: dict[str, Any] = {
            "node": _NODE_LABEL,
            "error_code": error_code,
            **audit,
        }
        if withheld:
            payload_audit["reason_withheld"] = True
        emit_trace_event("query_validated", payload_audit, state)

        # Delta only — never {**state, ...}: node_history and error_log are
        # Annotated[list[str], operator.add] channels and a spread re-concatenates
        # the whole history on every node (§6). normalized_query is deliberately
        # absent: this node validates the query, it never rewrites it (DB-3).
        delta: dict[str, Any] = {
            "status": AgentStatus.SUCCESS.value,
            "validation_result": validation_result,
            "error_code": error_code,
            "audit_log": _json(payload_audit),
        }
        if report is not None:
            delta["exception_report"] = report
        return delta


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _parse_object(raw: Any) -> Optional[dict[str, Any]]:
    """*raw* as a JSON object, or None.

    None rather than an exception, and None rather than `{}`: an empty object is
    a schema that approves nothing, which is the same outcome by a different
    route, but the two must not be conflated when reporting which one happened.
    """
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, RecursionError):
        # RecursionError, not just ValueError: json.loads() blows the stack
        # rather than reporting on a pathologically nested document, and letting
        # it out of execute() would cost the exception report (§7-6).
        return None
    return parsed if isinstance(parsed, dict) else None
