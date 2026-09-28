"""Issue #10 — PB-Cat2: Outer → Inner 受け渡し境界（本番 invoke 経路）.

docs/03_test_spec.md「PB-Cat2」および「PB-1」。

Cat 2 の Outer state は Inner に引き継がれない（`GraphNode.extract_input()` の戻り値が
Inner の `user_input` キーにしか載らない）。この境界は PB-1〜7 のどれにも該当しないため
独立して検証する。**投入は必ず 1 本の JSON を `Graph.invoke(..., VERIFIED_EXTERNAL)`**
に渡す本番経路のみ。差し替えるのは外部サービス（LLM・データセット層）だけで、State への
直接注入はしない（本番に無いデータ経路をテストのために新設しない）。

アサーション対象は Outer に持ち上がるフィールドに限る（§5-4）。`query_result` は
§5-4 により持ち上げないため invoke 経路から観測せず、アサーションもしない。
"""

import json

from framework.schemas.agent_status import AgentStatus

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


def _happy_graph(dataset=None):
    """正規化 JSON を返す LLM + 実データ委譲データセットで Outer を組む."""
    return make_graph(llm=StubLLM(HAPPY_NORMALIZED), dataset=dataset or RecordingDataset())


# ---------------------------------------------------------------- PB-C2-1 / C2-2


def test_pb_c2_1_intake_reaches_provenance_with_dataset_source():
    """PB-C2-1: intake が Inner に届き provenance 出力まで到達する.

    `inner_node_history` が Inner 5 ノード通過を示し、成果物にデータセット出所が載る。
    """
    result = run(
        _happy_graph(), intake("2025-Q1 の関東における訪日外客の訪問者数", APPROVED_SCHEMA)
    )
    assert result["status"] == AgentStatus.SUCCESS.value
    # Inner の 5 ノードを全て走破（Outer→Inner が 1 リクエストとして貫通）。
    assert json.loads(result["inner_node_history"]) == [
        "InputExpandNode",
        "RequestNormalizeNode",
        "QueryValidateNode",
        "ReadonlyQueryNode",
        "ProvenanceAssembleNode",
    ]
    # 成果物にデータセット出所（版・出典）が載る（DB-4 と対をなす）。
    assert result["provenance_output"]
    assert result["dataset_version"]
    assert "2025.03" in result["dataset_version"]
    assert result["output"]  # formatted_output → output ブリッジ（§3-1）


def test_pb_c2_2_domain_fields_cross_pack_unpack_boundary():
    """PB-C2-2: ドメイン 2 フィールドが pack/unpack（sentinel+hex）を越えて ① に届く.

    ① に正規化対象の自然言語リクエストと承認スキーマが届いていることを、① が実際に
    正規化して ②③④ が承認・集計・組立まで到達した事実（provenance 生成）で示す。
    S-2 の @final PII マスクで region/segment 名（Title Case 等）が壊れないことを含む。
    """
    llm = StubLLM(HAPPY_NORMALIZED)
    result = run(
        make_graph(llm=llm, dataset=RecordingDataset()),
        intake("Kanto Region の Foreign Visitors 数を 2025-Q1 で", APPROVED_SCHEMA),
    )
    # ① の LLM が呼ばれた = intake がマスクで壊れず ⓪→① に届いた。
    assert len(llm.calls) == 1
    # 承認スキーマがプロンプトに載っている = approved_schema も境界を越えた。
    prompt_text = json.dumps(llm.calls[0], ensure_ascii=False)
    assert "allowed_metrics" in prompt_text
    # ②③④ まで到達し成果物が出た（届いていなければ MISSING/INVALID で止まる）。
    assert result["error_code"] is None
    assert result["provenance_output"]


# ------------------------------------------------------------------- PB-C2-3


def test_pb_c2_3_outer_error_never_reaches_inner_llm():
    """PB-C2-3: Outer が ERROR（intake 不正）のとき Inner の外部サービス（LLM）に到達しない.

    `approved_schema` を欠いた intake は pack 時点で MISSING_INNER_INPUT になり、
    ⓪ が fail-closed で ①〜④ を素通り。LLM は一度も呼ばれず provenance は空。
    """
    llm = StubLLM(HAPPY_NORMALIZED)
    result = run(
        make_graph(llm=llm, dataset=RecordingDataset()),
        json.dumps({"analyst_request": "関東の訪問者数"}, ensure_ascii=False),
    )
    assert result["error_code"] == "MISSING_INNER_INPUT"
    assert llm.calls == []  # LLM 未呼び出し（外部サービスに到達しない）
    assert result["provenance_output"] is None


# ------------------------------------------------------------------- PB-C2-4


def test_pb_c2_4_inner_exception_report_returns_to_outer_output():
    """PB-C2-4: Inner の例外記録（サポート外）が Outer の出力（get_output）に現れる.

    承認スキーマ外の指標は ② が UNSUPPORTED_QUERY を立て、`error_code` /
    `exception_report` が Outer 出力に持ち上がる（§5-4）。
    """
    unsupported = dict(HAPPY_NORMALIZED, metric="消費額")
    result = run(
        make_graph(llm=StubLLM(unsupported), dataset=RecordingDataset()),
        intake("関東の消費額を", APPROVED_SCHEMA),
    )
    assert result["error_code"] == "UNSUPPORTED_QUERY"
    assert result["exception_report"]
    # formatted_output に exception_report が載る（provenance が無いとき・§5-4）。
    assert result["output"] == result["exception_report"]
    assert result["provenance_output"] is None


# ------------------------------------------------------------------- PB-C2-5


def test_pb_c2_5_s3_block_propagates_to_outer_unswallowed():
    """PB-C2-5: S-3 ブロック（DB-1）が握り潰されずに Outer へ戻る.

    データセット由来 credential が `dataset_version` に混入 → ③ 除去 → ④ が spine で
    検知して blocked。`error_code=S3_BLOCKED` / `s3_clearance=blocked` /
    `provenance_output is None` が Outer 出力に現れる。
    """
    result = run(
        make_graph(llm=StubLLM(HAPPY_NORMALIZED), dataset=CredentialDataset()),
        intake("2025-Q1 関東の訪日外客の訪問者数", APPROVED_SCHEMA),
    )
    assert result["error_code"] == "S3_BLOCKED"
    assert result["s3_clearance"] == "blocked"
    assert result["provenance_output"] is None
    # ブロック理由のレポートは残る（成果物公開はしないが監査可能性は保つ）。
    assert result["exception_report"]


# ------------------------------------------------------------------- PB-C2-6


def test_pb_c2_6_llm_failure_records_normalize_error_not_no_candidates():
    """PB-C2-6: LLM 障害が「候補なし」に倒れず記録される.

    `status=SUCCESS` かつ `NORMALIZE_ERROR`（記録を Outer へ届けるため）。Inner が
    ERROR を返すと merge_output がスキップされ記録が消える（§7-6）ので、SUCCESS + code
    でなければならない。
    """

    class _BoomLLM:
        def __init__(self) -> None:
            self.calls: list = []

        def complete(self, messages):
            self.calls.append(messages)
            raise RuntimeError("llm down")

    boom = _BoomLLM()
    result = run(
        make_graph(llm=boom, dataset=RecordingDataset()),
        intake("関東の訪問者数", APPROVED_SCHEMA),
    )
    assert boom.calls  # LLM には到達している（障害はその先で起きた）
    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["error_code"] == "NORMALIZE_ERROR"
    assert result["provenance_output"] is None


# ----------------------------------------------------------------------- PB-1


def test_pb_1_audit_events_fire_across_the_full_invoke_path(caplog):
    """PB-1: `emit_trace_event()` が全 invocation 経路で発火する（TC-05 と対をなす）.

    正常系の本番 invoke で、Inner 各ノードのドメインイベントが監査ロガー
    `agentcore.audit` に現れることを falsifiable に確認する（emit を消すと FAIL）。
    監査 payload に credential を載せないこと（DB-1 の credential も出さない）も見る。
    """
    import logging

    with caplog.at_level(logging.INFO, logger="agentcore.audit"):
        run(_happy_graph(), intake("2025-Q1 関東の訪日外客の訪問者数", APPROVED_SCHEMA))

    audit_text = "\n".join(
        r.getMessage() for r in caplog.records if r.name == "agentcore.audit"
    )
    # Inner 5 ノードのドメインイベントが全て出ている。
    for event in (
        "intake_expanded",
        "request_normalized",
        "query_validated",
        "readonly_queried",
        "provenance_assembled",
    ):
        assert event in audit_text, f"監査イベント {event} が発火していない"
    # 監査ログに credential 形状を出さない（S-4・§7-4）。
    assert CredentialDataset.CREDENTIAL not in audit_text
