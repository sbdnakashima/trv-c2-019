"""AgentCore Platform v1.0 — State schema tests (docs/02_design.md §6)"""

import operator
from typing import Optional, get_args, get_type_hints

from framework.schemas.agent_state import AgentState

from src.schemas.state import State

# The 12 domain fields of §6, in src/schemas/state.py declaration order (§6 fixes
# the *set* of fields, not their order — TypedDict order has no runtime meaning).
DOMAIN_FIELDS = (
    "analyst_request",
    "approved_schema",
    "normalized_query",
    "validation_result",
    "query_result",
    "dataset_version",
    "provenance_output",
    "s3_clearance",
    "exception_report",
    "error_code",
    "audit_log",
    "inner_node_history",
)

# Channels the framework's @final S-2 gate masks PII in; §6 forbids using them.
FORBIDDEN_FIELDS = ("validated_input", "llm_response")

# TypedDict merges inherited annotations into the subclass and erases the base
# from __mro__ (it is (State, dict, object)), so issubclass() cannot be used and
# __annotations__ cannot identify own fields. Both are derived by set algebra.
OWN_FIELDS = set(State.__annotations__) - set(AgentState.__annotations__)


def test_state_adds_exactly_twelve_channels():
    """Nothing beyond the 12 domain fields is bolted onto the framework state.

    A subset assertion would be dead here — TypedDict always merges the base's
    annotations in, so inherited keys cannot go missing. That property is enforced
    where it can actually fail: test_no_inherited_channel_is_redeclared indexes
    every inherited key directly.
    """
    assert len(State.__annotations__) == len(AgentState.__annotations__) + 12


def test_declares_exactly_the_twelve_domain_fields():
    assert OWN_FIELDS == set(DOMAIN_FIELDS), f"drift: {OWN_FIELDS ^ set(DOMAIN_FIELDS)}"
    assert len(DOMAIN_FIELDS) == 12


def test_every_domain_field_is_optional_str():
    """ADR-005: list/dict/Pydantic/dataclass payloads must be JSON strings.

    include_extras=True is required: the default strips Annotated metadata, so
    Annotated[Optional[str], operator.add] would compare equal to Optional[str]
    while turning the channel into a BinaryOperatorAggregate that concatenates
    on every write. Container types are implied by this equality check; channel
    *shape* is not, which is why the reducer is checked here rather than trusted.
    """
    hints = get_type_hints(State, include_extras=True)
    for field in DOMAIN_FIELDS:
        assert hints[field] == Optional[str], f"{field} is {hints[field]}, not Optional[str]"


def test_no_inherited_channel_is_redeclared():
    """No AgentState channel may be redeclared — all 31, not a hand-picked few.

    A redeclared inherited key stays inside AgentState's key set, so it drops out
    of OWN_FIELDS and membership assertions can never fail; annotation values are
    compared instead. Covering every inherited key (rather than listing the ones
    the design happens to touch) is what protects retry_count, messages, result
    and the NotRequired HITL fields, whose types the framework itself relies on.

    Subsumes the §6 constraints that used to be tested by name:
      - validated_input / llm_response (FORBIDDEN_FIELDS) must stay untouched,
        because the @final S-2 gate masks PII in them and would destroy Title
        Case region and segment names.
      - node_history / error_log must keep their operator.add reducers.
    """
    for field in AgentState.__annotations__:
        assert State.__annotations__[field] == AgentState.__annotations__[field], (
            f"{field} was redeclared; the framework's channel semantics are lost"
        )
    # If the framework ever renames these, the §6 rationale above went stale.
    for field in FORBIDDEN_FIELDS:
        assert field in AgentState.__annotations__, f"{field} is gone; recheck §6"


def test_inherits_reducer_channels_intact():
    """node_history / error_log must arrive with operator.add, or history dedups.

    A positive assertion on the inherited annotation: the redeclaration guard
    lives in test_no_inherited_channel_is_redeclared.
    """
    for field in ("node_history", "error_log"):
        assert operator.add in get_args(State.__annotations__[field]), (
            f"{field} lost its operator.add reducer"
        )


def test_no_credential_shaped_field_names():
    """ADR-005: secrets must never reach the checkpoint DB."""
    banned = ("secret", "token", "password", "api_key", "apikey", "credential", "jwt")
    for field in OWN_FIELDS:
        lowered = field.lower()
        for word in banned:
            assert word not in lowered, f"{field} looks like a credential channel"
