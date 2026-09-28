"""AgentCore Platform v1.0 — Outer pre/post_process tests (docs/02_design.md §3)"""

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes import pass_through_node as mod
from src.nodes.pass_through_node import PassThroughNode


def test_declares_the_trust_level_in_the_class_body():
    """gate-trust-level-check reads the class body, not an instance attribute."""
    assert PassThroughNode.__dict__["required_trust_level"] is TrustLevel.VERIFIED_EXTERNAL


def test_writes_no_channel_at_all():
    """§3: "State を変更しない". An empty delta is the assertion — returning
    `status` unchanged would be a redundant write, and a hard-coded SUCCESS from
    pre_process would mask the status `main` must establish before route() reads it."""
    assert PassThroughNode().execute({
        "user_input": "anything",
        "status": AgentStatus.PENDING.value,
        "query_result": "upstream value",
    }) == {}


def test_emits_exactly_one_domain_audit_event(monkeypatch):
    """TC-05 requires a domain-specific emit_trace_event() in every execute(),
    no-op nodes included; node_start / node_complete belong to BaseNode and
    re-emitting them would corrupt the audit trail (§7-4)."""
    seen = []
    monkeypatch.setattr(mod, "emit_trace_event", lambda t, p, s: seen.append((t, p)))
    PassThroughNode().execute({"user_input": "x"})

    assert len(seen) == 1
    event_type, payload = seen[0]
    assert event_type not in ("node_start", "node_complete", "node_error")
    assert payload["node"] == "PassThroughNode"


def test_the_same_class_fills_both_backbone_slots():
    """Outer node_history legitimately contains PassThroughNode twice (§11 DB-6),
    so the two instances must be interchangeable — no per-slot state."""
    pre, post = PassThroughNode(), PassThroughNode()
    state = {"user_input": "x", "status": AgentStatus.SUCCESS.value}
    assert pre.execute(state) == post.execute(state) == {}


def test_the_real_final_s3_gate_accepts_the_empty_delta():
    node = PassThroughNode()
    assert node._security_gate_output(node.execute({"user_input": "x"})) == {}
