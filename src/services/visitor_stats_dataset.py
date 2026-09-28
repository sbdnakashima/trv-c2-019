"""AgentCore Platform v1.0 — 承認済み JNTO 統計データセットへの読み取り専用アクセス層.

docs/02_design.md §4-3. ノード ③ `readonly_query` がクエリするデータソースは、
事前取り込み済みの組織承認済みデータセットのみ（ライブ API 呼び出しなし・§1-2）。
その所在とアクセスをノードから分離し、このモジュールに閉じる。

このクラスが体現する 3 つの規約:

*読み取り専用性は API 形状で構造的に保証する（DB-5）。* `insert` / `update` /
`delete` / `write` / `upsert` に相当する public メソッドを一切定義しない。書き込む
手段を持たないことを型として担保するのであって、実行時チェックで防ぐのではない。

*参照不能は raise せず戻り値で表現する（§7-6）。* dataset_root が無い・manifest や
rows が壊れている場合でも `load_manifest()` / `query()` は例外を投げず、`available`
フラグ False を含む dict を返す。呼び出し側のノード ③ がこれを `DATASET_UNAVAILABLE`
に変換する。ノードの execute() 内 raise は BaseNode.__call__() に status="error" 化
され、Inner の記録を失わせるため（§7-6）、その一段手前でも raise しない。

*manifest はインスタンス内にメモ化する（§4-3）。* Outer の register_nodes() で 1 回
だけ生成され注入で引き回される想定なので、メモ化はリクエスト横断で効く。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

# config/agent.yaml の agent.config.dataset_root と同一文字列にすること。
# 食い違うと「yaml を直したのに既定値が別パスを指す」silent divergence になる（§4-3）。
DEFAULT_DATASET_ROOT = "data/visitor_stats"

# 正規化クエリのうち、行の同定に使う 4 次元（filters は絞り込みで別扱い）。
_MATCH_DIMENSIONS = ("period", "region", "segment", "metric")

_MANIFEST_FILE = "manifest.json"
_ROWS_FILE = "rows.json"


class VisitorStatsDataset:
    """承認済み訪問者統計データセットの読み取り専用ビュー.

    書き込み系メソッドを一切持たないことで read-only 性を構造的に保証する（DB-5）。
    """

    def __init__(self, dataset_root: str) -> None:
        self._root = Path(dataset_root)
        # メモ化スロット。None = 未読み込み（load_manifest() で埋める）。
        self._manifest: Optional[dict[str, Any]] = None

    # -- public API（この 2 つのみ）------------------------------------------

    def load_manifest(self) -> dict[str, Any]:
        """データセットのメタデータを返す（メモ化）.

        Returns:
            available=True のとき name / version / ingested_date /
            available_dimensions を含む。参照不能なら available=False のみ。
        """
        if self._manifest is not None:
            return self._manifest
        self._manifest = self._read_manifest()
        return self._manifest

    def query(self, normalized_query: dict[str, Any]) -> dict[str, Any]:
        """正規化クエリに一致する集計行を返す（読み取り専用）.

        Args:
            normalized_query: period / region / segment / metric / filters を
                持つ dict（① request_normalize が生成した検証済みクエリ）。
                この dict を破壊しない。

        Returns:
            available False: データセット参照不能（§7-6）。
            available True + matched False: 健全だが該当行なし。
            available True + matched True: value / unit / citation /
                dataset_version / ingested_date を含む。
        """
        manifest = self.load_manifest()
        if not manifest.get("available"):
            return {"available": False}

        rows = self._read_rows()
        if rows is None:
            return {"available": False}

        row = self._find_row(rows, normalized_query)
        if row is None:
            return {"available": True, "matched": False}

        return {
            "available": True,
            "matched": True,
            "value": row.get("value"),
            "unit": row.get("unit"),
            "citation": row.get("citation"),
            "dataset_version": manifest.get("version"),
            "ingested_date": manifest.get("ingested_date"),
        }

    # -- 内部（読み取りのみ。書き込みは存在しない）----------------------------

    def _read_manifest(self) -> dict[str, Any]:
        raw = self._read_json(self._root / _MANIFEST_FILE)
        if not isinstance(raw, dict):
            return {"available": False}
        dims = raw.get("available_dimensions")
        return {
            "available": True,
            "name": raw.get("name"),
            "version": raw.get("version"),
            "ingested_date": raw.get("ingested_date"),
            "available_dimensions": dims if isinstance(dims, list) else [],
        }

    def _read_rows(self) -> Optional[list[dict[str, Any]]]:
        raw = self._read_json(self._root / _ROWS_FILE)
        if not isinstance(raw, list):
            return None
        return [r for r in raw if isinstance(r, dict)]

    @staticmethod
    def _read_json(path: Path) -> Any:
        """JSON を読む。存在しない・壊れている場合は None（raise しない・§7-6）."""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            return json.loads(text)
        except (ValueError, UnicodeError):
            return None

    @staticmethod
    def _find_row(rows: list[dict[str, Any]], normalized_query: dict[str, Any]) -> Optional[dict[str, Any]]:
        """4 次元が完全一致する最初の行を返す（照合は exact。② と同じ規約）."""
        for row in rows:
            if all(row.get(dim) == normalized_query.get(dim) for dim in _MATCH_DIMENSIONS):
                return row
        return None
