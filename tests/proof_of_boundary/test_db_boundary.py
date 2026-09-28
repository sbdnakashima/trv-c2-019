"""Issue #10 — Domain Boundary Tests（DB-1〜DB-6・本番 invoke 経路）.

docs/03_test_spec.md「Domain Boundary Tests」。§1-2 のスコープ外宣言と §8 の error_code
を裏づける中核テスト。正典 PB とは別採番。

**本番 invoke 経路（Outer → Inner）で検証し、Inner State への fake injection は行わない。**
差し替えるのは外部サービス（① の LLM・③ のデータセット層）のみ。アサーション対象は
Outer に持ち上げられるフィールドに限る（§5-4）— `query_result` は持ち上げないため
invoke 経路から観測せずアサーションもしない（Inner 内の None 消去は ④ unit で検証済み）。
"""

import json

from tests.proof_of_boundary._cat2_harness import (
    APPROVED_SCHEMA,
    HAPPY_NORMALIZED,
    CredentialDataset,
    RecordingDataset,
    StubLLM,
    intake,
    make_graph,
    run,
)


def _run(normalized, request, *, dataset=None, schema=None):
    """LLM に *normalized* を返させ、本番 invoke 経路で *request* を処理する."""
    graph = make_graph(
        llm=StubLLM(normalized), dataset=dataset or RecordingDataset()
    )
    return run(graph, intake(request, schema or APPROVED_SCHEMA))


# --------------------------------------------------------------------- DB-1


def test_db_1_credential_in_dataset_version_blocks_output():
    """DB-1: データセット由来 credential が出所（dataset_version）に混入 → S3_BLOCKED.

    §7-3 条件②。Fake dataset が `dataset_version` に credential を含めて返す → 本物の
    ③ が除去して `[REDACTED:<type>]` を残す → 本物の ④ が引用/版に除去マーカーを検知して
    `blocked`。条件①（除去後も生 credential 残存）は本番経路から到達不能な fail-safe の
    ため ④ の unit テスト（`test_provenance_assemble_node.py`）で扱う。
    """
    result = _run(HAPPY_NORMALIZED, "2025-Q1 関東の訪日外客の訪問者数", dataset=CredentialDataset())
    assert result["error_code"] == "S3_BLOCKED"
    assert result["s3_clearance"] == "blocked"
    assert result["provenance_output"] is None
    # 成果物は公開しないが、生 credential も Outer 出力に一切現れない。
    assert CredentialDataset.CREDENTIAL not in json.dumps(result, ensure_ascii=False)


# --------------------------------------------------------------------- DB-2


def test_db_2_disclosure_restriction_cannot_be_suppressed():
    """DB-2: 開示ポリシー抵触は DISCLOSURE_RESTRICTED・抑制/回避できない.

    restricted 語（市区町村）を次元に載せると、他に曖昧・未承認があっても開示制限が
    最優先で立つ（② は disclosure を最初に・リクエスト全体で判定する）。
    """
    restricted = dict(HAPPY_NORMALIZED, region="市区町村")
    result = _run(restricted, "市区町村別の訪問者数")
    assert result["error_code"] == "DISCLOSURE_RESTRICTED"
    assert result["provenance_output"] is None
    assert result["exception_report"]


def test_db_2_disclosure_restriction_via_filter_is_not_evaded():
    """DB-2: restricted 語をフィルターに移しても回避できない（全 leaf 走査）."""
    via_filter = dict(HAPPY_NORMALIZED, filters={"level": "個人識別"})
    result = _run(via_filter, "個人識別レベルで")
    assert result["error_code"] == "DISCLOSURE_RESTRICTED"
    assert result["provenance_output"] is None


# --------------------------------------------------------------------- DB-3


def test_db_3_unsupported_metric_is_not_silently_substituted():
    """DB-3: 承認スキーマ外の指標は UNSUPPORTED_QUERY。暗黙の指標置換をしない.

    ② は normalized_query を書き戻さない（DB-3）ため、要求外の指標が承認済み指標に
    すり替えられていないことを normalized_query で確認する。
    """
    off_schema = dict(HAPPY_NORMALIZED, metric="消費額")
    result = _run(off_schema, "関東の消費額を")
    assert result["error_code"] == "UNSUPPORTED_QUERY"
    assert result["provenance_output"] is None
    # 指標がすり替えられていない（承認済みの「訪問者数」に化けていない）。
    normalized = json.loads(result["normalized_query"])
    assert normalized["metric"] == "消費額"


def test_db_3_ambiguous_dimension_is_reported_not_widened():
    """DB-3: 一意に決まらない次元（None）は AMBIGUOUS_QUERY・暗黙の拡大解釈をしない."""
    ambiguous = dict(HAPPY_NORMALIZED, period=None)
    result = _run(ambiguous, "関東の訪日外客の訪問者数")
    assert result["error_code"] == "AMBIGUOUS_QUERY"
    assert result["provenance_output"] is None


# --------------------------------------------------------------------- DB-4


def test_db_4_success_output_always_carries_provenance():
    """DB-4: 成功出力に必ずデータセット名・版・フィルター解釈が含まれ、引用なしの集計値を返さない.

    アサーション対象は Outer 持ち上げの provenance_output / dataset_version /
    formatted_output（= output）。
    """
    result = _run(HAPPY_NORMALIZED, "2025-Q1 関東の訪日外客の訪問者数")
    assert result["error_code"] is None
    prov = result["provenance_output"]
    assert prov
    # 集計値と出所（出典/版）とクエリ来歴が揃っている（引用なしの数値を返さない）。
    assert "集計値" in prov
    assert "出典" in prov or "データセット版" in prov
    assert "クエリ来歴" in prov
    assert result["dataset_version"]
    assert result["output"] == prov  # formatted_output → output（§3-1）


# --------------------------------------------------------------------- DB-5


def test_db_5_dataset_has_no_write_api_and_none_is_called():
    """DB-5: 書き込み・更新・削除の経路が存在せず、本番経路から write が 0 件.

    read-only 性は「書き込みメソッドを持たない」構造で保証される（§4-3）。本番
    `VisitorStatsDataset` の公開 API に write 系が無いことと、invoke 経路を通しても
    データセットへの write 呼び出しが 0 件であることを確認する。
    """
    from src.services.visitor_stats_dataset import VisitorStatsDataset

    public = [n for n in dir(VisitorStatsDataset) if not n.startswith("_")]
    for name in public:
        assert not any(
            w in name for w in ("insert", "update", "delete", "write", "upsert")
        ), f"read-only データセットに書き込み系メソッド {name} がある"

    recording = RecordingDataset()
    result = run(
        make_graph(llm=StubLLM(HAPPY_NORMALIZED), dataset=recording),
        intake("2025-Q1 関東の訪日外客の訪問者数", APPROVED_SCHEMA),
    )
    assert result["error_code"] is None  # 正常系を通した上で
    assert recording.write_calls == 0  # write は一度も起きていない


# --------------------------------------------------------------------- DB-6


def test_db_6_inner_node_order_is_correct_and_not_duplicated():
    """DB-6: Inner のノード実行順が設計どおりで重複がない.

    Inner 順序は `inner_node_history`（merge_output が持ち上げる）で検証する。Outer の
    `node_history` はバックボーン 5 ノードのみで Inner 順序を含まないため使わない。
    pack/unpack の対称性・PII/credential 0 件は純関数として `test_input_expand_node.py`
    で検証済み（本テストは invoke 経路の実行順担保に限る）。
    """
    result = _run(HAPPY_NORMALIZED, "2025-Q1 関東の訪日外客の訪問者数")
    inner_history = json.loads(result["inner_node_history"])
    assert inner_history == [
        "InputExpandNode",
        "RequestNormalizeNode",
        "QueryValidateNode",
        "ReadonlyQueryNode",
        "ProvenanceAssembleNode",
    ]
    # 重複がない（各ノードちょうど 1 回）。
    assert len(inner_history) == len(set(inner_history))


def test_db_6_intake_symmetry_carries_no_credential_on_the_wire():
    """DB-6: packed intake（sentinel+hex）が credential を運ばない（TC-03 と共有）.

    Outer.extract_input() が返す packed 文字列を本番経路で組み立て、
    `detect_credentials()` が 0 件であることを確認する（base64 の `eyJ` 回避が効く）。
    """
    from shared.security.credential_detector import detect_credentials

    from src.graph.graph import VisitorStatsQueryGraphNode

    user_input = intake(
        "2025-Q1 の関東における訪日外客の訪問者数（Foreign Visitors）", APPROVED_SCHEMA
    )
    packed = VisitorStatsQueryGraphNode(dataset=RecordingDataset()).extract_input(
        {"user_input": user_input}
    )
    assert not detect_credentials(packed)
