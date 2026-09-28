"""VisitorStatsDataset — 承認済み JNTO 統計データセットへの読み取り専用アクセス層（docs/02_design.md §4-3）.

このテストが固定する契約:

*読み取り専用性は API 形状で構造的に保証する（DB-5）。* insert / update / delete /
write / upsert に相当する public メソッドが 1 つでも生えたら失敗する。設計上、
このクラスがデータを書き換える手段を持たないことがテストで担保される。

*参照不能は例外ではなく戻り値で表現する（§7-6）。* dataset_root が存在しない・
manifest が壊れている場合に `query()` / `load_manifest()` は raise せず、
`available` フラグ False を含む dict を返す。ノード ③ がこれを
`DATASET_UNAVAILABLE` に変換できるようにするため。

*manifest はメモ化する（§4-3）。* 同一インスタンスで `load_manifest()` を複数回
呼んでもディスクは 1 回しか読まれない。
"""

import json
from pathlib import Path

import pytest

from src.services.visitor_stats_dataset import (
    DEFAULT_DATASET_ROOT,
    VisitorStatsDataset,
)


# --- fixtures ---------------------------------------------------------------

_MANIFEST = {
    "name": "JNTO 訪日外客統計（承認済み取り込み分）",
    "version": "2025.03",
    "ingested_date": "2025-04-10",
    "available_dimensions": ["period", "region", "segment", "metric"],
}

# rows: 正規化クエリの (period, region, segment, metric) で引ける集計行。
_ROWS = [
    {
        "period": "2025-Q1",
        "region": "関東",
        "segment": "訪日外客",
        "metric": "訪問者数",
        "value": 12_840_000,
        "unit": "人",
        "citation": "JNTO 訪日外客統計 2025年3月",
    },
    {
        "period": "2025-Q1",
        "region": "関西",
        "segment": "訪日外客",
        "metric": "延べ宿泊者数",
        "value": 9_530_000,
        "unit": "人泊",
        "citation": "観光庁 宿泊旅行統計調査 2025年3月",
    },
]


def _write_dataset(root: Path, manifest=_MANIFEST, rows=_ROWS) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    (root / "rows.json").write_text(
        json.dumps(rows, ensure_ascii=False), encoding="utf-8"
    )


@pytest.fixture
def dataset_root(tmp_path):
    root = tmp_path / "visitor_stats"
    _write_dataset(root)
    return root


# --- 構造的 read-only（DB-5）------------------------------------------------


def test_no_write_methods_exist():
    """insert / update / delete / write / upsert に相当する public メソッドを持たない."""
    forbidden = {"insert", "update", "delete", "write", "upsert", "save", "put", "post"}
    public = {n for n in dir(VisitorStatsDataset) if not n.startswith("_")}
    assert forbidden.isdisjoint(public), f"書き込み系メソッドが存在する: {forbidden & public}"


def test_public_surface_is_only_load_manifest_and_query():
    """公開メソッドは load_manifest / query の 2 つだけ."""
    public = {
        n
        for n in dir(VisitorStatsDataset)
        if not n.startswith("_") and callable(getattr(VisitorStatsDataset, n))
    }
    assert public == {"load_manifest", "query"}


# --- manifest ---------------------------------------------------------------


def test_load_manifest_returns_available_true_with_metadata(dataset_root):
    ds = VisitorStatsDataset(str(dataset_root))
    m = ds.load_manifest()
    assert m["available"] is True
    assert m["name"] == _MANIFEST["name"]
    assert m["version"] == "2025.03"
    assert m["ingested_date"] == "2025-04-10"
    assert m["available_dimensions"] == ["period", "region", "segment", "metric"]


def test_load_manifest_is_memoized(dataset_root):
    """複数回呼んでもディスクを 1 回しか読まない（§4-3 メモ化）."""
    ds = VisitorStatsDataset(str(dataset_root))
    first = ds.load_manifest()
    # manifest ファイルを消しても、2 回目はメモから返る。
    (dataset_root / "manifest.json").unlink()
    second = ds.load_manifest()
    assert second == first
    assert second["available"] is True


# --- query（正常系）---------------------------------------------------------


def test_query_returns_matching_row_value(dataset_root):
    ds = VisitorStatsDataset(str(dataset_root))
    r = ds.query(
        {
            "period": "2025-Q1",
            "region": "関東",
            "segment": "訪日外客",
            "metric": "訪問者数",
            "filters": None,
        }
    )
    assert r["available"] is True
    assert r["matched"] is True
    assert r["value"] == 12_840_000
    assert r["unit"] == "人"
    assert r["citation"] == "JNTO 訪日外客統計 2025年3月"
    assert r["dataset_version"] == "2025.03"
    assert r["ingested_date"] == "2025-04-10"


def test_query_no_match_returns_matched_false_not_error(dataset_root):
    """該当行がなければ matched=False。available は True（データセットは健全）."""
    ds = VisitorStatsDataset(str(dataset_root))
    r = ds.query(
        {
            "period": "2099-Q4",
            "region": "南極",
            "segment": "訪日外客",
            "metric": "訪問者数",
            "filters": None,
        }
    )
    assert r["available"] is True
    assert r["matched"] is False
    assert "value" not in r


# --- query は状態を変えない（read-only 実挙動）------------------------------


def test_query_does_not_mutate_input_or_dataset(dataset_root):
    ds = VisitorStatsDataset(str(dataset_root))
    q = {"period": "2025-Q1", "region": "関東", "segment": "訪日外客", "metric": "訪問者数", "filters": None}
    original = dict(q)
    ds.query(q)
    assert q == original, "query() が入力 dict を破壊してはならない"
    # rows.json はディスク上で不変。
    rows_after = json.loads((dataset_root / "rows.json").read_text(encoding="utf-8"))
    assert rows_after == _ROWS


# --- 参照不能（§7-6: raise せず available=False）----------------------------


def test_missing_root_returns_unavailable_not_raises(tmp_path):
    ds = VisitorStatsDataset(str(tmp_path / "does_not_exist"))
    m = ds.load_manifest()
    assert m["available"] is False
    r = ds.query({"period": "x", "region": "y", "segment": "z", "metric": "訪問者数", "filters": None})
    assert r["available"] is False
    assert r.get("matched") is not True


def test_broken_manifest_returns_unavailable_not_raises(tmp_path):
    root = tmp_path / "visitor_stats"
    root.mkdir(parents=True)
    (root / "manifest.json").write_text("{ this is not json", encoding="utf-8")
    (root / "rows.json").write_text("[]", encoding="utf-8")
    ds = VisitorStatsDataset(str(root))
    m = ds.load_manifest()
    assert m["available"] is False


def test_broken_rows_returns_unavailable_not_raises(dataset_root):
    (dataset_root / "rows.json").write_text("{ not a list", encoding="utf-8")
    ds = VisitorStatsDataset(str(dataset_root))
    r = ds.query({"period": "2025-Q1", "region": "関東", "segment": "訪日外客", "metric": "訪問者数", "filters": None})
    assert r["available"] is False


# --- 既定パスは yaml と同一文字列（silent divergence 防止・§4-3）------------


def test_default_dataset_root_matches_yaml_value():
    assert DEFAULT_DATASET_ROOT == "data/visitor_stats"


def test_bundled_default_dataset_loads(dataset_root, monkeypatch):
    """リポジトリ同梱の data/visitor_stats が実在し manifest が読める（配線の健全性）."""
    repo_root = Path(__file__).resolve().parents[2]
    bundled = repo_root / DEFAULT_DATASET_ROOT
    assert bundled.is_dir(), "同梱データセット data/visitor_stats が存在しない"
    ds = VisitorStatsDataset(str(bundled))
    m = ds.load_manifest()
    assert m["available"] is True
    assert isinstance(m["version"], str) and m["version"]
