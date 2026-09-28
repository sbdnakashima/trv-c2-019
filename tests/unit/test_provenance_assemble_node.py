"""Inner ④ provenance_assemble — docs/02_design.md §7-3, §7-6, §8, DB-1.

④ は Inner の最後のノード。③ が読み取った集計値・データセット版・②/① の
正規化クエリを受け取り、来歴つき Markdown（provenance_output）に組み立てる。
同時に S-3 出力ゲートの「判定ノード」でもある。

このノードが担う 4 つの性質、重要な順に:

1. status は常に SUCCESS。異常は error_code + exception_report で運ぶ（§7-6:
   raise / ERROR は GraphNode に merge_output() を飛ばさせ記録が消える）。
   framework の @final S-3 ゲート（_security_gate_output）は delta 内の文字列に
   detect_credentials が当たると RuntimeError を上げるので、④ が返す delta は
   その時点で credential-free でなければならない。

2. 上流エラーは fail-closed で継承する。error_code が立っていたら来歴を組み立てず
   そのまま carry する（開示制限クエリの結果を Markdown 化して外に出さない）。

3. S-3 判定（§7-3）。除去マーカー [REDACTED:*] が dataset_version / citation /
   集計値に到達している（= ③ がデータセットの credential を除去した痕跡が
   来歴の根幹に食い込んでいる）なら blocked にする。blocked では
   provenance_output と query_result を None で上書きし（キー省略では
   last-write-wins チャネルに ③ の値が残る）、s3_clearance="blocked"、
   error_code="S3_BLOCKED"、exception_report を残す。検出文字列は引用しない
   （引用すると @final ゲートに当たって報告ごと消える）。

4. 除去マーカーが自由記述部分だけにある / まったく無い正常系では出力を返す
   （redacted / clear）。s3_clearance はこの 3 値（clear/redacted/blocked）。
"""
import json

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.security.credential_detector import detect_credentials

from src.nodes.provenance_assemble_node import (
    ERROR_S3_BLOCKED,
    ProvenanceAssembleNode,
    build_exception_report,
)

VALID_QUERY = {
    "period": "2025-Q1",
    "region": "関東",
    "segment": "訪日外客",
    "metric": "訪問者数",
    "filters": None,
}

QUERY_RESULT = json.dumps(
    {"value": 12_840_000, "unit": "人", "citation": "JNTO 2025年3月"},
    ensure_ascii=False,
)


def _state(
    query_result=QUERY_RESULT,
    dataset_version="2025.03（取り込み: 2025-04-10）",
    normalized_query=VALID_QUERY,
    error_code=None,
    exception_report=None,
    **extra,
):
    state = {
        "query_result": query_result,
        "dataset_version": dataset_version,
        "normalized_query": None
        if normalized_query is None
        else json.dumps(normalized_query, ensure_ascii=False),
        "error_code": error_code,
        "exception_report": exception_report,
        "session_id": "s",
        "correlation_id": "c",
        "thread_id": "t",
        "trace_id": "tr",
    }
    state.update(extra)
    return state


def gated(node, state):
    """execute() を framework の @final S-3 ゲート越しに呼ぶ（__call__ と同じ経路）."""
    return node._security_gate_output(node.execute(node._security_gate_input(state)))


# ------------------------------------------------------------------ 正常系（clear）


def test_matched_result_builds_provenance_markdown():
    r = gated(ProvenanceAssembleNode(), _state())
    assert r["status"] == AgentStatus.SUCCESS.value
    assert r["error_code"] is None
    assert r["s3_clearance"] == "clear"
    md = r["provenance_output"]
    assert md.lstrip().startswith("#")
    assert "12,840,000" in md or "12840000" in md  # 集計値が本文に出る
    assert "人" in md
    assert "JNTO 2025年3月" in md  # 出典
    assert "2025.03" in md  # データセット版
    # 4 次元の来歴が読める（期間/地域/セグメント/指標）。
    assert "2025-Q1" in md
    assert "関東" in md
    assert "訪問者数" in md


def test_provenance_is_delta_only_not_whole_state():
    """operator.add チャネル（node_history 等）を {**state} で再連結しない（§6）."""
    r = gated(ProvenanceAssembleNode(), _state(node_history=["x"], approved_schema="keep"))
    assert "node_history" not in r
    assert "approved_schema" not in r
    assert "session_id" not in r


def test_query_result_is_not_lifted_to_outer():
    """§5-4: ④ は raw な query_result を Outer に持ち上げない（生値漏洩防止）.

    正常系でも query_result を delta に再掲しない。Outer が受け取るのは来歴つき
    provenance_output だけ。"""
    r = gated(ProvenanceAssembleNode(), _state())
    assert "query_result" not in r or r.get("query_result") is None


def test_clear_result_has_no_s3_blocked():
    r = gated(ProvenanceAssembleNode(), _state())
    assert r["s3_clearance"] == "clear"
    assert r["error_code"] is None


# --------------------------------------------------- 上流エラーは fail-closed


def test_upstream_error_is_carried_and_no_provenance_built():
    r = gated(
        ProvenanceAssembleNode(),
        _state(
            error_code="DISCLOSURE_RESTRICTED",
            query_result=None,
            exception_report="## 既存レポート\nDISCLOSURE_RESTRICTED",
        ),
    )
    assert r["error_code"] == "DISCLOSURE_RESTRICTED"
    assert r["status"] == AgentStatus.SUCCESS.value
    # 来歴は組み立てない。
    assert r.get("provenance_output") is None


def test_upstream_report_is_not_overwritten():
    r = gated(
        ProvenanceAssembleNode(),
        _state(error_code="UNSUPPORTED_QUERY", exception_report="## 既存レポート（② が書いた）"),
    )
    # 上流の report を上書きしない（検出したノードの帰属を壊さない）。
    assert r.get("exception_report", "既存") == "既存" or "exception_report" not in r


# ------------------------------------------------- S-3 判定: blocked（§7-3・DB-1）


def test_redaction_marker_in_dataset_version_blocks():
    """DB-1 本番発火経路: ③ がデータセット版の credential を除去して [REDACTED:*] を
    残した → 来歴の根幹（dataset_version）に痕跡が食い込む → blocked."""
    r = gated(
        ProvenanceAssembleNode(),
        _state(dataset_version="2025.03 [REDACTED:conn_string]（取り込み: 2025-04-10）"),
    )
    assert r["error_code"] == ERROR_S3_BLOCKED
    assert r["s3_clearance"] == "blocked"
    assert r["status"] == AgentStatus.SUCCESS.value
    # 出力は放棄する（キー省略ではなく None で上書き — last-write-wins チャネル対策）。
    assert r["provenance_output"] is None
    assert r["query_result"] is None
    # 記録は残す。
    assert r["exception_report"]
    # 検出文字列を引用しない（引用すると @final ゲートに当たって報告ごと消える）。
    assert "conn_string" not in r["exception_report"] or "[REDACTED:" not in r["exception_report"]


def test_redaction_marker_in_citation_blocks():
    """出典（citation）に除去痕跡が食い込んでいる → 来歴の根幹なので blocked."""
    qr = json.dumps(
        {"value": 12_840_000, "unit": "人", "citation": "JNTO [REDACTED:api_key]"},
        ensure_ascii=False,
    )
    r = gated(ProvenanceAssembleNode(), _state(query_result=qr))
    assert r["error_code"] == ERROR_S3_BLOCKED
    assert r["s3_clearance"] == "blocked"
    assert r["provenance_output"] is None
    assert r["query_result"] is None


def test_blocked_output_carries_no_credential_and_survives_final_gate():
    """blocked の delta は @final S-3 ゲート（detect_credentials）を通過できる.

    RuntimeError が上がったらこのテストは gated() の時点で落ちる。exception_report・
    audit に生 credential も検出文字列も載せていないことを保証する。"""
    r = gated(
        ProvenanceAssembleNode(),
        _state(dataset_version="2025.03 [REDACTED:conn_string]"),
    )
    blob = json.dumps(r, ensure_ascii=False)
    assert not detect_credentials(blob)


# ------------------------------------------------- S-3 判定: blocked（条件③ 値欠落）


def test_missing_aggregate_value_blocks():
    """除去が集計値そのものを失わせた（value が無い）→ 来歴が空虚になるので blocked."""
    qr = json.dumps({"unit": "人", "citation": "JNTO 2025年3月"}, ensure_ascii=False)
    r = gated(ProvenanceAssembleNode(), _state(query_result=qr))
    assert r["error_code"] == ERROR_S3_BLOCKED
    assert r["s3_clearance"] == "blocked"
    assert r["provenance_output"] is None


# ------------------------------------------------- S-3 判定: blocked（条件① fail-safe）


def test_raw_credential_residue_blocks_fail_safe():
    """本番到達不能な fail-safe: 万一 delta 組立前に生 credential 形が残っていたら
    detect_credentials 再スキャンで検知して blocked にする（unit で直接検証）.

    ③ が正しく除去していればここは通らないが、④ は自分で再スキャンして防ぐ。"""
    poisoned = json.dumps(
        {
            "value": 12_840_000,
            "unit": "人",
            "citation": "接続先 mysql://user:" + "a" * 40 + "@db/host",
        },
        ensure_ascii=False,
    )
    r = gated(ProvenanceAssembleNode(), _state(query_result=poisoned))
    assert r["error_code"] == ERROR_S3_BLOCKED
    assert r["s3_clearance"] == "blocked"
    assert r["provenance_output"] is None
    # @final ゲートを通過する（生 credential を delta に残さない）。
    assert not detect_credentials(json.dumps(r, ensure_ascii=False))


# ------------------------------------------------- S-3 判定: redacted（自由記述のみ）


def test_marker_only_in_free_text_note_is_redacted_not_blocked():
    """除去痕跡が自由記述（正規化クエリの filters 説明など）だけにあり、来歴の根幹
    （値/出典/版）には無い場合は出力を返す（redacted）."""
    q = dict(VALID_QUERY)
    q["filters"] = {"note": "補足 [REDACTED:token] を含む自由記述"}
    r = gated(ProvenanceAssembleNode(), _state(normalized_query=q))
    assert r["s3_clearance"] == "redacted"
    assert r["error_code"] is None
    assert r["provenance_output"]  # 出力は返す
    assert r["status"] == AgentStatus.SUCCESS.value


# --------------------------------------------------------- 契約・S-4・trust


def test_required_trust_level_is_verified_external():
    assert ProvenanceAssembleNode().required_trust_level == TrustLevel.VERIFIED_EXTERNAL


def test_node_never_raises_on_garbage_query_result():
    """query_result が壊れた JSON でも raise せず error_code 化する（§7-6）."""
    r = gated(ProvenanceAssembleNode(), _state(query_result="{not json"))
    assert r["status"] == AgentStatus.SUCCESS.value
    assert r["error_code"] is not None


def test_audit_log_carries_no_analyst_values():
    """S-4 監査ログにアナリスト値・生 credential を含めない（§7-4）."""
    r = gated(ProvenanceAssembleNode(), _state())
    audit = r.get("audit_log")
    if audit:
        assert not detect_credentials(audit)
        assert "12840000" not in audit and "12,840,000" not in audit


def test_build_exception_report_is_reviewable_markdown():
    report = build_exception_report(ERROR_S3_BLOCKED, "S-3 出力ゲートで来歴を放棄")
    assert ERROR_S3_BLOCKED in report
    assert "S-3 出力ゲートで来歴を放棄" in report
    assert report.lstrip().startswith("#")
