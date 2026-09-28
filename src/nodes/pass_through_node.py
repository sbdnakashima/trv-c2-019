"""AgentCore Platform v1.0 — Outer pre_process / post_process (docs/02_design.md §3)

`AgentBaseGraph` fixes a five-slot backbone, and this template has nothing to do
in the pre_process and post_process slots: intake interpretation belongs to
`extract_input()` and Inner ⓪, output assembly to `merge_output()`. The framework
ships no PassThrough default (`framework/nodes/defaults/` has none), so the slots
are filled with this node — one class registered twice, which is why Outer
`node_history` legitimately contains `PassThroughNode` twice (§11 DB-6).

It still emits an S-4 event: scaffold TC-05 requires a domain-specific
emit_trace_event() inside every node's execute(), no-op nodes included.
"""

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event


class PassThroughNode(FunctionNode):
    """Backbone slot filler — emits an audit event and writes no channel."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> dict[str, Any]:
        emit_trace_event("pass_through", {"node": self.__class__.__name__}, state)
        # Empty delta, deliberately: returning `status` unchanged would be a
        # redundant write, and returning a fixed SUCCESS from pre_process would
        # mask the status that `main` is supposed to establish before route()
        # reads it. BaseNode.__call__() appends node_history itself.
        return {}
