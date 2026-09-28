"""Inner ③ readonly_query — docs/02_design.md §8, §7-3, §4-3.

このノードが担う 4 つの性質、重要な順に:

1. 上流エラーは fail-closed で継承する。② が UNSUPPORTED_QUERY を出したクエリを
   データセットに投げてはならない。error_code が立っていたらデータセットに触れず
   そのまま carry する。
2. データセット由来文字列は delta に載せる前に detect_credentials() で除去する
   （§7-3 の「適用ノード」規約）。除去した痕跡は [REDACTED:<type>] として残り、
   ④ が S-3 で検知できる。除去は raise せず値で運ぶ。
3. データセット参照不能は例外ではなく DATASET_UNAVAILABLE として運ぶ（§7-6・§8）。
   VisitorStatsDataset.query() が available=False を返したら error_code に変換し、
   exception_report を残す。node の execute() は決して raise しない。
4. status は常に SUCCESS。異常は error_code + exception_report で運ぶ（§7-6:
   raise / ERROR は GraphNode に merge_output() を飛ばさせ記録が消える）。
"""
import json

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.security.credential_detector import detect_credentials

from src.nodes.readonly_query_node import (
    ERROR_DATASET_UNAVAILABLE,
    ReadonlyQueryNode,
    build_exception_report,
)

VALID_QUERY = {
    "period": "2025-Q1",
    "region": "関東",
    "segment": "訪日外客",
    "metric": "訪問者数",
    "filters": None,
}

VALID_RESULT = json.dumps({"valid": True, "reason": "ok"}, ensure_ascii=False)


class FakeDataset:
    """VisitorStatsDataset の差し替え。query() が返す dict をテストが決める（§4-3 注入）."""

    def __init__(self, result):
        self._result = result
        self.query_calls = []

    def load_manifest(self):
        return {"available": True, "version": "2025.03", "ingested_date": "2025-04-10"}

    def query(self, normalized_query):
        self.query_calls.append(dict(normalized_query))
        return dict(self._result)


def _matched(value=12_840_000, version="2025.03", citation="JNTO 2025年3月"):
    return {
        "available": True,
        "matched": True,
        "value": value,
        "unit": "人",
        "citation": citation,
        "dataset_version": version,
        "ingested_date": "2025-04-10",
    }


def _state(query=VALID_QUERY, validation_result=VALID_RESULT, **extra):
    state = {
        "normalized_query": None if query is None else json.dumps(query, ensure_ascii=False),
        "validation_result": validation_result,
        "error_code": None,
        "exception_report": None,
        "session_id": "s",
        "correlation_id": "c",
        "thread_id": "t",
        "trace_id": "tr",
    }
    state.update(extra)
    return state


def gated(node, state):
    """execute() を framework の @final セキュリティゲート越しに呼ぶ（__call__ と同じ経路）."""
    return node._security_gate_output(node.execute(node._security_gate_input(state)))


# ------------------------------------------------------------------ 正常系


def test_matched_query_lifts_result_and_version():
    node = ReadonlyQueryNode(dataset=FakeDataset(_matched()))
    r = gated(node, _state())
    assert r["status"] == AgentStatus.SUCCESS.value
    assert r["error_code"] is None
    qr = json.loads(r["query_result"])
    assert qr["value"] == 12_840_000
    assert qr["unit"] == "人"
    assert r["dataset_version"] == "2025.03（取り込み: 2025-04-10）" or "2025.03" in r["dataset_version"]


def test_query_is_delta_only_not_whole_state():
    """operator.add チャネル（node_history 等）を {**state} で再連結しない（§6）."""
    node = ReadonlyQueryNode(dataset=FakeDataset(_matched()))
    r = gated(node, _state(node_history=["x"], approved_schema="keep"))
    assert "node_history" not in r
    assert "approved_schema" not in r
    assert "session_id" not in r


def test_dataset_receives_the_normalized_query():
    fake = FakeDataset(_matched())
    node = ReadonlyQueryNode(dataset=fake)
    gated(node, _state())
    assert fake.query_calls == [VALID_QUERY]


# --------------------------------------------------- 上流エラーは fail-closed


def test_upstream_error_is_carried_and_dataset_not_touched():
    fake = FakeDataset(_matched())
    node = ReadonlyQueryNode(dataset=fake)
    r = gated(
        node,
        _state(
            error_code="UNSUPPORTED_QUERY",
            validation_result=json.dumps({"valid": False, "reason": "no"}),
            exception_report="## 既存レポート\nUNSUPPORTED",
        ),
    )
    assert r["error_code"] == "UNSUPPORTED_QUERY"
    assert fake.query_calls == [], "上流エラー時はデータセットに触れてはならない"
    assert r.get("query_result") is None
    assert r["status"] == AgentStatus.SUCCESS.value


def test_upstream_report_is_not_overwritten():
    node = ReadonlyQueryNode(dataset=FakeDataset(_matched()))
    r = gated(
        node,
        _state(
            error_code="AMBIGUOUS_QUERY",
            exception_report="## 既存レポート（② が書いた）",
        ),
    )
    # 上流の report を上書きしない（検出したノードの帰属を壊さない）。
    assert r.get("exception_report", "既存") == "既存" or "exception_report" not in r


def test_invalid_validation_result_is_fail_closed():
    """validation_result が valid=False なのに error_code 未設定の異常は fail-closed."""
    fake = FakeDataset(_matched())
    node = ReadonlyQueryNode(dataset=fake)
    r = gated(node, _state(validation_result=json.dumps({"valid": False, "reason": "x"})))
    assert r["error_code"] is not None
    assert fake.query_calls == []


# ------------------------------------------------- データセット参照不能（§7-6）


def test_dataset_unavailable_becomes_error_code_not_raise():
    node = ReadonlyQueryNode(dataset=FakeDataset({"available": False}))
    r = gated(node, _state())
    assert r["error_code"] == ERROR_DATASET_UNAVAILABLE
    assert r["status"] == AgentStatus.SUCCESS.value
    assert r.get("query_result") is None
    assert r["exception_report"]


def test_no_match_becomes_error_code_not_raise():
    node = ReadonlyQueryNode(dataset=FakeDataset({"available": True, "matched": False}))
    r = gated(node, _state())
    assert r["error_code"] is not None
    assert r["status"] == AgentStatus.SUCCESS.value


def test_dataset_raising_is_swallowed_into_error_code():
    """万一 dataset が raise しても node は raise せず error_code 化する（§7-6 二重防御）."""

    class Boom:
        def load_manifest(self):
            return {"available": True}

        def query(self, q):
            raise RuntimeError("db exploded")

    node = ReadonlyQueryNode(dataset=Boom())
    r = gated(node, _state())
    assert r["status"] == AgentStatus.SUCCESS.value
    assert r["error_code"] is not None


# ------------------------------------------------ S-3 除去（§7-3 適用ノード）


def test_credential_in_dataset_value_is_redacted_before_delta():
    """データセット由来文字列に credential が混じったら [REDACTED:*] へ除去して載せる."""
    poisoned = _matched(citation="接続先 mysql://user:" + "a" * 40 + "@db/host")
    node = ReadonlyQueryNode(dataset=FakeDataset(poisoned))
    r = gated(node, _state())
    # query_result / dataset_version のどこにも生 credential を残さない。
    blob = json.dumps(r, ensure_ascii=False)
    assert not detect_credentials(blob), "delta に生 credential が残っている"
    assert "[REDACTED:" in blob, "除去痕跡 [REDACTED:*] が残っていない（④ が S-3 検知できない）"


def test_redaction_does_not_lose_the_report_channel():
    """除去が起きても status=SUCCESS を維持し、記録を失わない（§7-6）."""
    poisoned = _matched(citation="Bearer\t" + "A" * 20)
    node = ReadonlyQueryNode(dataset=FakeDataset(poisoned))
    r = gated(node, _state())
    assert r["status"] == AgentStatus.SUCCESS.value


def test_clean_result_has_no_redaction_marker():
    node = ReadonlyQueryNode(dataset=FakeDataset(_matched()))
    r = gated(node, _state())
    assert "[REDACTED:" not in json.dumps(r, ensure_ascii=False)


def test_query_result_stays_parseable_after_newline_reform_redaction():
    """Issue #4 shape: leaf is clean, but json.dumps re-forms a conn_string across
    an escaped newline. query_result must stay valid JSON so ④ can read it (§7-3③)."""
    # `mysql://ab` + real newline: the leaf's `://ab` run is < 8 chars, so the
    # per-leaf pass finds nothing; only after json.dumps escapes the newline into
    # the two non-space chars \\n does the greedy conn_string run appear.
    poisoned = _matched(citation="mysql://ab\ncdefghijklmnopqrstuv")
    node = ReadonlyQueryNode(dataset=FakeDataset(poisoned))
    r = gated(node, _state())
    assert r["status"] == AgentStatus.SUCCESS.value
    parsed = json.loads(r["query_result"])  # must not raise
    assert parsed["value"] == 12_840_000
    assert not detect_credentials(json.dumps(r, ensure_ascii=False))


def test_credential_in_non_citation_leaf_is_also_redacted():
    """除去は全 string leaf を対象にする（citation だけでない）.

    `unit` は同梱データセットでは `人`/`人泊` に制約されるが、除去の網羅性は
    「どの leaf でも生 credential を delta に残さない」で担保すべきで、text leaf の
    ハンドリスト（citation のみ）に依存してはならない。unit 側に credential 形が
    入っても除去痕跡が残り、生 credential は残らないことを固定する。"""
    poisoned = _matched()
    poisoned["unit"] = "mysql://" + "a" * 40 + "@db/host"
    node = ReadonlyQueryNode(dataset=FakeDataset(poisoned))
    r = gated(node, _state())
    assert r["status"] == AgentStatus.SUCCESS.value
    blob = json.dumps(r, ensure_ascii=False)
    assert not detect_credentials(blob), "unit 由来の生 credential が delta に残っている"
    assert "[REDACTED:" in blob
    # query_result はパース可能なまま（④ が再スキャンできる・§7-3③）。
    json.loads(r["query_result"])


# --------------------------------------------------------- 契約・S-4・trust


def test_required_trust_level_is_verified_external():
    assert ReadonlyQueryNode(dataset=FakeDataset(_matched())).required_trust_level == (
        TrustLevel.VERIFIED_EXTERNAL
    )


def test_audit_log_carries_no_analyst_values():
    """S-4 監査ログにアナリスト値・生 credential を含めない（§7-4）."""
    poisoned = _matched(citation="mysql://u:" + "z" * 40 + "@h/d", value=999)
    node = ReadonlyQueryNode(dataset=FakeDataset(poisoned))
    r = gated(node, _state())
    audit = r.get("audit_log")
    if audit:
        assert not detect_credentials(audit)
        assert "999" not in audit  # 生の集計値を監査に出さない


def test_node_never_raises_on_garbage_normalized_query():
    node = ReadonlyQueryNode(dataset=FakeDataset(_matched()))
    r = gated(node, _state(query=None, validation_result=VALID_RESULT))
    # normalized_query が無いのに valid=True の矛盾 → fail-closed、raise しない。
    assert r["status"] == AgentStatus.SUCCESS.value
    assert r["error_code"] is not None


def test_build_exception_report_is_reviewable_markdown():
    report = build_exception_report(ERROR_DATASET_UNAVAILABLE, "データセット未取り込み")
    assert ERROR_DATASET_UNAVAILABLE in report
    assert "データセット未取り込み" in report
    assert report.lstrip().startswith("#")
