"""AgentCore Platform v1.0 — Outer graph (docs/02_design.md §3 / §3-1 / §5 / §5-4 / §7-7).

Cat 2 Outer graph. The fixed AgentBaseGraph backbone
`START → initialize → pre_process → main → {route} → post_process → finalize → END`
carries no domain logic; all of it lives inside the `main` slot's
`VisitorStatsQueryGraphNode`, which invokes the inner
`VisitorStatsQueryGraph` (§4) and bridges its input/output.

Four bridges make the two graphs one request (§5):

*Dataset lifetime (§3-1 / §4-3, hop ①).* `register_nodes()` builds the single
`VisitorStatsDataset` — from `config["dataset_root"]`, defaulting to
`DEFAULT_DATASET_ROOT` so a wiring gap never `KeyError`s the process at import —
and injects that one instance into the GraphNode. The inner graph reuses it, so
the manifest is memoised across requests (a per-request `get_subgraph()` would
otherwise re-read it every call).

*Input bridge (§5-2).* Outer→Inner state is not shared; `extract_input()` packs
the caller JSON with `pack_intake()` so the inner ⓪ can restore it without the
@final S-2 gate shredding region/segment names.

*Output bridge (§5-4).* `merge_output()` lifts status (so Outer doesn't stall at
`pending`), `formatted_output` (provenance_output or exception_report — never
`output`, which the State channel drops, §3-1), the domain fields, and builds
`inner_node_history` from the inner `node_history`. It deliberately does NOT lift
`query_result` (§7-3 leak prevention) nor the reducer channels.

*Failure bridge (§7-7).* `error_strategy = "handle"` + `on_subgraph_error()` keep
the exception report alive when the inner graph dies on an unexpected exception —
the one path that returns `status = ERROR` honestly rather than losing the report
to `propagate`. `error` is read with `getattr` because the raw-exception path
hands a bare exception with no `error_log`.
"""

import json
from typing import Any, ClassVar

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus

from src.nodes.pass_through_node import PassThroughNode
from src.schemas.state import State
from src.services.visitor_stats_dataset import DEFAULT_DATASET_ROOT, VisitorStatsDataset

# §8: on_subgraph_error だけが返す error_code（予期しない例外・§7-7）。
ERROR_INTERNAL = "INTERNAL_ERROR"

# §5-4: merge_output が持ち上げるドメインフィールド（status / formatted_output /
# inner_node_history は個別処理。query_result・reducer チャネルは持ち上げない）。
_LIFTED_DOMAIN_FIELDS = (
    "error_code",
    "provenance_output",
    "exception_report",
    "dataset_version",
    "s3_clearance",
    "normalized_query",
    "validation_result",
)

# §3-1: get_output が super の 5 キーに追記するドメインフィールド。
_OUTPUT_DOMAIN_FIELDS = (
    "provenance_output",
    "exception_report",
    "dataset_version",
    "error_code",
    "s3_clearance",
    "normalized_query",
    "validation_result",
    "inner_node_history",
)

# §6 / §3-1: Outer が None 初期化するドメインフィールド 12 個（user_input を含めない）。
_OUTER_INITIAL_FIELDS = (
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
    "inner_node_history",
)


def _build_internal_error_report(error: Exception) -> str:
    """§7-7: Inner の error_log 由来のレビュー可能な例外レポート.

    `error` は SubgraphError（error_log あり）か生例外（error_log 無し）の 2 通り。
    無防備に `error.error_log` を読むと生例外経路で AttributeError → §7-7 が防ごうと
    した失敗が再発するため、必ず getattr で取得する。
    """
    log = getattr(error, "error_log", [])
    lines = [str(x) for x in log] if isinstance(log, list) else [str(log)]
    detail = "\n".join(f"- {x}" for x in lines) if lines else "- （詳細なし）"
    return (
        "# Inner graph 例外レポート（INTERNAL_ERROR）\n\n"
        "Inner graph が予期しない例外で停止しました（§7-7 多層防御）。\n\n"
        f"## error_log\n{detail}\n"
    )


class VisitorStatsQueryGraphNode(GraphNode):
    """Outer `main` スロット。Inner graph を invoke し入出力をブリッジする（§5）."""

    # §7-7: framework 既定 propagate ではなく handle を明示宣言する。
    error_strategy: ClassVar[str] = "handle"

    def __init__(self, dataset: Any = None) -> None:
        # §4-3 ホップ②: Outer register_nodes() が生成した唯一の dataset を保持し、
        # get_subgraph() で Inner の config に載せる。
        super().__init__()
        self._dataset = dataset

    def get_subgraph(self) -> Any:
        # §4-3 ホップ③: Inner を毎 invoke 生成する（GraphNode.execute の契約）。
        # dataset インスタンスを config で運ぶ（メモ化は Outer 側インスタンスで効く）。
        from src.graph.visitor_stats_query_graph import VisitorStatsQueryGraph

        return VisitorStatsQueryGraph(config={"dataset": self._dataset})

    def extract_input(self, state: AgentState) -> str:
        # §5-2: 呼び出し側 JSON を pack_intake でパックし Inner ⓪ に渡す。
        # build_intake_payload / pack_intake は raise しない（異常は _intake_error）。
        from src.nodes.input_expand_node import build_intake_payload, pack_intake

        data = state if isinstance(state, dict) else {}
        return pack_intake(build_intake_payload(data.get("user_input")))

    def merge_output(self, state: AgentState, sub_result: dict[str, Any]) -> dict[str, Any]:
        # §5-4: 変更キーのみの delta。status を必ず持ち上げる（載せないと Outer が
        # pending のまま finalize に落ち post_process がスキップされる）。
        result = sub_result if isinstance(sub_result, dict) else {}
        provenance = result.get("provenance_output")
        exception_report = result.get("exception_report")

        delta: dict[str, Any] = {
            "status": result.get("status"),
            # formatted_output に成果物を載せる（output は State チャネルに無く破棄・§3-1）。
            "formatted_output": provenance or exception_report,
        }
        for field in _LIFTED_DOMAIN_FIELDS:
            delta[field] = result.get(field)

        # DB-6: 非 reducer の inner_node_history に Inner のノード順を JSON で載せる。
        # node_history（reducer）を直接持ち上げると二重計上になる。
        delta["inner_node_history"] = json.dumps(result.get("node_history", []), ensure_ascii=False)
        # query_result は意図的に持ち上げない（§7-3 の生値流出防止）。
        return delta

    def on_subgraph_error(self, state: AgentState, error: Exception) -> dict[str, Any]:
        # §7-7: 予期しない例外でも例外レポートを失わない最後の保険。
        # SUCCESS に偽装せず ERROR を正直に返しつつレポートを残す。
        report = _build_internal_error_report(error)
        return {
            "status": AgentStatus.ERROR.value,
            "error_code": ERROR_INTERNAL,
            "exception_report": report,
            "formatted_output": report,
            # error_log は reducer チャネルへ 1 件 append（§6 / §7-7）。
            "error_log": [f"{ERROR_INTERNAL}: {getattr(error, 'args', (error,))}"],
        }


class Graph(AgentBaseGraph):
    """Outer graph — 固定バックボーン + main スロットの Inner ブリッジ（§3）."""

    @property
    def name(self) -> str:
        return "TRV-C2-019 Destination Visitor-Statistics Query Agent"

    @property
    def state_schema(self) -> type:
        # §3-1 必須: override しないと State のドメインフィールドが全て捨てられる。
        return State

    def _extra_initial_state(self) -> dict[str, Any]:
        # §3-1 / §6: ドメインフィールド 12 個を None 初期化。user_input は含めない
        # （含めると BaseGraph.invoke が書いた呼び出し側入力を None 上書きしてしまう）。
        return {field: None for field in _OUTER_INITIAL_FIELDS}

    def register_nodes(self) -> None:
        # §3-1: super() で 5 スロット（initialize/finalize 含む）を作った後に埋める。
        super().register_nodes()
        # §4-3 ホップ①: VisitorStatsDataset を 1 回だけ生成（.get で fail-safe。
        # 配線漏れが KeyError で起動不能になるのを避ける・§4-3 項目3）。
        dataset = VisitorStatsDataset(self.config.get("dataset_root", DEFAULT_DATASET_ROOT))
        self._nodes["pre_process"] = PassThroughNode()
        self._nodes["main"] = VisitorStatsQueryGraphNode(dataset=dataset)
        self._nodes["post_process"] = PassThroughNode()

    def get_output(self, state: Any) -> dict[str, Any]:
        # §3-1: super の 5 キー（output/status/trace_id/correlation_id/node_history）に
        # ドメインフィールドを追記する。
        out: dict[str, Any] = dict(super().get_output(state))
        data = state if isinstance(state, dict) else {}
        for field in _OUTPUT_DOMAIN_FIELDS:
            out[field] = data.get(field)
        return out
