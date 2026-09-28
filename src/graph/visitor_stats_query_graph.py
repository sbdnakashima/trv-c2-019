"""AgentCore Platform v1.0 — Inner graph (docs/02_design.md §4 / §4-1 / §4-3).

`VisitorStatsQueryGraph` is the Cat 2 *inner* workflow — the five deterministic
hops that turn an analyst's natural-language request into a provenance-wrapped
aggregate. It subclasses `BaseGraph` directly (custom linear topology, no
initialize/finalize backbone — those belong to the Outer graph, §3) and
implements all seven abstract members: name / state_schema / register_nodes /
add_edges / route / get_output / _validate_config.

```
START → ⓪ input_expand → ① request_normalize → ② query_validate
      → ③ readonly_query → ④ provenance_assemble → END
```

Three contracts are load-bearing here:

*Dataset injection (§4-3, hop ④).* `register_nodes()` does NOT construct
`VisitorStatsDataset`; ③ `readonly_query` receives the single instance the Outer
graph created once, carried in `config["dataset"]`. `_validate_config()` makes
that mandatory — a missing hop fails at compile() with `ConfigError`, not at
③'s runtime with `None.query()`.

*get_output surfaces node_history (§4-1, DB-6).* `AgentBaseGraph.get_output()`
returns only five keys; the Outer `merge_output()` can read nothing but this
graph's `get_output()` return value (`_enrich_output()` is a no-op), so
node_history and every domain field are listed explicitly. Without node_history
the Outer cannot build `inner_node_history` and DB-6 has no evidence.

*_extra_initial_state is 11 fields, not 12 (§4-1).* The inner graph initializes
the same domain fields as the Outer *except* `inner_node_history` — that channel
is written by the Outer's `merge_output()`, never by the inner nodes.
"""

from typing import Any

from framework.errors import ConfigError
from framework.graph.base_graph import BaseGraph

from src.nodes.input_expand_node import InputExpandNode
from src.nodes.provenance_assemble_node import ProvenanceAssembleNode
from src.nodes.query_validate_node import QueryValidateNode
from src.nodes.readonly_query_node import ReadonlyQueryNode
from src.nodes.request_normalize_node import RequestNormalizeNode
from src.schemas.state import State

# §4 の 5 ホップ。START → 各ノード → END を add_edges() で直列に張る。
_INPUT_EXPAND = "input_expand"
_REQUEST_NORMALIZE = "request_normalize"
_QUERY_VALIDATE = "query_validate"
_READONLY_QUERY = "readonly_query"
_PROVENANCE_ASSEMBLE = "provenance_assemble"

# §4-1: get_output() が明示公開するドメインフィールド（node_history は別途）。
_DOMAIN_OUTPUT_FIELDS = (
    "provenance_output",
    "exception_report",
    "dataset_version",
    "error_code",
    "s3_clearance",
    "normalized_query",
    "validation_result",
    "query_result",
)

# §4-1 / §6: Inner が None 初期化する 11 フィールド（inner_node_history を含めない）。
_INNER_INITIAL_FIELDS = (
    "analyst_request",
    "approved_schema",
    "normalized_query",
    "validation_result",
    "query_result",
    "dataset_version",
    "provenance_output",
    "exception_report",
    "s3_clearance",
    "audit_log",
    "error_code",
)


class VisitorStatsQueryGraph(BaseGraph):
    """Inner graph — 承認済み JNTO 統計への読み取り専用クエリ 5 ホップ（§4）."""

    @property
    def name(self) -> str:
        return "visitor_stats_query_inner"

    @property
    def state_schema(self) -> type:
        # §4-1 / §3-1: Outer と同じ State。BaseGraph._build_graph() が
        # StateGraph(state_schema) と各ノードの input_schema に使うため、
        # ここで State を返さないとドメインフィールドがチャネルとして消える。
        return State

    def _validate_config(self) -> None:
        # §4-3 規範: dataset 注入（4 ホップ）が 1 つでも抜けたら compile 時点で
        # ConfigError にする。③ の実行時 AttributeError（None.query()）を防ぐ。
        if self.config.get("dataset") is None:
            raise ConfigError(
                "VisitorStatsQueryGraph.config['dataset'] が未設定です"
                "（§4-3 の 4 ホップ注入が欠落している可能性があります）"
            )

    def register_nodes(self) -> None:
        # §4-1: super() を呼ばない（BaseGraph.register_nodes は abstract）。
        # initialize / finalize は登録しない（Outer バックボーンの責務）。
        # ③ にはホップ ④ で dataset インスタンスを注入する（§4-3）。
        self._nodes[_INPUT_EXPAND] = InputExpandNode()
        self._nodes[_REQUEST_NORMALIZE] = RequestNormalizeNode()
        self._nodes[_QUERY_VALIDATE] = QueryValidateNode()
        self._nodes[_READONLY_QUERY] = ReadonlyQueryNode(dataset=self.config["dataset"])
        self._nodes[_PROVENANCE_ASSEMBLE] = ProvenanceAssembleNode()

    def add_edges(self) -> None:
        # 線形トポロジ。分岐は無い（異常は §7-6 の error_code で値として運ぶ）。
        from langgraph.graph import END, START

        self._sg.add_edge(START, _INPUT_EXPAND)
        self._sg.add_edge(_INPUT_EXPAND, _REQUEST_NORMALIZE)
        self._sg.add_edge(_REQUEST_NORMALIZE, _QUERY_VALIDATE)
        self._sg.add_edge(_QUERY_VALIDATE, _READONLY_QUERY)
        self._sg.add_edge(_READONLY_QUERY, _PROVENANCE_ASSEMBLE)
        self._sg.add_edge(_PROVENANCE_ASSEMBLE, END)

    def route(self, state: Any) -> str:
        # §4-1: 線形トポロジのため add_conditional_edges からは呼ばれないが、
        # ABC 充足のために実装する。最終ノードへ落とすだけ。
        return _PROVENANCE_ASSEMBLE

    def _extra_initial_state(self) -> dict[str, Any]:
        # §4-1: inner_node_history と user_input は含めない（前者は Outer 専用、
        # 後者は BaseGraph.invoke が呼び出し側入力を書いた後に上書きされる）。
        return {field: None for field in _INNER_INITIAL_FIELDS}

    def get_output(self, state: Any) -> dict[str, Any]:
        # §4-1 / DB-6: status（GraphNode.execute の ERROR 判定材料）と
        # node_history（Outer merge_output が inner_node_history を組む唯一の材料）を
        # 必ず含める。ドメインフィールドと error_code も明示公開する。
        data = state if isinstance(state, dict) else {}
        out: dict[str, Any] = {
            "status": data.get("status"),
            "node_history": data.get("node_history", []),
        }
        for field in _DOMAIN_OUTPUT_FIELDS:
            out[field] = data.get(field)
        return out
