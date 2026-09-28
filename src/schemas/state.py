"""AgentCore Platform v1.0"""

# ADR-005: State must be a flat TypedDict (see ADR-005 for the prohibited
# alternatives). LangGraph checkpoints use msgpack serialization, so only
# plain serializable fields are allowed. Do NOT add credentials or secrets.

from typing import Optional

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Destination Visitor-Statistics Query Agent state (docs/02_design.md §6).

    Outer (AgentBaseGraph) and Inner (BaseGraph) share this single class —
    do not define a separate Inner state.

    Inherited from AgentState: user_input, status, formatted_output,
    node_history, error_log, session_id, trace_id, correlation_id.

    Every domain field is Optional[str]; list/dict payloads are carried as JSON
    strings because msgpack cannot serialize them as native containers.

    Never used (docs/02_design.md §6 State 制約):
        validated_input / llm_response — the framework's @final S-2 gate scans
        _PII_SCAN_FIELDS = ("user_input", "validated_input", "llm_response")
        and masks PII in them. Region and segment names are Title Case, so a
        raw LLM response placed in either channel would be destroyed. Only the
        parsed result (normalized_query) reaches the state; the raw response
        stays a local variable inside execute().
        Do NOT rename normalized_query to validated_input or llm_response.
    """

    # ⓪ input_expand — restored from the packed intake payload (§5-2)
    analyst_request: Optional[str]
    approved_schema: Optional[str]  # JSON string

    # ① request_normalize — 4 dimensions + filters
    normalized_query: Optional[str]  # JSON string

    # ② query_validate — valid query or exception reason
    validation_result: Optional[str]  # JSON string

    # ③ readonly_query — aggregates and their provenance
    query_result: Optional[str]  # JSON string; cleared by ④
    dataset_version: Optional[str]

    # ④ provenance_assemble — cited output and its S-3 clearance
    provenance_output: Optional[str]  # Markdown
    s3_clearance: Optional[str]  # "clear" | "redacted" | "blocked"

    # ⓪〜④ — review-ready exception report and error taxonomy (§8)
    exception_report: Optional[str]  # Markdown
    error_code: Optional[str]

    # ①〜④ — audit metadata
    audit_log: Optional[str]  # JSON string

    # Outer merge_output() only — Inner node order, the DB-6 assertion handle
    inner_node_history: Optional[str]  # JSON string
