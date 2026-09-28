"""Issue #11 — PB-3（実データ取得）と PB-1 残分岐（監査イベントの網羅性）.

docs/03_test_spec.md「PB-3」および「PB-1」の未消化分。#10 の PB-Cat2 / DB-1〜6 と
同じく **本番 invoke 経路（`Graph().invoke()` + VERIFIED_EXTERNAL）のみ**で検証する。
差し替えるのは外部サービス（① の LLM）だけで、③ のデータセットは実ファイル委譲の
`RecordingDataset`（本物の `VisitorStatsDataset` に委譲）を使う。Inner State への
fake injection はしない（[[feedback-pb-no-fake-injection]]）。

- **PB-3**: 承認済みデータセットからの取得が「スタブ値」ではなく `data/visitor_stats/`
  の実ファイル由来であることを、成果物に載る集計値・出典・版・取り込み日で示す。
  §1-2 の外部 HTTP / ベクトル DB 非使用宣言により、外部サービス→承認済みデータセット層
  （実ファイル読み取り）の読み替えで PB-3 を充足する（免除ではない）。
- **PB-1 残分岐**: 監査ロガー `agentcore.audit` の node ライフサイクルが全経路で
  破綻しない（`node_start` は必ず `node_complete` で閉じる＝ノードが途中で落ちない）こと、
  および **エラー経路でも Inner のドメインイベントが欠落しない**（監査証跡に穴が空かない）
  ことを falsifiable に確認する。#10 の PB-1 は正常系のドメインイベント発火を見たが、
  こちらはペアリング健全性とエラー経路の網羅性という別分岐を担う。
"""

import json
import logging

from tests.proof_of_boundary._cat2_harness import (
    APPROVED_SCHEMA,
    HAPPY_NORMALIZED,
    RecordingDataset,
    StubLLM,
    intake,
    make_graph,
    run,
)


def _happy_graph():
    """正規化 JSON を返す LLM + 実ファイル委譲データセットで Outer を組む."""
    return make_graph(llm=StubLLM(HAPPY_NORMALIZED), dataset=RecordingDataset())


def _capture_audit(graph, user_input):
    """本番 invoke を回し、`agentcore.audit` の (event_type, payload.node) 列を返す."""

    class _Cap(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.messages: list[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.messages.append(record.getMessage())

    logger = logging.getLogger("agentcore.audit")
    prev_level = logger.level
    logger.setLevel(logging.INFO)
    handler = _Cap()
    logger.addHandler(handler)
    try:
        result = run(graph, user_input)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)

    events: list[tuple[str, object]] = []
    for message in handler.messages:
        try:
            record = json.loads(message)
        except (ValueError, TypeError):
            continue
        events.append((record.get("event_type"), record.get("payload", {}).get("node")))
    return result, events


# ----------------------------------------------------------------------- PB-3


def test_pb_3_success_values_come_from_real_dataset_files_not_stubs():
    """PB-3: 成果物の集計値・出典・版が `data/visitor_stats/` 実ファイル由来である.

    LLM スタブは正規化次元（期間/地域/セグメント/指標）しか供給しない。集計値・出典・
    データセット版・取り込み日は ③ が承認済みデータセット層を読み取って初めて決まる。
    実ファイル `rows.json`（value=12840000・unit=人・citation=JNTO…）と
    `manifest.json`（version=2025.03・ingested=2025-04-10）の値が成果物に載れば、
    スタブではなく実データ取得が本番経路で成立している証拠になる。
    """
    result = run(_happy_graph(), intake("2025-Q1 関東の訪日外客の訪問者数", APPROVED_SCHEMA))
    assert result["error_code"] is None

    prov = result["provenance_output"]
    assert prov
    # 実ファイル rows.json 由来の集計値と出典（LLM スタブは供給しない値）。
    assert "12,840,000" in prov, "実ファイルの集計値が成果物に載っていない"
    assert "JNTO 訪日外客統計 2025年3月" in prov, "実ファイルの出典が成果物に載っていない"
    # 実ファイル manifest.json 由来の版・取り込み日。
    assert "2025.03" in result["dataset_version"]
    assert "2025-04-10" in result["dataset_version"], "manifest の取り込み日が載っていない"


def test_pb_3_dataset_reads_go_through_the_real_delegate():
    """PB-3: データセット読み取りが実委譲（本物の VisitorStatsDataset）を通っている.

    本物の `VisitorStatsDataset` に委譲しつつ read（load_manifest / query）呼び出しを
    数える薄いラッパを本番シーム（`dataset=`）に差し込む。本番経路を通すと読み取りが
    1 回以上発生し、その戻り値が実ファイルの manifest 値と一致することを確認する
    （スタブ値の差し替えでは一致しない）。共有ハーネスは変更せずローカルに定義する。
    """
    from src.services.visitor_stats_dataset import DEFAULT_DATASET_ROOT, VisitorStatsDataset

    class _CountingDataset:
        """実 `VisitorStatsDataset` へ委譲し read 回数を数えるだけの薄いラッパ."""

        def __init__(self, root: str) -> None:
            self._inner = VisitorStatsDataset(root)
            self.read_calls = 0

        def load_manifest(self):
            self.read_calls += 1
            return self._inner.load_manifest()

        def query(self, normalized_query):
            self.read_calls += 1
            return self._inner.query(normalized_query)

    counting = _CountingDataset(DEFAULT_DATASET_ROOT)
    result = run(
        make_graph(llm=StubLLM(HAPPY_NORMALIZED), dataset=counting),
        intake("2025-Q1 関東の訪日外客の訪問者数", APPROVED_SCHEMA),
    )
    assert result["error_code"] is None
    # 実委譲を通じて読み取りが発生している（委譲先が呼ばれていなければ 0 のまま）。
    assert counting.read_calls >= 1, "承認済みデータセットへの読み取りが発生していない"

    # 委譲先が返す版が実ファイルの manifest 値と一致する（スタブなら別値になる）。
    real_version = VisitorStatsDataset(DEFAULT_DATASET_ROOT).load_manifest()["version"]
    assert real_version in result["dataset_version"]


# ----------------------------------------------------------------------- PB-1


def test_pb_1_every_node_start_is_closed_by_a_node_complete():
    """PB-1: 監査上、開始した全ノードが完了で閉じる（途中で落ちるノードが無い）.

    `base_node.__call__` は成功時 `node_complete`、例外時 `node_error` を出す。正常系の
    本番 invoke で `node_start` を出したノードは全て `node_complete` で閉じ、`node_error`
    や取りこぼし（開いたまま閉じない）が無いことを確認する。ライフサイクルの穴＝ノードの
    異常終了を検知する（監査証跡が破綻していないことの担保）。
    """
    _result, events = _capture_audit(
        _happy_graph(), intake("2025-Q1 関東の訪日外客の訪問者数", APPROVED_SCHEMA)
    )
    starts = [node for etype, node in events if etype == "node_start"]
    completes = [node for etype, node in events if etype == "node_complete"]
    errors = [node for etype, node in events if etype == "node_error"]

    assert starts, "node_start が 1 件も無い（監査が動いていない）"
    # 正常系ではエラーイベントは出ない。
    assert errors == []
    # 開始した各ノードが完了で閉じる（多重度も含めて一致）。
    assert sorted(starts) == sorted(completes), (
        f"node_start と node_complete が対応しない: starts={sorted(starts)} "
        f"completes={sorted(completes)}"
    )


def test_pb_1_error_path_still_emits_full_inner_audit_trail():
    """PB-1: エラー経路でも Inner のドメインイベントが欠落しない（監査に穴が空かない）.

    LLM 障害（NORMALIZE_ERROR）で終わる本番 invoke でも、Inner は fail-safe で ①〜④ を
    通過して記録を残す（§7-6：Inner が ERROR を返すと merge がスキップされ記録が消えるため、
    ドメインイベントは通過時に必ず出す設計）。正常系と同じ 5 ドメインイベントが監査ログに
    揃うことを確認する（emit を消すと FAIL する falsifiable な検証）。
    """

    class _BoomLLM:
        def __init__(self) -> None:
            self.calls: list = []

        def complete(self, messages):
            self.calls.append(messages)
            raise RuntimeError("llm down")

    boom = _BoomLLM()
    result, events = _capture_audit(
        make_graph(llm=boom, dataset=RecordingDataset()),
        intake("関東の訪問者数", APPROVED_SCHEMA),
    )
    # 障害経路であることを確認（LLM には到達し、その先で失敗した）。
    assert boom.calls
    assert result["error_code"] == "NORMALIZE_ERROR"

    domain = {etype for etype, _node in events}
    for event in (
        "intake_expanded",
        "request_normalized",
        "query_validated",
        "readonly_queried",
        "provenance_assembled",
    ):
        assert event in domain, f"エラー経路で監査イベント {event} が欠落している"
    # ライフサイクルも破綻しない（開始が完了で閉じる）。
    starts = sorted(node for etype, node in events if etype == "node_start")
    completes = sorted(node for etype, node in events if etype == "node_complete")
    assert starts == completes
