"""Issue #8 — Graph composition（Outer + Inner 4-hop 配線）.

docs/02_design.md §3 / §3-1 / §4 / §4-1 / §4-3 / §5 / §5-4 / §7-7 / §10。

このテストは 3 つの構成要素を契約で固定する:

1. Inner `VisitorStatsQueryGraph`（`src/graph/visitor_stats_query_graph.py`）
   - BaseGraph 直接継承・7 abstract メンバー実装・線形 5 ノードで compile できる
   - `_validate_config()` は `config["dataset"]` の存在を必須検証する（§4-3 規範）
   - `get_output()` は status / node_history / ドメインフィールド / error_code を返す（§4-1・DB-6）
   - `_extra_initial_state()` は 11 フィールド（inner_node_history と user_input を含めない）

2. `VisitorStatsQueryGraphNode`（`GraphNode`・Outer main スロット）
   - `error_strategy = "handle"`（§7-7・framework 既定 propagate からの明示的変更）
   - `extract_input()` は user_input を pack_intake でパックして返す（§5-2）
   - `merge_output()` は §5-4 のキーを持ち上げ、query_result は持ち上げない
   - `merge_output()` は inner_node_history を Inner node_history から JSON で組む（DB-6）
   - `on_subgraph_error()` は getattr で error_log を読み（生例外でも落ちない・§7-7）、
     status=ERROR / error_code=INTERNAL_ERROR / exception_report / formatted_output を返す

3. Outer `Graph`（`src/graph/graph.py`）
   - state_schema=State / _extra_initial_state 12 フィールド / get_output にドメイン追記
   - register_nodes が VisitorStatsDataset を 1 回だけ生成し main スロットに注入
   - end-to-end invoke がバックボーンを通り status=success で来歴を返す
"""
import json

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.errors import ConfigError

from src.nodes.input_expand_node import (
    INNER_INPUT_FIELDS,
    INTAKE_SENTINEL,
    unpack_intake,
)
from src.schemas.state import State
from src.services.visitor_stats_dataset import DEFAULT_DATASET_ROOT, VisitorStatsDataset


# --------------------------------------------------------------- Inner graph


def _inner():
    from src.graph.visitor_stats_query_graph import VisitorStatsQueryGraph

    return VisitorStatsQueryGraph(config={"dataset": VisitorStatsDataset(DEFAULT_DATASET_ROOT)})


def test_inner_graph_compiles_with_linear_topology():
    """Inner は 7 abstract メンバーを実装し compile できる（§4）."""
    g = _inner()
    g.compile()  # BaseGraph._build_graph → StateGraph.compile が例外なく通る
    # ノードは ⓪〜④ の 5 個（initialize/finalize は Outer の責務・登録しない）。
    assert set(g._nodes) == {
        "input_expand",
        "request_normalize",
        "query_validate",
        "readonly_query",
        "provenance_assemble",
    }


def test_inner_state_schema_is_State():
    assert _inner().state_schema is State


def test_inner_validate_config_requires_dataset():
    """§4-3 規範: config["dataset"] が無ければ compile 時に ConfigError で落ちる."""
    from src.graph.visitor_stats_query_graph import VisitorStatsQueryGraph

    with pytest.raises(ConfigError):
        VisitorStatsQueryGraph(config={})._validate_config()


def test_inner_validate_config_passes_with_dataset():
    _inner()._validate_config()  # 例外を投げない


def test_inner_extra_initial_state_excludes_inner_node_history_and_user_input():
    """§4-1: Inner は 11 フィールドを初期化（inner_node_history / user_input を含めない）."""
    init = _inner()._extra_initial_state()
    assert "inner_node_history" not in init
    assert "user_input" not in init
    # §6 のドメインフィールド 12 個 − inner_node_history = 11 個。
    for f in (
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
    ):
        assert f in init and init[f] is None


def test_inner_get_output_returns_status_history_and_domain_fields():
    """§4-1・DB-6: get_output は status / node_history / ドメイン / error_code を含む."""
    g = _inner()
    state = {
        "status": AgentStatus.SUCCESS.value,
        "node_history": ["input_expand", "request_normalize"],
        "provenance_output": "# 来歴",
        "error_code": None,
        "dataset_version": "2025.03",
        "s3_clearance": "clear",
        "normalized_query": "{}",
        "validation_result": "{}",
        "exception_report": None,
    }
    out = g.get_output(state)
    assert out["status"] == AgentStatus.SUCCESS.value
    assert out["node_history"] == ["input_expand", "request_normalize"]
    assert out["provenance_output"] == "# 来歴"
    assert out["error_code"] is None
    assert out["dataset_version"] == "2025.03"
    assert out["s3_clearance"] == "clear"


# --------------------------------------------------- VisitorStatsQueryGraphNode


def _node():
    from src.graph.graph import VisitorStatsQueryGraphNode

    return VisitorStatsQueryGraphNode(dataset=VisitorStatsDataset(DEFAULT_DATASET_ROOT))


def test_node_declares_handle_error_strategy():
    """§7-7: framework 既定 propagate ではなく handle を明示宣言する."""
    assert _node().error_strategy == "handle"


def test_extract_input_packs_user_input_for_inner():
    """§5-2: Outer の user_input を pack_intake でパックして Inner に渡す."""
    user_input = json.dumps(
        {"analyst_request": "関東の訪日客数", "approved_schema": "{}"},
        ensure_ascii=False,
    )
    packed = _node().extract_input({"user_input": user_input})
    assert isinstance(packed, str)
    assert packed.startswith(INTAKE_SENTINEL)
    # Inner ⓪ が復元できる（roundtrip）。
    payload = unpack_intake(packed)
    assert payload["analyst_request"] == "関東の訪日客数"
    for f in INNER_INPUT_FIELDS:
        assert f in payload


def test_merge_output_lifts_design_keys_and_status():
    """§5-4: status / formatted_output / ドメインフィールドを持ち上げる."""
    sub_result = {
        "status": AgentStatus.SUCCESS.value,
        "provenance_output": "# 来歴 12,840,000 人",
        "exception_report": None,
        "error_code": None,
        "dataset_version": "2025.03",
        "s3_clearance": "clear",
        "normalized_query": "{}",
        "validation_result": "{}",
        "query_result": '{"value": 12840000}',
        "node_history": ["input_expand", "provenance_assemble"],
    }
    delta = _node().merge_output({}, sub_result)
    assert delta["status"] == AgentStatus.SUCCESS.value
    # formatted_output に provenance_output を載せる（output ではない・§3-1）。
    assert delta["formatted_output"] == "# 来歴 12,840,000 人"
    assert "output" not in delta
    assert delta["provenance_output"] == "# 来歴 12,840,000 人"
    assert delta["s3_clearance"] == "clear"
    assert delta["dataset_version"] == "2025.03"


def test_merge_output_does_not_lift_query_result():
    """§5-4 / §7-3: 生の query_result は Outer に持ち上げない."""
    sub_result = {
        "status": AgentStatus.SUCCESS.value,
        "provenance_output": "# 来歴",
        "query_result": '{"value": 12840000}',
        "node_history": [],
    }
    delta = _node().merge_output({}, sub_result)
    assert "query_result" not in delta


def test_merge_output_does_not_lift_reducer_channels():
    """§5-4: node_history / error_log は reducer なので載せない（二重計上防止）."""
    sub_result = {
        "status": AgentStatus.SUCCESS.value,
        "provenance_output": "# 来歴",
        "node_history": ["input_expand", "provenance_assemble"],
        "error_log": ["x"],
    }
    delta = _node().merge_output({}, sub_result)
    assert "node_history" not in delta
    assert "error_log" not in delta


def test_merge_output_builds_inner_node_history_from_node_history():
    """DB-6: inner_node_history = json(sub_result.node_history)（非 reducer チャネル）."""
    sub_result = {
        "status": AgentStatus.SUCCESS.value,
        "provenance_output": "# 来歴",
        "node_history": ["input_expand", "request_normalize", "provenance_assemble"],
    }
    delta = _node().merge_output({}, sub_result)
    assert json.loads(delta["inner_node_history"]) == [
        "input_expand",
        "request_normalize",
        "provenance_assemble",
    ]


def test_merge_output_uses_exception_report_when_no_provenance():
    """§5-4: provenance_output が無ければ formatted_output に exception_report を載せる."""
    sub_result = {
        "status": AgentStatus.SUCCESS.value,
        "provenance_output": None,
        "exception_report": "# 例外レポート\nDISCLOSURE_RESTRICTED",
        "error_code": "DISCLOSURE_RESTRICTED",
        "node_history": [],
    }
    delta = _node().merge_output({}, sub_result)
    assert delta["formatted_output"] == "# 例外レポート\nDISCLOSURE_RESTRICTED"
    assert delta["error_code"] == "DISCLOSURE_RESTRICTED"


def test_on_subgraph_error_reads_error_log_with_getattr_on_raw_exception():
    """§7-7: error が生例外（error_log 属性なし）でも getattr で落ちない."""
    raw = AttributeError("boom")  # _handle_call_error 経由で来る生例外を模す
    delta = _node().on_subgraph_error({"error_log": ["should-not-read-from-state"]}, raw)
    assert delta["status"] == AgentStatus.ERROR.value
    assert delta["error_code"] == "INTERNAL_ERROR"
    assert delta["exception_report"]
    assert delta["formatted_output"] == delta["exception_report"]


def test_on_subgraph_error_uses_subgraph_error_error_log():
    """§7-7: SubgraphError 経路では error.error_log を使いレポートに反映する."""
    from framework.errors import SubgraphError

    err = SubgraphError(agent_name="inner", error_log=["step1 failed"], trace_id="t")
    delta = _node().on_subgraph_error({}, err)
    assert delta["status"] == AgentStatus.ERROR.value
    assert delta["error_code"] == "INTERNAL_ERROR"
    # error_log は reducer チャネルへ 1 件 append（delta にリストで載る）。
    assert isinstance(delta.get("error_log"), list)
    assert len(delta["error_log"]) == 1


# ------------------------------------------------------------------- Outer Graph


def _outer():
    from src.graph.graph import Graph

    return Graph(config={"dataset_root": DEFAULT_DATASET_ROOT})


def test_outer_state_schema_is_State():
    assert _outer().state_schema is State


def test_outer_extra_initial_state_has_12_domain_fields_no_user_input():
    """§3-1 / §6: ドメインフィールド 12 個を None 初期化・user_input を含めない."""
    init = _outer()._extra_initial_state()
    assert "user_input" not in init  # 含めると呼び出し側入力が None 上書きされる
    for f in (
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
        "inner_node_history",  # Outer は inner_node_history も初期化する（Inner との差）
    ):
        assert f in init and init[f] is None


def test_outer_register_nodes_injects_dataset_into_main_slot():
    """§3-1 / §4-3 ホップ①: VisitorStatsDataset を 1 回だけ生成し main に注入する."""
    g = _outer()
    g.register_nodes()
    main = g._nodes["main"]
    # main スロットは VisitorStatsQueryGraphNode で、dataset が注入されている。
    from src.graph.graph import VisitorStatsQueryGraphNode

    assert isinstance(main, VisitorStatsQueryGraphNode)
    assert main._dataset is not None
    assert isinstance(main._dataset, VisitorStatsDataset)
    # pre_process / post_process は PassThroughNode。
    from src.nodes.pass_through_node import PassThroughNode

    assert isinstance(g._nodes["pre_process"], PassThroughNode)
    assert isinstance(g._nodes["post_process"], PassThroughNode)


def test_outer_get_output_adds_domain_fields():
    """§3-1: super().get_output の 5 キーにドメインフィールドを追記する."""
    g = _outer()
    state = {
        "formatted_output": "# 来歴",
        "status": AgentStatus.SUCCESS.value,
        "trace_id": "t",
        "correlation_id": "c",
        "node_history": ["initialize", "main"],
        "provenance_output": "# 来歴",
        "s3_clearance": "clear",
        "error_code": None,
        "dataset_version": "2025.03",
        "inner_node_history": "[]",
    }
    out = g.get_output(state)
    assert out["output"] == "# 来歴"  # 5 キー側は維持
    assert out["provenance_output"] == "# 来歴"
    assert out["s3_clearance"] == "clear"
    assert out["dataset_version"] == "2025.03"
    assert out["inner_node_history"] == "[]"


def test_outer_end_to_end_invoke_wires_backbone_and_inner():
    """Outer→Inner が 1 リクエストとして貫通する配線契約を固定する.

    ① request_normalize は本番で AzureOpenAIClient を使うため（テストで LLM を注入
    しない場合）ここでは NORMALIZE_ERROR で fail-closed するが、それは §7-6 の
    status=SUCCESS + error_code 経路であり、本テストが検証するのは LLM の中身では
    なく配線である:
      - merge_output が status を持ち上げるので pending にならない（§5-4）
      - Inner ⓪〜④ が全て走り DB-6 の inner_node_history が組まれる
      - formatted_output → output のブリッジが効き output が None にならない（§3-1）
      - S-1 trust gate を通すため caller_trust_level=VERIFIED_EXTERNAL を渡す。
    """
    from framework.schemas.invocation_context import InvocationContext
    from framework.schemas.trust_level import TrustLevel

    g = _outer()
    user_input = json.dumps(
        {
            "analyst_request": "2025-Q1 の関東エリアにおける訪日外客の訪問者数を教えてください",
            "approved_schema": json.dumps(
                {
                    "period": ["2025-Q1"],
                    "region": ["関東"],
                    "segment": ["訪日外客"],
                    "metric": ["訪問者数"],
                },
                ensure_ascii=False,
            ),
        },
        ensure_ascii=False,
    )
    ctx = InvocationContext(
        session_id="s", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, caller_id=""
    )
    result = g.invoke(user_input, ctx=ctx)
    # pending のままにならない = merge_output が status を持ち上げた（§5-4）。
    assert result["status"] == AgentStatus.SUCCESS.value
    # 主成果物 output が None にならない（formatted_output 経由・§3-1）。
    assert result["output"]
    # DB-6: Inner の 5 ノードが全て走り inner_node_history が組まれる。
    inner_history = json.loads(result["inner_node_history"])
    assert len(inner_history) == 5
