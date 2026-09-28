"""Issue #10 — 本番 invoke 経路で境界を検証するための最小ハーネス（PB-Cat2 / DB-1〜）.

docs/03_test_spec.md「本テンプレート固有の検証方針」:
**差し替えてよいのは外部サービスのみ（LLM・承認済みデータセット層）。**

このモジュールは Outer `Graph` / Inner `VisitorStatsQueryGraph` を**サブクラス化**し、
`register_nodes()` / `get_subgraph()` の**本番と同一のシーム**で外部サービスだけを
差し替える。差し替えるのは 2 点のみ:

- ① `request_normalize` の LLM（`RequestNormalizeNode(llm=...)`。本番は None で
  AzureOpenAIClient を都度生成する外部サービス）
- ③ `readonly_query` のデータセット層（`config["dataset"]`。本番は承認済み実ファイル層）

実行されるのは**本物の** ⓪①②③④ ノードコードと**本物の** invoke 機構
（S-1 ゲート・S-2/S-3 ゲート・pack/unpack・merge_output・reducer チャネル）。
本番に存在しないデータ経路を新設しない（[[feedback-pb-no-fake-injection]]）。State への
直接注入もしない — 投入は必ず 1 本の JSON（§5-2）を `Graph.invoke()` に渡す経路のみ。
"""

import json
from typing import Any

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import Graph, VisitorStatsQueryGraphNode
from src.graph.visitor_stats_query_graph import VisitorStatsQueryGraph
from src.services.visitor_stats_dataset import DEFAULT_DATASET_ROOT, VisitorStatsDataset

# ------------------------------------------------------------ 外部サービス① LLM


class StubLLM:
    """① が本番で使う AzureOpenAIClient と同じ契約の外部サービススタブ.

    `complete(messages)` は str か `{"content": str}` を返す（本番 AzureOpenAIClient と
    同一契約・request_normalize_node._complete が両形を受ける）。テストは正規化 JSON を
    そのまま返させることで、本物の ① が payload をマッピングし ②③④ へ流す経路を通す。
    """

    def __init__(self, normalized: Any) -> None:
        # dict → JSON 文字列で返す（本番 LLM も JSON テキストを返す）。str はそのまま。
        self._content = (
            normalized
            if isinstance(normalized, str)
            else json.dumps(normalized, ensure_ascii=False)
        )
        self.calls: list[Any] = []

    def complete(self, messages: Any) -> dict[str, Any]:
        self.calls.append(messages)
        return {"content": self._content}


# ---------------------------------------------------- 外部サービス② データセット層


class RecordingDataset:
    """本番 `VisitorStatsDataset` を委譲でラップし、書き込み経路が無いことを記録する.

    DB-5 用: read-only 性は「書き込みメソッドを持たない」構造で保証されるが（§4-3）、
    本番 invoke 経路からもデータセットへの write が 0 件であることを可観測にする。
    実データは本物の `VisitorStatsDataset`（実ファイル）に委譲する（PB-3 の実データ取得）。
    """

    def __init__(self, root: str = DEFAULT_DATASET_ROOT) -> None:
        self._inner = VisitorStatsDataset(root)
        self.write_calls = 0

    def load_manifest(self) -> dict[str, Any]:
        return self._inner.load_manifest()

    def query(self, normalized_query: dict[str, Any]) -> dict[str, Any]:
        return self._inner.query(normalized_query)

    # write 系はどれも定義しないが、万一 ③ が呼んだら記録できるよう __getattr__ で捕捉。
    def __getattr__(self, name: str) -> Any:
        if any(w in name for w in ("insert", "update", "delete", "write", "upsert")):
            def _blocked(*_a: Any, **_k: Any) -> None:
                self.write_calls += 1
                raise AttributeError(name)
            return _blocked
        raise AttributeError(name)


class CredentialDataset:
    """DB-1 用の外部サービススタブ: `dataset_version` に credential 形状を含めて返す.

    本番 `VisitorStatsDataset` と同一の戻り値契約（available/matched/value/unit/
    citation/dataset_version/ingested_date）。データセット由来文字列に credential が
    紛れ込む現実の失敗を再現し、本物の ③（redact）→ ④（S-3 判定）を通す。
    fake injection ではなく外部サービスの返り値差し替え（本番 ③④ をそのまま実行）。
    """

    # 本物の credential ではない（§7-5）: credential_detector パターンに合致する形だけ。
    CREDENTIAL = "AKIAIOSFODNN7EXAMPLE"

    def load_manifest(self) -> dict[str, Any]:
        return {
            "available": True,
            "name": "credential-in-version dataset",
            "version": f"2025.03 {CredentialDataset.CREDENTIAL}",
            "ingested_date": "2025-04-10",
            "available_dimensions": ["period", "region", "segment", "metric"],
        }

    def query(self, normalized_query: dict[str, Any]) -> dict[str, Any]:
        return {
            "available": True,
            "matched": True,
            "value": 12840000,
            "unit": "人",
            "citation": "JNTO 訪日外客統計 2025年3月",
            # dataset_version に credential が混入している（③ が除去 → ④ が spine で検知）。
            "dataset_version": f"2025.03 {CredentialDataset.CREDENTIAL}",
            "ingested_date": "2025-04-10",
        }


# ---------------------------------------------------------- Outer/Inner サブクラス


def make_graph(*, llm: Any, dataset: Any) -> Graph:
    """外部サービス（LLM・dataset）だけを本番シームで差し替えた Outer Graph を返す.

    差し替えは register_nodes / get_subgraph の**本番と同じ位置**でのみ行う。
    ノード実装・エッジ・状態スキーマ・ゲートはすべて本番のものが走る。
    """

    class _Inner(VisitorStatsQueryGraph):
        def register_nodes(self) -> None:
            # 本番 register_nodes と同一構成。差し替えるのは ① の llm と ③ の dataset のみ。
            from src.nodes.input_expand_node import InputExpandNode
            from src.nodes.provenance_assemble_node import ProvenanceAssembleNode
            from src.nodes.query_validate_node import QueryValidateNode
            from src.nodes.readonly_query_node import ReadonlyQueryNode
            from src.nodes.request_normalize_node import RequestNormalizeNode

            self._nodes["input_expand"] = InputExpandNode()
            self._nodes["request_normalize"] = RequestNormalizeNode(llm=llm)
            self._nodes["query_validate"] = QueryValidateNode()
            self._nodes["readonly_query"] = ReadonlyQueryNode(dataset=self.config["dataset"])
            self._nodes["provenance_assemble"] = ProvenanceAssembleNode()

    class _Node(VisitorStatsQueryGraphNode):
        def get_subgraph(self) -> Any:
            return _Inner(config={"dataset": dataset})

    class _Outer(Graph):
        def register_nodes(self) -> None:
            from framework.graph.agent_base_graph import AgentBaseGraph

            AgentBaseGraph.register_nodes(self)  # バックボーン 5 スロット
            from src.nodes.pass_through_node import PassThroughNode

            self._nodes["pre_process"] = PassThroughNode()
            self._nodes["main"] = _Node(dataset=dataset)
            self._nodes["post_process"] = PassThroughNode()

    return _Outer(config={"dataset_root": DEFAULT_DATASET_ROOT})


def verified_ctx() -> InvocationContext:
    """S-1 トラストゲートを通すため VERIFIED_EXTERNAL を明示する（§7-1）."""
    return InvocationContext(
        session_id="s", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, caller_id=""
    )


def intake(analyst_request: str, approved_schema: Any) -> str:
    """呼び出し側 JSON（§5-2 の 1 本経路）。approved_schema は dict なら JSON 文字列化."""
    schema = (
        approved_schema
        if isinstance(approved_schema, str)
        else json.dumps(approved_schema, ensure_ascii=False)
    )
    return json.dumps(
        {"analyst_request": analyst_request, "approved_schema": schema},
        ensure_ascii=False,
    )


# 承認済みスキーマ（② が読む形。実データ行 2025-Q1/関東/訪日外客/訪問者数 に一致）。
APPROVED_SCHEMA: dict[str, Any] = {
    "dimensions": ["period", "region", "segment", "metric"],
    "allowed_metrics": ["訪問者数", "延べ宿泊者数"],
    "disclosure_restricted": ["個人識別", "市区町村"],
}

# 実データ行に完全一致する正規化クエリ（happy path で ③ が matched=True を返す）。
HAPPY_NORMALIZED: dict[str, Any] = {
    "period": "2025-Q1",
    "region": "関東",
    "segment": "訪日外客",
    "metric": "訪問者数",
    "filters": {},
}


def run(graph: Graph, user_input: str) -> dict[str, Any]:
    """本番と同じ invoke 経路（JSON 1 本 + VERIFIED_EXTERNAL ctx）で結果を得る."""
    return graph.invoke(user_input, ctx=verified_ctx())
