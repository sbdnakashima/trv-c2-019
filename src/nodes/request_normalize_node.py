"""AgentCore Platform v1.0 — Inner ① request_normalize (docs/02_design.md §7-2 / §8)

Turns the analyst's natural-language request into the one structure ② can
validate deterministically: four dimensions plus a filter object.

    {"period": ..., "region": ..., "segment": ..., "metric": ..., "filters": {}}

The dimension values are `Optional[str]`. `None` means *undetermined* — ② reports
AMBIGUOUS_QUERY for it (§8) — and that is deliberately where a multi-valued or
non-scalar answer lands too: an implicit widening of the query is the failure
mode §8 exists to prevent, so "two regions" must reach ② as "not uniquely
determined" rather than as a list it has no contract for.

Nothing here raises. An exception inside execute() is caught by
BaseNode.__call__() and turned into status="error", which makes
GraphNode.execute() skip merge_output() — the exception report never reaches the
caller (§7-6). Every anomaly is therefore carried as a value: `error_code` +
`exception_report`, with `status = SUCCESS`.
"""

import json
from typing import Any, ClassVar, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.security.credential_detector import detect_credentials
from shared.utils.audit_logger import emit_trace_event

from src.security.credential_redaction import redact_credentials, redact_json_document

# The output contract, in the order ② reads it. Exactly these keys, always all of
# them: a key ① omits reads downstream as "undetermined", and a key it adds is
# dropped by ② with no signal at all.
QUERY_DIMENSIONS = ("period", "region", "segment", "metric")
FILTERS_KEY = "filters"
NORMALIZED_QUERY_KEYS = QUERY_DIMENSIONS + (FILTERS_KEY,)

# The two codes §8 assigns to ①. Anything else already on the state arrived from
# ⓪ and is passed through untouched, report included.
ERROR_CREDENTIAL_IN_REQUEST = "CREDENTIAL_IN_REQUEST"
ERROR_NORMALIZE = "NORMALIZE_ERROR"

# Hook → execute() carrier. The S-2 hook's return value is what __call__() hands
# to execute() (base_node.py:206), but only execute()'s *return delta* reaches a
# LangGraph channel — so this key is a private hand-off, never a State field, and
# execute() must not put it in the delta.
_S2_REDACTED_KEY = "_s2_credentials_redacted"

_DEFAULT_MODEL = "gpt-4o-mini"
_NODE_LABEL = "request_normalize"

_SYSTEM_PROMPT = (
    "あなたは観光統計クエリの正規化器です。"
    "アナリストの自然言語リクエストを、承認済みスキーマに沿って "
    "period（期間）/ region（地域）/ segment（セグメント）/ metric（指標）/ filters（絞り込み条件）"
    "の 5 キーだけを持つ JSON に正規化してください。\n"
    "制約:\n"
    "- 承認済みスキーマに存在しない次元・指標を追加・代替しないこと。\n"
    "- 一意に決まらない次元は推測せず null にすること（複数候補を列挙しない）。\n"
    "- period / region / segment / metric は文字列または null。filters は JSON オブジェクト。\n"
    "- 上記 5 キー以外を含めず、説明文を付けず、JSON のみを返すこと。"
)

_REASONS = {
    ERROR_CREDENTIAL_IN_REQUEST: (
        "依頼本文または承認済みスキーマに認証情報らしき文字列が含まれていたため、正規化を中止しました。"
        "該当箇所は記録前に除去済みです。認証情報を含めずに再送してください。"
    ),
    ERROR_NORMALIZE: (
        "依頼を 4 次元（期間 / 地域 / セグメント / 指標）+ フィルターの形に正規化できませんでした。"
        "対象の期間・地域・セグメント・指標を明示して再送してください。"
    ),
}
_UPSTREAM_REASON = "上流ノードで検出された異常を引き継ぎ、正規化は実施していません。"


def build_exception_report(error_code: str) -> str:
    """Reviewable Markdown report. Quotes no request text and no detected value.

    §7-3: quoting a detected credential would make the report itself trip the
    @final S-3 gate, and the report is the one artifact that must survive.
    """
    reason = _REASONS.get(error_code, _UPSTREAM_REASON)
    return (
        "## 依頼を処理できませんでした\n\n"
        f"- エラーコード: `{error_code}`\n"
        f"- 発生ノード: `{_NODE_LABEL}`\n"
        f"- 理由: {reason}\n"
    )


def _coerce_dimension(value: Any) -> Optional[str]:
    """One dimension value → a single string, or None for "undetermined".

    A bool is checked before int because ``isinstance(True, int)`` is True, and
    a boolean dimension carries no period/region/segment/metric meaning.
    """
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return str(value)
    return None  # list / dict — not uniquely determined, ② reports AMBIGUOUS_QUERY


def normalize_payload(payload: dict[str, Any]) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
    """Map a parsed LLM answer onto the output contract. Never raises.

    Returns ``(query, notes)``. ``query`` is None when the answer breaks the
    contract in a way that could *widen* the request — only `filters` can do
    that, because a dimension has a downstream code for "undetermined" and a
    filter set does not. ``notes`` is audit metadata and is built from this
    module's own constants and counts only: never from model-produced text, which
    the @final S-3 gate would scan once it reached `audit_log` (§7-3).
    """
    filters = payload.get(FILTERS_KEY)
    if filters is None:
        filters = {}
    if not isinstance(filters, dict):
        return None, {"reason": "filters_is_not_an_object"}

    query: dict[str, Any] = {dim: _coerce_dimension(payload.get(dim)) for dim in QUERY_DIMENSIONS}
    query[FILTERS_KEY] = filters
    return query, {
        "undetermined_dimensions": [dim for dim in QUERY_DIMENSIONS if query[dim] is None],
        "unexpected_keys": len(set(payload) - set(NORMALIZED_QUERY_KEYS)),
    }


def _extract_json_object(text: Any) -> Optional[dict[str, Any]]:
    """First JSON object in *text*, or None.

    Slicing from the first ``{`` to the last ``}`` rather than parsing the whole
    string handles the two wrappers Claude actually produces — a ```json fence
    and a one-line preamble — without a second prompt round trip. A response
    with no object at all is a NORMALIZE_ERROR (§8).
    """
    if not isinstance(text, str):
        return None
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except (ValueError, RecursionError):
        # RecursionError, not just ValueError: json.loads() blows the stack rather
        # than reporting on a pathologically nested document. Letting it out of
        # execute() would cost the exception report (§7-6), and this is the only
        # expression in this module that can raise on well-typed input.
        return None
    return parsed if isinstance(parsed, dict) else None


def _parses_as_json(text: str) -> bool:
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


def _build_prompt(request: str, schema: Any) -> str:
    schema_text = schema if isinstance(schema, str) and schema.strip() else "{}"
    return (
        "# 承認済みスキーマ（この範囲を超えないこと）\n"
        f"{schema_text}\n\n"
        "# アナリストの依頼\n"
        f"{request}\n\n"
        "# 出力\n"
        f"{', '.join(NORMALIZED_QUERY_KEYS)} の 5 キーを持つ JSON のみ"
    )


class RequestNormalizeNode(FunctionNode):
    """Inner ① — natural language → 4 dimensions + filters, via one LLM call.

    Security: S-1 (trust level below) / **S-2** / S-3 removal / S-4.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, llm: Any = None, model: str = _DEFAULT_MODEL) -> None:
        """*llm* is injected by tests; production leaves it None and builds an
        AzureOpenAIClient per invocation from the secret provider. The API key is
        never stored on the instance or in State (§12)."""
        self._llm = llm
        self._model = model

    # ------------------------------------------------------------------ S-2

    def _extra_security_gate_input(self, state: AgentState) -> AgentState:
        """Remove credentials from `analyst_request` / `approved_schema`.

        The framework's own @final input gate PII-masks `_PII_SCAN_FIELDS`
        (`user_input` / `validated_input` / `llm_response`) only, so this
        template's domain fields are this hook's responsibility (§7-2).

        Never raises and never sets status = ERROR: `_security_gate_input()` is
        @final and __call__() short-circuits — skipping execute() — the moment
        the returned state carries an error status (base_node.py:207-220).
        execute() is the only place the exception report is built, so both would
        destroy the artifact this template exists to produce (§7-2 raise しない).

        Defence-in-depth: unreachable on the production invoke path, because ⓪
        already redacts both fields and fails closed when its own post-removal
        re-scan still finds something (§7-3 適用ノード). It is kept because that
        guarantee lives in code that can regress, and because §8 assigns
        CREDENTIAL_IN_REQUEST to ① — the node that owns the code owns the check.
        """
        updated: dict[str, Any] = dict(state)

        request, removed_request = redact_credentials(updated.get("analyst_request"))
        # Leaf-wise for the schema (§7-3): a flat replacement would swallow the
        # closing quote and the following key, handing ② an unparseable schema.
        schema, removed_schema = redact_json_document(updated.get("approved_schema"))
        removed = removed_request + removed_schema

        if removed:
            updated["analyst_request"] = request
            updated["approved_schema"] = schema
            # First error wins: an upstream code and its report must survive the
            # fail-closed pass-through (§7-6).
            if not updated.get("error_code"):
                updated["error_code"] = ERROR_CREDENTIAL_IN_REQUEST
        updated[_S2_REDACTED_KEY] = removed
        return updated

    # -------------------------------------------------------------- execute

    def execute(self, state: AgentState) -> dict[str, Any]:
        # A plain dict: `state` also carries the private hook carrier, which is
        # not a declared State key.
        data: dict[str, Any] = dict(state)
        error_code: Optional[str] = data.get("error_code")
        request = data.get("analyst_request")
        schema = data.get("approved_schema")
        # Typed rather than int()-coerced: this module's contract is that nothing
        # here raises, and int("x") is the one place a stray value could.
        carrier = data.get(_S2_REDACTED_KEY)
        redacted_in = carrier if isinstance(carrier, int) else 0

        notes: dict[str, Any] = {}
        query: Optional[dict[str, Any]] = None

        if not error_code and not (isinstance(request, str) and request.strip()):
            # Unreachable by contract — ⓪ nulls both fields whenever it sets an
            # error_code, so a blank request with no code means ⓪ regressed.
            # Fail closed rather than send an empty prompt to the model.
            error_code = ERROR_NORMALIZE

        if not error_code:
            raw, llm_error = self._complete(state, request, schema)
            if llm_error:
                notes["llm_error"] = llm_error
            payload = _extract_json_object(raw)
            if payload is None:
                error_code = ERROR_NORMALIZE
            else:
                query, mapping_notes = normalize_payload(payload)
                notes.update(mapping_notes)
                if query is None:
                    error_code = ERROR_NORMALIZE

        normalized_query: Optional[str] = None
        redacted_out = 0
        if query is not None:
            normalized_query, redacted_out = redact_json_document(json.dumps(query, ensure_ascii=False))
            # The mirror of ⓪'s check, on the two properties that actually
            # matter downstream: the @final S-3 gate tests the *serialized*
            # string for cleanliness, and ② parses it. Reporting NORMALIZE_ERROR
            # here keeps §7-6's exception report; letting the gate raise loses it
            # (RuntimeError → status="error" → GraphNode skips merge_output()).
            if (
                not isinstance(normalized_query, str)
                or detect_credentials(normalized_query)
                or not _parses_as_json(normalized_query)
            ):
                error_code = ERROR_NORMALIZE
                normalized_query = None

        if redacted_in:
            # ⓪ re-scans its own post-removal output before writing it to a
            # channel (input_expand_node.py §7-3 mirror); the two fields §7-2 ④
            # makes *this* node rewrite need the same check, and for a reason that
            # is reachable: `_redact_leaf` judges each leaf before JSON escaping,
            # so `mysql://a\nbcdefghij` is clean as a leaf — the newline ends
            # `[^\s]{8,}` — and matches `conn_string` once json.dumps() turns that
            # newline into the two non-space characters `\n`.
            #
            # None, not the dirty text: writing it would make the @final S-3 gate
            # raise (RuntimeError → status="error" → GraphNode skips
            # merge_output()) and take the exception report with it. None still
            # overwrites ⓪'s credential-bearing copy in the channel, which is the
            # whole point of §7-2 ④, and the field is unusable to ② either way
            # because an error_code is already set.
            unredactable = sorted(
                name
                for name, value in (("analyst_request", request), ("approved_schema", schema))
                if isinstance(value, str) and detect_credentials(value)
            )
            if unredactable:
                notes["unredactable_fields"] = unredactable
                request = None if "analyst_request" in unredactable else request
                schema = None if "approved_schema" in unredactable else schema

        # S-4 (§7-4). Counts, our own field names, and the exception *type* only —
        # never a provider error message (it can echo request headers) and never
        # model-produced text. node_start / node_complete / node_error are
        # BaseNode's job, not ours.
        payload_audit: dict[str, Any] = {
            "node": _NODE_LABEL,
            "model": self._model,
            "error_code": error_code,
            "credentials_redacted": redacted_in + redacted_out,
            **notes,
        }
        emit_trace_event("request_normalized", payload_audit, state)

        # Delta only — never {**state, ...}: node_history and error_log are
        # Annotated[list[str], operator.add] channels and a spread re-concatenates
        # the whole history on every node (§6).
        delta: dict[str, Any] = {
            "status": AgentStatus.SUCCESS.value,
            "normalized_query": normalized_query,
            "error_code": error_code,
            "audit_log": json.dumps(payload_audit, ensure_ascii=False),
        }
        if redacted_in:
            # §7-2 ④: the redacted fields must reach the channel, or the original
            # credential-bearing text written by ⓪ stays in State — the
            # checkpoint DB — and in Inner get_output(). Only the fields this
            # node actually changed are rewritten.
            delta["analyst_request"] = request
            delta["approved_schema"] = schema
        if error_code and not data.get("exception_report"):
            # Never overwrite an upstream report: it names the node that detected
            # the problem, and rewriting it would misattribute the failure.
            delta["exception_report"] = build_exception_report(error_code)
        return delta

    # ------------------------------------------------------------------ LLM

    def _complete(self, state: AgentState, request: Any, schema: Any) -> tuple[Optional[str], Optional[str]]:
        """Return ``(raw_text, failure_type_name)``. Never raises.

        Every failure — missing secret, transport error, provider error,
        unexpected response shape — collapses to a None text, which execute()
        turns into NORMALIZE_ERROR (§8). Only the exception's *type name* is
        reported: a provider message can quote the request, including headers.

        The raw text stays a local variable and never reaches a State channel
        (§6): it is free prose containing Title Case region and segment names,
        which a later node's @final S-2 PII scan would replace with [MASKED].
        """
        try:
            llm = self._llm if self._llm is not None else self._build_client(state)
            raw = llm.complete(
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": _build_prompt(request, schema)},
                ]
            )
        except Exception as exc:  # noqa: BLE001 — see the docstring
            return None, type(exc).__name__
        if isinstance(raw, str):
            return raw, None
        if isinstance(raw, dict) and isinstance(raw.get("content"), str):
            return raw["content"], None
        return None, "UnexpectedResponseShape"

    def _build_client(self, state: AgentState) -> Any:
        ctx = InvocationContext.from_state(state)
        # Imported here rather than at module scope: AzureOpenAIClient pulls in
        # langchain_openai, a runtime-image dependency (pyproject
        # `dependencies = []`). A module-scope import would make every unit test
        # in this file fail at collection.
        from shared.services.llm.azure_openai_client import AzureOpenAIClient

        return AzureOpenAIClient(
            config={
                "api_key": ctx.secrets.require("AZURE_OPENAI_API_KEY"),
                "azure_endpoint": ctx.secrets.require("AZURE_OPENAI_ENDPOINT"),
                "azure_deployment": ctx.secrets.require("AZURE_OPENAI_DEPLOYMENT"),
            }
        )
