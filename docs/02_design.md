# TRV-C2-019 Destination Visitor-Statistics Query Agent — 設計仕様書

- **Template ID**: TRV-C2-019
- **Template Name**: Destination Visitor-Statistics Query Agent
- **Category / Industry**: Cat 2 / TRV
- **scaffold issue**: agent1000/agent-templates/(internal issue reference removed)
- **設計日**: 2026-07-30

---

## 1. 概要とスコープ境界

観光アナリストの制御された自然言語リクエストを、**事前取り込み済みの組織承認済み JNTO 統計データセット**への
読み取り専用クエリに変換する。リクエストを **期間・地域・セグメント・指標の 4 次元 + フィルター**に正規化し、
データセット/バージョン・取り込み日・フィルター解釈・引用を付与した集計訪問者統計（Markdown）を返す。
サポート外・曖昧・開示制限に該当するクエリは、**レビュー可能な例外レポート**として返す。

### 1-1. スコープ内

| 項目 | 内容 |
|---|---|
| 入力 | アナリストの自然言語リクエスト + 承認済みクエリスキーマ（JSON） |
| 処理 | NL → 4 次元 + フィルター正規化 → スキーマ/開示ポリシー検証 → 読み取り専用クエリ → 出所組立 |
| 出力 | 出所付き集計値（Markdown。データセット/バージョン・取り込み日・フィルター解釈・引用）または例外レポート |

### 1-2. スコープ外（設計上の禁止事項）

- **JNTO ライブ API は使用しない**（公式 API 非提供）。データソースは事前取り込み済みの承認済みデータセットのみ。
- 需要予測・目的地推薦・個人旅行行動推論・旅行者ターゲティング・公告発行は対象外。
- **書き込み・更新・削除は一切行わない**（読み取り専用）。
- クエリの拡大・別指標への暗黙の置き換えを行わない。曖昧な場合は `AMBIGUOUS_QUERY` として例外返却する
  （拡大解釈は明示的な人間の決定が必要）。
- 分析適合性の最終判断はアナリスト（人間）が担う。本エージェントは出所付き集計値の取得までを担当する。

---

## 2. AgentCore アーキテクチャ上の位置づけ

- **Agent Class**: Cat 2（特定の job-to-be-done を複数ステップでオーケストレーションする）
- **L1 Base**: `AgentBaseGraph`（L1 直接継承。`AutonomousBaseGraph` は使用しない）
- **Composition Pattern**: GraphNode（subgraph）— Outer の `main` スロットに Inner graph を composition する 2 層構成
- **Error propagation strategy**: **`handle`**（`GraphNode.error_strategy = "handle"` + `on_subgraph_error()` override）。
  framework 既定の `propagate` からの意図的な変更であり、理由と契約は §7-7 に定義する
- **三層分離**:
  - **State**: フラット TypedDict（`AgentState` 継承）。Pydantic 禁止（ADR-005・msgpack 非互換）
  - **Node**: L1 継承（Template Method — `execute()` のみ override。`__call__()` は override 禁止）
  - **Graph**: composition（`register_nodes()` によるノード差し替え）

### Import Isolation

- [x] Level 0 SDK（`agenticstar`）を `src/` から import しない（`gate-import-isolation`）
- [x] import 対象は `framework.*` / `shared.*` / `langgraph.*` のみ

---

## 3. Outer graph 構成（`src/graph/graph.py`）

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                            ↓ (RETRY / それ以外)
                                    pre_process / finalize
```

| スロット | 実装クラス | 責務 |
|---|---|---|
| initialize | `InitializeNode`（framework 既定） | `trace_id` / `session_id` 採番、`caller_trust_level` / `hitl_allowed` の保全（`correlation_id` は `BaseGraph.invoke()` が設定） |
| pre_process | `PassThroughNode`（`src/nodes/pass_through_node.py`） | State を変更しない（intake の解釈は `extract_input()` と Inner ⓪ が担う）。ただし S-4 イベントは発行する |
| main | `VisitorStatsQueryGraphNode`（`GraphNode`） | Inner graph の invoke・入出力ブリッジ |
| post_process | `PassThroughNode` | State を変更しない。S-4 イベントは発行する |
| finalize | `FinalizeNode`（framework 既定） | `response_metadata` 組立（**`status` は書かない**） |

`PassThroughNode` は framework に存在しない（`framework/nodes/defaults/` に `PassThrough` は無い）ため
テンプレート側で `FunctionNode` を継承して実装する。したがって:

- `required_trust_level = TrustLevel.VERIFIED_EXTERNAL` を宣言する（`gate-trust-level-check`）
- **`execute()` 内で `emit_trace_event()` を 1 回以上呼ぶ**。scaffold の TC-05 は
  「ドメイン固有 `emit_trace_event()` を `execute()` 内で**副作用の有無に関わらず全ノードで最低 1 回**」を必須とするため、
  `return {}` だけの実装は TC-05 で落ちる（§7-4）
- State は変更しない（`{}` を返す）。「no-op」は State に対してのみ成立する

### 3-1. Outer で override するもの（すべて必須）

| override | 理由 |
|---|---|
| `state_schema` → `State` を返す | **必須**。`AgentBaseGraph.state_schema` は `AgentState` を返す。`BaseGraph._build_graph()` は `StateGraph(self.state_schema)` と各ノードの `input_schema=self.state_schema` にこれを使うため、override しないと `State` のドメインフィールドは**チャネルとして存在せず全て捨てられる** |
| `_extra_initial_state()` | ドメインフィールドを `None` 初期化する（未初期化だと `input_schema` フィルタがキーを渡さず後段で `AttributeError`） |
| `get_output()` | `AgentBaseGraph.get_output()` は `{output, status, trace_id, correlation_id, node_history}` の 5 キーのみを返すため、ドメインフィールド（`provenance_output` / `exception_report` / `dataset_version` / `error_code` / `s3_clearance` / `normalized_query` / `validation_result` / `inner_node_history`）を追加公開する。`super().get_output(state)` を呼んだ結果に追記する |
| `on_subgraph_error()` | Inner が予期しない例外で ERROR になった場合でも例外レポートを失わないための多層防御（§7-7） |
| `register_nodes()` | `super().register_nodes()` を呼んで 5 スロットを作った**後**に pre/main/post を実体ノードで埋める |

> ⚠️ **`output` は State チャネルに存在しないため、`merge_output()` で `output` を返しても黙って破棄される。**
> `AgentState` が宣言するのは `formatted_output` / `result` であり `output` ではない。
> `AgentBaseGraph.get_output()` は `state.get("formatted_output") or state.get("result")` から
> `output` キーを**組み立てて**返すだけである。
> LangGraph は未宣言キーを例外なく黙って捨てるため、`output` を直接載せると
> **呼び出し側が受け取る主成果物 `output` が常に `None` になる**。
> よって `merge_output()` は **`formatted_output`** に成果物（`provenance_output` または `exception_report`）を載せる（§5-4）。

`register_nodes()` は `self.config.get("dataset_root", DEFAULT_DATASET_ROOT)`
（`config/config.yaml` の `dataset_root`）から `VisitorStatsDataset` を **1 回だけ生成**し、
そのインスタンスを `VisitorStatsQueryGraphNode` のコンストラクタに渡す。
`BaseNode` には `config` 属性が無く、`config` を保持するのは `BaseGraph`（`self.config`）だけであるため、
この受け渡しを省略するとノード ③ がデータセットを取得できない。
インスタンス（パス文字列ではなく）を渡す理由・`self.config` の供給元（`agent.yaml` は
L1 が自動で読み込まないため配線が必要）・`[]` ではなく `.get()` を使う理由は **§4-3** を参照。

`add_edges()` / `route()` は **override しない**（固定バックボーンを尊重する）。

> ⚠️ **`_extra_initial_state()` に `AgentState` 継承フィールドを入れてはならない。**
> `BaseGraph.invoke()` は `initial_state["user_input"] = user_input` を設定した**後に**
> `initial_state.update(self._extra_initial_state())` を実行する。
> したがって `"user_input": None` を含めると**呼び出し側の入力が `None` で上書きされ、全リクエストが失敗する**。
> 対象は本設計 §6 のドメインフィールド 12 個のみ（`user_input` / `status` / `node_history` / `error_log` /
> `session_id` / `trace_id` / `correlation_id` / `formatted_output` は含めない）。
> 参照仕様 `my_guide/TRV-C2-019.md` のスニペットには `"user_input": None` が含まれているが、**それは誤りである**。

### 3-2. scaffold プレースホルダーの削除（Issue #8 で実施）

`src/nodes/pre_process_node.py` / `main_node.py` / `post_process_node.py` と
`tests/unit/test_main_node.py` は scaffold 同梱のプレースホルダーであり、本設計のノード構成には含まれない。
Graph composition 完了時に `git rm` する（残すと `required_trust_level` 未宣言で
`gate-trust-level-check` が FAIL し、削除漏れの import で `run-tests` が FAIL する）。

---

## 4. Inner graph 構成（`src/graph/visitor_stats_query_graph.py`）

`BaseGraph` を直接継承（カスタムトポロジ）。`BaseGraph` の 7 つの abstract メンバー
`name` / `state_schema` / `register_nodes` / `add_edges` / `route` / `get_output` / `_validate_config` を全て実装する。

```
START → input_expand → request_normalize → query_validate → readonly_query → provenance_assemble → END
```

| # | ノード | ファイル | 責務 | セキュリティ |
|---|---|---|---|---|
| ⓪ | `input_expand` | `src/nodes/input_expand_node.py` | `user_input`(packed) → `analyst_request` / `approved_schema` 復元 | S-1 / S-3 除去 / **S-4** |
| ① | `request_normalize` | `src/nodes/request_normalize_node.py` | LLM で NL → 4 次元（期間/地域/セグメント/指標）+ フィルター正規化 | S-1 / **S-2** / S-3 除去 / **S-4** |
| ② | `query_validate` | `src/nodes/query_validate_node.py` | 決定論的に承認スキーマ・開示ポリシーを検証。無効/曖昧/制限は例外化 | S-1 / **S-4** |
| ③ | `readonly_query` | `src/nodes/readonly_query_node.py` | 承認済みデータセットへの読み取り専用クエリ・`dataset_version` 取得 | S-1 / S-3 除去 / **S-4** |
| ④ | `provenance_assemble` | `src/nodes/provenance_assemble_node.py` | 出所組立・Markdown 生成・S-3 クリアランス判定 | S-1 / **S-3** / **S-4** |

**S-4 は全ノード必須**（`PassThroughNode` ×2・`VisitorStatsQueryGraphNode` も含む）。
scaffold TC-05 が「ドメイン固有 `emit_trace_event()` を `execute()` 内で全ノード最低 1 回」を要求する（§7-4）。

### 4-1. Inner の実装規約

- 全ノードは `framework.nodes.function_node.FunctionNode` を継承し、
  クラス本体で `required_trust_level = TrustLevel.VERIFIED_EXTERNAL` を宣言する（`gate-trust-level-check`）。
- `state_schema` は `State` を返す（Outer と同じ。§3-1 と同じ理由）。
- `register_nodes()` は **`super()` を呼ばない**（`BaseGraph.register_nodes()` は abstract）。
  `initialize` / `finalize` は登録しない（Outer バックボーンの責務）。
- `_extra_initial_state()` は Outer と**同じ**ドメインフィールドを `None` 初期化する。
  ただし `user_input` と `inner_node_history` は**含めない**（`inner_node_history` は Outer 専用で
  `merge_output()` が書くフィールドであり、Inner 側では使わない）。
- 線形トポロジのため `route()` は ABC 充足のために実装するが `add_conditional_edges()` からは呼ばれない。
- `get_output()` は次を必ず含める:
  - **`status`**（`GraphNode.execute()` が `sub_result["status"]` を見て ERROR 判定するため）
  - **`node_history`**（Outer の `merge_output()` が `inner_node_history` を組む唯一の材料。DB-6 の検証手段）
  - ドメインフィールドと `error_code`

> ⚠️ **`get_output()` が `node_history` を返さないと DB-6 は成立しない。**
> `merge_output(state, sub_result)` が読めるのは `sub_result`（= Inner の `get_output()` の戻り値）だけであり、
> `BaseGraph._enrich_output()` の既定は no-op のため**他に経路が無い**。
> `node_history` は `AgentState` 継承フィールドでドメインフィールドではないため、
> 「ドメインフィールドを返す」だけの実装では `sub_result` に入らない。明示的に列挙すること。

### 4-2. LLM 使用箇所

| ノード | LLM | 備考 |
|---|---|---|
| ① `request_normalize` | ✅ AzureOpenAIClient | `AZURE_OPENAI_API_KEY`（`ctx.secrets.require()` 経由）。コンストラクタ注入でテスト時は Mock |
| ⓪ / ② / ③ / ④ | ❌ | 完全に決定論的 |

ハルシネーション対策: LLM 出力は必ず後段 ② `query_validate` で**承認スキーマへの決定論的マッピング**を強制する。
スキーマ外・曖昧は例外返却とし、LLM が推測した次元値をそのままクエリに使わない。

### 4-3. データアクセス層（`src/services/visitor_stats_dataset.py`）

ノード ③ `readonly_query` が参照する承認済みデータセットへのアクセスは、ノードから分離した
service モジュールに閉じる。`src/services/` は `REQUIRED_STG_DIRS` の対象であり
stg-complete 時に `__init__.py` 以外のモジュールを 1 個以上必要とするため、
scaffold のプレースホルダー `src/services/service.py` を本モジュールで置き換える。

| 項目 | 設計 |
|---|---|
| クラス | `VisitorStatsDataset` |
| 公開メソッド | `load_manifest() -> dict`（データセット名 / バージョン / 取り込み日 / 利用可能次元）<br>`query(normalized_query: dict) -> dict`（集計値を返す） |
| **書き込み系メソッドを定義しない** | `insert` / `update` / `delete` / `write` / `upsert` に相当するメソッドを**一切持たない**。読み取り専用性はこの API 形状で構造的に保証する（DB-5） |
| データセット所在 | `config/config.yaml` の `dataset_root`（取り込み済みデータの配置パス）。**シークレットではない**ため `config` で管理する（`AgentBaseGraph._validate_config()` は `max_retry` / `memory_enabled` / `hitl` のみ検査するため未知キーの追加は安全） |
| 注入 | ノード ③ のコンストラクタ引数で注入（`readonly_query_node.py` は `VisitorStatsDataset` を直接生成しない）。テストでは Fake を注入する |
| 参照不能時 | 例外を投げず `DATASET_UNAVAILABLE` を表現できる戻り値とし、③ が `error_code` に変換する（§7-6 の raise 禁止規約に従う） |
| インスタンスの生存期間 | **Outer の `register_nodes()` で 1 回だけ生成**し、以降は同一インスタンスを注入で引き回す（下記 4 ホップ）。`load_manifest()` の結果はインスタンス内にメモ化する |

#### `VisitorStatsDataset` の受け渡し経路（4 ホップ・省略不可）

`config` を保持するのは `BaseGraph`（`self.config`）だけであり、`BaseNode` に `config` 属性は無い。
また `GraphNode.get_subgraph()` は引数を取らないため、Inner の生成時に値を渡す必要がある。

```
config/config.yaml: dataset_root               ← ★ ホップ ⓪（下記「config の供給元」）
  → ① Outer Graph.register_nodes(): VisitorStatsDataset(self.config.get("dataset_root", DEFAULT_DATASET_ROOT)) を 1 回だけ生成
  → ② VisitorStatsQueryGraphNode(dataset=...) のコンストラクタで保持
  → ③ get_subgraph(): VisitorStatsQueryGraph(config={"dataset": self._dataset}) を生成
  → ④ Inner register_nodes(): ReadonlyQueryNode(dataset=self.config["dataset"])
```

**`dataset_root`（パス文字列）ではなく `VisitorStatsDataset`（インスタンス）を引き回す**のが要点である。

- `GraphNode.execute()` は invoke ごとに `get_subgraph()` を呼ぶ（`graph_node.py:135`）。
  `_ensure_compiled()` のキャッシュはインスタンス単位（`base_graph.py:115-120`）だが、
  ホップ ③ で Inner を毎回**新規生成**するため `compile()` → `register_nodes()`（`base_graph.py:127-130`）
  もリクエストごとに走る。
- したがってホップ ④ で `VisitorStatsDataset` を生成する設計にすると、
  **インスタンス内にメモ化してもリクエスト横断では効かず**、毎リクエストでマニフェストを読み直す。
- ホップ ① の Outer `register_nodes()` は Outer graph のライフサイクルで 1 回だけ走るため
  （`_ensure_compiled()` は `_compiled is None` のときだけ compile。`src/api/server.py` は
  モジュールレベルで `Graph()` → `compile()` を 1 回実行し同一インスタンスを再利用する）、
  そこで生成したインスタンスを注入で引き回せばメモ化がリクエスト横断で効く。
- `config` は State ではない（インスタンス属性であり `initial_state` にも
  `_extra_initial_state()` にも入らない。LangGraph に渡る `RunnableConfig` は
  `{"configurable": {"thread_id": ...}}` のみ）ため、**非プリミティブを載せてよい**。
  §6 の「State にオブジェクトを入れない」制約とは対象が異なる。
  なお `AgentBaseGraph._validate_config()`（`agent_base_graph.py:36-74`・`max_retry` /
  `memory_enabled` / `hitl` を検査）が掛かるのは **Outer の config（`dataset_root` = str）だけ**である。
  インスタンスを運ぶ **Inner** は `BaseGraph` 直接継承で `_validate_config()` は
  `@abstractmethod`（`base_graph.py:86-95`）であり、検証規則はテンプレートが自分で書く。

**Inner の `_validate_config()` は `config["dataset"]` の存在を必須検証する**（規範）。
ホップが 1 つ抜けた場合に compile 時点で `ConfigError` として落ち、
ノード ③ の実行時 `AttributeError`（`None.query()`）にならないようにするため。

この 4 ホップを省略すると、ノード ③ は `config/agent.yaml` を直読みするしかなくなり、
テスト時に Fake を注入できず DB-5 / PB-3 が検証不能になる。

#### ホップ ⓪ — `self.config` の供給元（Issue #8 で実装・省略すると起動時に落ちる）

⚠️ **`BaseGraph.__init__(config=None)` は `self.config = config or {}` とするだけで
（`base_graph.py:30-31`）、`config/agent.yaml` を読み込む処理は L1 に存在しない。**
`framework/utils/config_loader.load_config()` はあるが、呼び出しているのは
`autonomous_base_graph.py:83`（pricing 読み込み）のみである（全件 grep 確認済み）。
さらに scaffold 同梱の `src/api/server.py` は `agent = Graph()` と**引数なしで生成**している。
したがって ① を `self.config["dataset_root"]` と書くと **import 時に `KeyError` で起動が落ちる**
（`/health` も応答せず Phase 5 の STG invoke・`gate-stg` がブロックされる）。

**Issue #8（Graph composition）** で以下の 3 点を必ずセットで実装する:

| # | 対応 | 内容 |
|---|---|---|
| 1 | `config/agent.yaml` に鍵を追加 | `agent.config` 配下に `dataset_root: "data/visitor_stats"` を追加する（現状 `max_retry` / `timeout_seconds` のみ） |
| 2 | standalone 経路の配線 | `src/api/server.py` を `Graph(config=load_config("config/agent.yaml")["agent"]["config"])` に変更する。プラットフォーム経路（`AgentRegistry`）は `agent.config` の中身を**フラットに**渡す（根拠: `agent_base_graph.py:42` が `self.config.get("max_retry")` とフラット参照しており、`agent.yaml` では `max_retry` が `agent.config` 配下にある）。すなわち `agent:` / `config:` の 2 階層は剥がして渡される。**同じ編集で `server.py` に残る namespace / agent name のプレースホルダーも併せて実値に置換する**（未置換だと `provision_secrets()` の namespace が不正になりシークレット解決が失敗する。CI はプレースホルダー構文を検査しないため取りこぼしやすい） |
| 3 | 欠落時の fail-safe | ① は `self.config["dataset_root"]` ではなく **`self.config.get("dataset_root", DEFAULT_DATASET_ROOT)`** とする。配線漏れが `KeyError` で起動不能になるのを避け、参照不能は §4-3 の `DATASET_UNAVAILABLE`（実行時）または Inner `_validate_config()` の `ConfigError`（compile 時）として表現する |

**振り先が Issue #8 である理由と依存関係**: ホップ ① の `Outer Graph.register_nodes()`・ホップ ③ の
`get_subgraph()`・ホップ ④ の `Inner register_nodes()` はいずれも Issue #8 で初めて書かれるコードである
（Issue #3 は Inner ⓪ `input_expand_node` 単体でデータセットに触れない）。とくに**項目 3 は
ホップ ① と同一 MR に含めなければならない** — ホップ ① が入った時点で `KeyError` による起動不能が
発生し得るため、fail-safe を後続 MR に回すと develop が一時的に起動不能な状態になる。
項目 1・2 は先行 Issue で実施しても害はないが、**項目 3 だけを分離することは禁止**する。

`DEFAULT_DATASET_ROOT` は `src/services/visitor_stats_dataset.py` のモジュール定数として
`DEFAULT_DATASET_ROOT = "data/visitor_stats"` と定義する（**項目 1 の yaml 値と同一文字列**にすること。
食い違うと「yaml を直したのに既定値が別パスを指す」silent divergence になる）。

シークレットは不要（ローカル配置の承認済みデータセットのみを読む。外部 API 呼び出しなし）。
したがって §12 のシークレット一覧は ① `request_normalize` の Azure OpenAI 資格情報のみで完結する。

---

## 5. Cat 2 の intake 契約（Outer → Inner）

Outer → Inner に State は**直接引き継がれない**。`GraphNode.execute()` は
`extract_input(state) -> str` の戻り値を Inner の `user_input` として `subgraph.invoke()` に渡すのみである。

### 5-1. 呼び出し側の入力契約

Outer の `user_input` は次の 2 キーを持つ JSON オブジェクト文字列とする。
キー名は `INNER_INPUT_FIELDS` として単一定義する。

```json
{"analyst_request": "<自然言語リクエスト>", "approved_schema": "<承認済みクエリスキーマ(JSON文字列)>"}
```

### 5-2. intake payload の pack / unpack（`src/nodes/input_expand_node.py` が単一の正典）

```
Outer.extract_input(state)
    = pack_intake(build_intake_payload(state.get("user_input")))
        ↓  inner user_input（packed 文字列）
Inner ⓪ input_expand.execute()
    = unpack_intake(state.get("user_input")) → analyst_request / approved_schema
        ↓
Inner.get_output()  →  sub_result  →  Outer.merge_output()  →  親 State への delta（変更キーのみ）
```

`src/nodes/input_expand_node.py` が export するもの（Outer / Inner の両方がここから import し、**定義を二重化しない**）:

| 名前 | 契約 |
|---|---|
| `INNER_INPUT_FIELDS = ("analyst_request", "approved_schema")` | intake のキー集合 |
| `INTAKE_SENTINEL` | packed 文字列の先頭に付く固定文字列。**規範的制約**: `[A-Za-z0-9_]` のみで構成し、空白を含まず、Title Case 英単語の連続を作らず、**末尾を英字にする**（§5-3） |
| `build_intake_payload(raw_user_input) -> dict` | 呼び出し側 JSON を解析し `INNER_INPUT_FIELDS` を抽出。失敗時は `_intake_error` に理由を格納。**raise しない** |
| `pack_intake(payload) -> str` | `INTAKE_SENTINEL + json(payload).encode("utf-8").hex()`。**raise しない** |
| `unpack_intake(packed) -> dict` | `pack_intake` の逆。失敗時は `{"_intake_error": "INVALID_INNER_INPUT"}`。**raise しない** |

`extract_input()` / `pack_intake()` / `unpack_intake()` はいずれも例外を投げない。
raise すると `BaseNode.__call__()` が status=ERROR にして例外レポートが消失する（§7-6）ため、
**intake の異常は全て `_intake_error` → `error_code` として値で表現する**。

### 5-3. なぜ平文 JSON をそのまま渡さないのか（S-2 PII マスクによる入力破壊の回避）

Inner ⓪ は `FunctionNode` であり、その `@final` S-2 ゲートが `user_input` に対して
`detect_pii()` を実行し、ヒット箇所を `[MASKED]` に置換してから `execute()` に渡す。
`shared/security/pii_detector.py` の `name` パターン
（`\b[A-Z][a-z]{1,20}(?:\s[A-Z][a-z]{1,20})+\b`）は **Title Case の英単語 2 語以上に無条件でヒットする**ため、
平文 JSON を渡すと地域名・セグメント名が破壊される。

実測（`docs/03_test_spec.md` に TC として登録する）:

| 入力形式 | `detect_pii` 検出 |
|---|---|
| 平文 JSON（`"Osaka Prefecture への Foreign Visitors …"`） | `name` × 2（`Osaka Prefecture` / `Foreign Visitors`）→ **クエリ内容が消える** |
| `pack_intake()` 済み（sentinel + hex） | **0 件** |

`pack_intake` に **hex（`[0-9a-f]`）** を使い、sentinel を英字で終わらせるのは意図的である。
sentinel 自体が PII パターンにヒットしてはならない。実測（sentinel 単体・sentinel + hex 本体の両方）:

| sentinel 候補 | `detect_pii` 検出 | §5-2 制約 | 採否 |
|---|---|---|---|
| `"Intake Payload "` | `name` × 1（`Intake Payload`） | 違反（大文字・空白） | ✕ |
| `"trvc2019intake"` | 0 件 | 適合 | **○ 採用** |
| `"intakev1payload"` | 0 件 | 適合 | ○ |
| `"INTAKE_V1_"` | 0 件 | 違反（大文字・末尾が `_`） | ✕ |

`"INTAKE_V1_"` は現行の検出器では 0 件だが、大文字を含み末尾が `_` であるため §5-2 の制約に違反する。
0 件であることに依存せず**構造的にマッチ不能**な形（小文字英数字のみ・末尾英字）に限定することで、
検出器のパターンが将来追加されても壊れない。§5-2 の規範的制約はこのための契約である。

- 大文字・`@`・空白・`-`・`.`・`(` を含まないため、`name` / `email` / `phone` / `ssn` パターンは**構造的に**マッチ不能。
- sentinel と hex 本体の間に単語境界（`\b`）が生じないため、`credit_card` / `my_number_jp` の
  `\b\d{4}…` も 1 トークンの内部ではマッチ不能。
- **base64 は不可**: JSON の先頭 `{"` が `eyJ` に符号化され、`credential_detector` の `jwt` パターン
  （`eyJ[a-zA-Z0-9._\-]{10,}`）に必ずヒットする（実測確認済み）。hex は `eyJ` を含み得ない。

### 5-4. `merge_output()` の責務

```
merge_output(state, sub_result) → 親 State への delta
```

- **`status` を必ず持ち上げる**。Outer の `status` は `merge_output()` の delta にしか依存せず、
  `FinalizeNode` は `status` を書かない。載せないと Outer は `pending` のままとなり、
  `AgentBaseGraph.route()` が `finalize` を返して **`post_process` がスキップされ**、
  `invoke()` の戻り status も `pending` になる。
- **`formatted_output` に成果物を載せる**（`provenance_output` があればそれ、無ければ `exception_report`）。
  `output` は State チャネルに存在せず載せても破棄されるため、**`output` を返してはならない**（§3-1 の警告）。
  `AgentBaseGraph.get_output()` が `formatted_output` から `output` キーを組み立てる。
- `error_code` / `provenance_output` / `exception_report` / `dataset_version` / `s3_clearance` /
  `normalized_query` / `validation_result` / `inner_node_history` を持ち上げる。
- **`query_result` は意図的に持ち上げない**（§7-3 の流出防止。生の集計値は出所付きの
  `provenance_output` としてのみ公開する）。この帰結として DB-1 のアサーション対象も
  `provenance_output` / `s3_clearance` / `error_code` に限定する（§11）。
- **`node_history` / `error_log` は載せない**（reducer チャネル。§6 参照。Inner の履歴を親に concat すると二重計上になる）。
  代わりに Inner のノード順序を**非 reducer** の `inner_node_history` に載せる（DB-6 の検証手段）:
  `inner_node_history = json.dumps(sub_result.get("node_history", []), ensure_ascii=False)`。
  これは reducer チャネルを経由しないため二重計上せず、かつ Outer の `node_history`
  （バックボーン 5 ノード）を汚さない。Inner 側の `get_output()` が `node_history` を返すことが前提（§4-1）。
- 変更キーのみを返す（`{**state}` を返さない）。

---

## 6. State 定義（`src/schemas/state.py`）

`class State(AgentState)`。ドメインフィールドは全て `Optional[str]`。
Outer / Inner は**同一の `State` クラス**を共有する（別定義にしない）。

`_extra_initial_state()` 列は **Outer の値**である（ドメインフィールド 12 個）。
**Inner は `inner_node_history` を除いた 11 個**を初期化する
（`inner_node_history` は Outer の `merge_output()` が Inner の `get_output()` から組み立てる
Outer 専用フィールドであり、Inner 自身は書かない。§4-1・§5-4）。

| フィールド | 型 | 書き込みノード | 内容 | `_extra_initial_state()`（Outer） |
|---|---|---|---|---|
| `user_input` | `Optional[str]` | 呼び出し側 / `extract_input()` | Outer: 呼び出し側 JSON / Inner: packed intake（継承フィールド） | ❌ 含めない |
| `analyst_request` | `Optional[str]` | ⓪ input_expand | 復元した自然言語リクエスト | ✅ |
| `approved_schema` | `Optional[str]` | ⓪ input_expand | 承認済みクエリスキーマ（JSON 文字列） | ✅ |
| `normalized_query` | `Optional[str]` | ① request_normalize | 4 次元 + フィルター正規化結果（JSON 文字列） | ✅ |
| `validation_result` | `Optional[str]` | ② query_validate | 有効クエリ or 例外理由（JSON 文字列） | ✅ |
| `query_result` | `Optional[str]` | ③ readonly_query / ④（消去） | 取得した集計値（JSON 文字列） | ✅ |
| `dataset_version` | `Optional[str]` | ③ readonly_query | データセット名/バージョン・取り込み日 | ✅ |
| `provenance_output` | `Optional[str]` | ④ provenance_assemble | 出所付き集計値（Markdown） | ✅ |
| `exception_report` | `Optional[str]` | ⓪〜④ | 例外レポート（Markdown・レビュー可能） | ✅ |
| `s3_clearance` | `Optional[str]` | ④ provenance_assemble | S-3 クリアランス状態（`clear` / `redacted` / `blocked`） | ✅ |
| `audit_log` | `Optional[str]` | ①〜④ | 監査用メタデータ（JSON 文字列） | ✅ |
| `error_code` | `Optional[str]` | ⓪〜④ / **`on_subgraph_error()`**（`INTERNAL_ERROR`・§7-7） | §8 の error_code | ✅ |
| `inner_node_history` | `Optional[str]` | `merge_output()` | Inner のノード実行順（JSON 文字列）。DB-6 の検証手段 | ✅ |

`status` / `node_history` / `error_log` / `trace_id` / `correlation_id` / `session_id` /
`formatted_output` は `AgentState` から継承する。

「書き込みノード」列は Inner のノード ⓪〜④ に加え、**Outer 側の 2 経路**を含む:

| Outer 側の書き込み元 | 書くフィールド |
|---|---|
| `merge_output()`（正常系・§5-4） | `status` / `formatted_output` / `error_code` / `provenance_output` / `exception_report` / `dataset_version` / `s3_clearance` / `normalized_query` / `validation_result` / `inner_node_history` |
| `on_subgraph_error()`（予期しない例外・§7-7） | `status`（ERROR）/ `formatted_output`（例外レポート）/ `exception_report` / `error_code`（`INTERNAL_ERROR`）/ `error_log`（reducer へ append） |

### State 制約（必須）

- フラット TypedDict のみ（プリミティブ + JSON シリアライズ可能型）。
- **list / dict は JSON 文字列で保持**（msgpack 非互換）。
- **認証情報・API キー・JWT を State に入れない**（checkpoint DB 経由の漏洩防止）。
- `InvocationContext` を State に保持しない（`InvocationContext.from_state(state)` で都度生成する）。
- Pydantic モデル / dataclass / 任意の Python オブジェクトを入れない。
- **`AgentState` 継承フィールドのうち `validated_input` / `llm_response` を使用しない**（必須）。
  framework の `@final` S-2 ゲートは `_PII_SCAN_FIELDS = ("user_input", "validated_input", "llm_response")`
  を PII マスク対象とする。この 3 つを使わない（`user_input` は §5-3 の hex で保護済み）ことが、
  §5-3 の防御を全ノード経路で成立させている根拠である。
  State に載せるのは**パース済みの正規化結果のみ**（`normalized_query`）とし、
  **LLM の生応答文字列は `execute()` のローカル変数に留めて State チャネルに書かない**
  （生応答は自由文であり、Title Case の地域名・セグメント名を含むため、
  後続ノードの S-2 スキャン対象になった時点で破壊される。またパース前の生文は誰も再利用しない）。
  `normalized_query` という名前を将来 `validated_input` / `llm_response` に変更してはならない
  （変更すると `name` パターンで地域名・セグメント名が再び破壊される）。
- 未宣言キーを `execute()` の戻り delta に含めない。LangGraph は**例外を出さず黙って捨てる**ため、
  タイプミスや `output` のような未宣言キーはデバッグ困難な欠落として現れる（§3-1）。

### `node_history` / `error_log` は reducer チャネル

`AgentState` では `node_history` / `error_log` が `Annotated[list[str], operator.add]` として定義されている。
このため各ノードの `execute()` は **`{**state, ...}` を返さず、変更キーのみの delta を返す**。
`{**state}` を spread すると既存の履歴が再度 concat され、`node_history` が重複増殖する
（`BaseNode.__call__()` が自ノード名を append することとは独立の問題）。

> **設計判断（実装計画からの逸脱 ①）**: 実装計画 Global Constraints には
> 「state は必ず `{**state, ...}` で新 dict を返す」と記載があるが、上記 reducer チャネルの仕様と
> `src/examples/domain_workflow_graph_sample.py`（「Each node's execute() returns only changed state keys」）に従い、
> **変更キーのみを返す方式を採用する**。

---

## 7. セキュリティ設計

### 7-1. S-1 トラストゲート

- 全ノードが `required_trust_level = TrustLevel.VERIFIED_EXTERNAL` を明示宣言する。
- `BaseNode.__call__()` が呼び出し元 trust level を検証し、不足時は `error_log` に記録して ERROR を返す。
- `VisitorStatsQueryGraphNode`（`GraphNode`）も同値を宣言する
  （`gate-trust-level-check` は `FunctionNode` 直接サブクラスのみ検査するが、意図を明示するため宣言する）。

### 7-2. S-2 入力ゲート（① `request_normalize_node`）

`FunctionNode._security_gate_input()` は `@final` のため **override 禁止**（override すると class 定義時に `TypeError`）。
拡張は `_extra_security_gate_input(self, state: AgentState) -> AgentState` フックで行う
（`function_node.py:129`。戻り値は `dict` ではなく **`AgentState`**。`state` を書き換えて返す）。

| 項目 | 設計 |
|---|---|
| 検査対象 | `analyst_request` / `approved_schema`（framework 既定の PII スキャンは `user_input` / `validated_input` / `llm_response` のみを対象とするため、ドメインフィールドは本フックで補う） |
| 検出器 | framework の `shared.security.credential_detector.detect_credentials()`（5 パターン: `openai_key` / `jwt` / `aws_key` / `bearer_token` / `conn_string`）を使用。テンプレート側で独自 regex を再実装しない |
| 検出時の挙動 | ① 該当箇所を `[REDACTED:<type>]` に**置換**する → ② フック内で `state["error_code"] = "CREDENTIAL_IN_REQUEST"` を立てる（**フック内の代入はチャネルに届かない。`execute()` への申し送りに過ぎない**）→ ③ `execute()` は LLM を呼ばず例外レポートを組み立てる → ④ **`execute()` は redact 済み `analyst_request` / `approved_schema` と `error_code` / `exception_report` を戻り delta に必ず含める** |
| **raise しない** | framework のフック契約は "Must not raise; surface errors via state"。Inner ノードで raise すると `__call__()` が status=ERROR を返し、Inner graph 全体が ERROR → `GraphNode` が `SubgraphError` を投げて `merge_output()` に到達しないため、**例外レポートという成果物そのものが消失する** |

> **設計判断（実装計画からの逸脱 ②）**: 実装計画には「credential 検出時 `SecurityViolationError` を raise」と
> あるが、上記の記録消失（Cat 2 の確立された規約）を避けるため、
> **Inner ノードでは raise せず redact + `error_code` で fail-closed する**方式を採用する。
> Outer 側ノードは記録消失を伴わないため raise 可。
> 参照仕様 `my_guide/TRV-C2-019.md` の PB-1 が期待する `SecurityViolationError` は本設計が上書きする。

**④ が必須である理由**: `__call__()` は `state = self._security_gate_input(state)` の戻り値を
`execute()` に渡すだけで、**LangGraph のチャネルに書かれるのは `execute()` の戻り delta のみ**である。
本設計は delta 返却方式（§6 逸脱 ①）を採るため、hook 内の redact を `execute()` が delta に載せない限り、
⓪ が書いた credential 入りの原文が State チャネル（= checkpoint DB）と Inner `get_output()` に残留する。
**除去してから拒否する**という順序も同じ理由で必須である。

### 7-3. S-3 出力ゲート（④ `provenance_assemble_node` + 外部由来文字列を返す全ノード）

`FunctionNode._security_gate_output()` も `@final`。拡張は `_extra_security_gate_output(result) -> dict`。

| 項目 | 設計 |
|---|---|
| framework 既定 | `result` の全 str 値を `detect_credentials()` で走査し、検出時 `RuntimeError` を raise してブロックする。**`_extra_security_gate_output()` より先に走る** |
| テンプレート方針 | framework ゲートに到達する前に、`execute()` 内で外部由来文字列（データセット由来の値・LLM 出力）から credential を**除去**する（除去 → 切り詰めの順・文字列リーフ単位） |
| 適用ノード | ⓪（呼び出し側由来）/ ①（LLM 出力）/ ③（データセット由来）/ ④（組立結果）の**すべて**。`@final` S-3 は全ノードで自動実行されるため、外部由来文字列を delta に載せるノードは例外なく除去責務を負う |
| 実装の所在 | 4 ノードで重複させないため `src/security/credential_redaction.py` に共有する。`redact_credentials(text)` が平文用、`redact_json_document(raw)` が直列化済み JSON 用（上記「文字列リーフ単位」の実装。直列化形に平文用を掛けると `conn_string` の `[^\s]{8,}` が閉じ引用符と後続キーまで飲み、②に解釈不能なスキーマが渡る）。いずれも `(除去後テキスト, 除去件数)` を返し、件数は S-4 payload の `credentials_redacted` に載る |
| ブロック時 | `error_code = "S3_BLOCKED"` / `s3_clearance = "blocked"` を設定し、`provenance_output` と `query_result` を **`None` で上書き**する。例外レポートは残す |
| 例外レポートの内容制約 | `exception_report` に**検出文字列そのものを引用してはならない**（型名・フィールド名・件数のみを記載する）。引用すると `exception_report` 自体が `@final` S-3 ゲートの `detect_credentials()` にヒットして `RuntimeError` になり（`function_node.py:93-114`）、`BaseNode` が `status=error` に落とす（`base_node.py:248-263`）ため、レポートという成果物が失われる |
| クリアランス | 除去なし = `clear` / 除去あり = `redacted` / 出力放棄 = `blocked` |
| `_extra_security_gate_output()` の役割 | `S3_BLOCKED` の判定自体は `execute()` 内で行う。フックは **defence-in-depth の二重チェック**（残存があれば監査イベントを出す）に留め、ここで初めて raise する設計にはしない |

#### `blocked` の発火条件（規範・DB-1 の検証対象）

置換による除去は原理的に常に成功するため、`redacted` と `blocked` の境界を規範として定義する。
④ は除去後に**必ず再スキャンし**、次のいずれかに該当する場合のみ `blocked` とする。

| 条件 | 検知手段 | 本番 invoke 経路から到達可能か |
|---|---|---|
| ① 除去後の再スキャンで `detect_credentials()` がなお検出する | ④ が組立結果を再スキャン | **不可**（下記の理由）。fail-safe として残す |
| ② 除去マーカーが `dataset_version` または引用（provenance）に及んでいる | ④ が `dataset_version` / 引用文字列に `[REDACTED:` が含まれるかを検査 | **可** |
| ③ 除去により集計値本体（`query_result` の数値部分）が失われた | ④ が組立前の数値フィールドの欠落を検査 | **可** |

②③ 以外の除去（例: 自由記述の注記部分のみ）は `redacted` とし、出力は返す。

**条件 ① は本番 invoke 経路から発火させられない**（意図的）。上の「適用ノード」行のとおり
③ `readonly_query` はデータセット由来文字列を delta に載せる前に同じ `detect_credentials()` で
除去する義務を負うため、④ の再スキャンは必ず 0 件になる。
条件 ① は「③ の除去が壊れた場合」にのみ発火する **fail-safe** であり、
到達不能であること自体が ③ の除去責務が果たされている証拠である。
① の検証は ④ の unit テスト（credential 残存文字列を直接 ④ に渡す）で行い、境界テストの対象としない。

**DB-1 は条件 ② を本番 invoke 経路から発火させて検証する**。
Fake dataset が `dataset_version` に `detect_credentials` にヒットする文字列を含めて返す →
③ が除去して `[REDACTED:<type>]` を残す → ④ が引用に除去マーカーを検知して `blocked`。
これにより Inner State への fake injection なしで `S3_BLOCKED` を発火できる（§11）。

**「返り dict からキーを外す」だけでは不十分**: LangGraph の非 reducer チャネルは last-write-wins のため、
キーを返さないだけでは前段 ③ `readonly_query` が書いた `query_result` が State に残り、
Inner `get_output()` 経由で Outer に流出する。よって **`None` で明示的に上書きする**。

### 7-4. S-4 監査ログ

- 各ノードの `execute()` 内で、節目ごとに
  `emit_trace_event(event_type: str, payload: dict, state)`（`from shared.utils.audit_logger import emit_trace_event`）を呼ぶ。
- `node_start` / `node_complete` / `node_error` は `BaseNode.__call__()` が自動発行するため
  **テンプレート側で発行しない**（重複は監査証跡を破壊する）。
- payload に生の credential・未マスク PII を含めない（監査ログはシークレットストアではない）。

### 7-5. S-5 クラス JWT スキャン

`BaseNode.__init_subclass__()` が import 時に `scan_class_for_jwt(cls)` を実行する。
クラス属性・既定値に JWT 形状の文字列を置かない。

### 7-6. Inner ノードの status 規約

`AgentBaseGraph.route()` は `status == SUCCESS` のときのみ `post_process` に進む
（HITL 承認系ステータスも進むが本テンプレートは HITL 非使用）。
また `GraphNode.execute()` は Inner の `status == ERROR` を検知すると `merge_output()` を**実行しない**
（`error_strategy = "handle"`（§7-7）では `SubgraphError` を組み立てて `on_subgraph_error()` に渡す。
既定の `propagate` ではそれを raise する。いずれの場合も `merge_output()` には到達しない）。

したがって **Inner の全ノードは、ドメイン例外（サポート外・開示制限・曖昧・S-3 ブロック）であっても
`status = SUCCESS` を明示的に返し、異常は `error_code` + `exception_report` で表現する**。
`error_code` が既に設定されている場合、後続ノードは自身の処理をスキップして pass-through する（fail-closed）。

異常を隠蔽するわけではない: `error_code` / `exception_report` は `merge_output()` → `Graph.get_output()` で
必ず公開され、呼び出し側はレビュー可能な例外レポートを受け取る。

### 7-7. 予期しない例外に対する多層防御（`error_strategy = "handle"`）

§7-6 の「Inner では raise しない」は**方針であって保証ではない**。実装バグ・型不一致・
ライブラリ内部の例外が発生すれば `BaseNode.__call__()` の例外境界が `status = "error"` に変換する。
Inner は線形トポロジで分岐がないため、そのまま `get_output()` が `status="error"` を返し、
`GraphNode.execute()` が `SubgraphError` を raise → Outer `main` の例外境界が捕捉 → Outer `status="error"` →
`route()` が `finalize` へ → **`merge_output()` が実行されず `exception_report` が失われる**。
これは §7-2 / §7-6 が回避しようとしたまさにその経路である。

そこで `VisitorStatsQueryGraphNode` は framework 既定の `propagate` ではなく **`error_strategy = "handle"`** を宣言し、
`on_subgraph_error(state, error) -> dict` を override する。

| 項目 | 設計 |
|---|---|
| 宣言 | `error_strategy: ClassVar[str] = "handle"`（framework 既定 `propagate` からの明示的な変更。scaffold criterion 9 は "handle" 使用時の明示宣言を要求しており、本節がその宣言である） |
| 戻り値 | `status = ERROR`（**SUCCESS に偽装しない**）+ `error_code = "INTERNAL_ERROR"` + `exception_report`（Inner の `error_log` 由来のレビュー可能なレポート）+ `formatted_output = exception_report` + `error_log`（reducer チャネルへ 1 件 append）。§6 の Outer 書き込み元サブテーブルと一致させる |
| 理由 | `propagate` は例外レポートを完全に失う。SUCCESS 偽装は内部障害を成功と誤報告する。**ERROR を正直に返しつつレポートを残す**のが唯一の正解 |
| 制約 | `on_subgraph_error()` は `sub_result` を受け取らない（シグネチャは `(state, error)`）ため、Inner のドメインフィールドは持ち上げられない。**通常経路の例外レポートは §7-6 の `status=SUCCESS` + `error_code` で担保する**のが本線であり、本節はその最後の保険である |
| **`error` の型は 2 通り** | ⚠️ `error_log` 属性を無防備に読んではならない。**必ず `getattr(error, "error_log", [])` で取得する** |

**`error` に渡ってくるものが 2 通りある理由**:

| 経路 | `error` の型 | `error_log` |
|---|---|---|
| Inner が `status == ERROR` を返した | `SubgraphError`（`agent_name` / `error_log` / `trace_id` を持つ） | ✅ あり |
| `subgraph.invoke()` 自体が例外を投げた（`_handle_call_error` 経由） | **生の例外**（`AttributeError` 等がそのまま渡る） | ❌ **無い** |

後者で `error.error_log` を直接読むと `on_subgraph_error()` 内で `AttributeError` が発生し、
`BaseNode.__call__()` の例外境界に捕捉されて `status="error"` かつ `exception_report` 無しになる。
**§7-7 が防ごうとした失敗そのものが再発する**ため、`getattr()` による防御は必須である。

---

## 8. error_code 一覧

| error_code | 発生箇所 | 意味 | 出力 |
|---|---|---|---|
| `MISSING_INNER_INPUT` | `build_intake_payload` / ⓪ | `user_input` が空、または `INNER_INPUT_FIELDS` が欠落 | 例外レポート |
| `INVALID_INNER_INPUT` | `build_intake_payload` / `unpack_intake` / ⓪ | `user_input` が JSON として解釈できない、または packed 文字列が壊れている | 例外レポート |
| `MASKED_CRITICAL_FIELD` | `build_intake_payload` / ⓪ | 必須フィールドに `[MASKED]` が混入し復元不能 | 例外レポート（fail-closed） |
| `CREDENTIAL_IN_REQUEST` | ①（S-2） | リクエスト本文に credential パターンを検出 | 例外レポート（除去済み） |
| `NORMALIZE_ERROR` | ① | LLM 呼び出し失敗 / 出力が JSON として解釈不能 | 例外レポート |
| `AMBIGUOUS_QUERY` | ② | 4 次元のいずれかが一意に決まらない（暗黙の拡大解釈を禁ずる） | 例外レポート |
| `UNSUPPORTED_QUERY` | ② | 承認スキーマに存在しない次元 / 指標 / 集計方法 | 例外レポート |
| `DISCLOSURE_RESTRICTED` | ② | 開示ポリシー（最小集計単位等）に抵触 | 例外レポート |
| `DATASET_UNAVAILABLE` | ③ | 承認済みデータセットが未取り込み / 参照不能 | 例外レポート |
| `S3_BLOCKED` | ④（S-3） | 出力に credential が残存し出力を放棄 | 例外レポート（出力破棄） |
| `INTERNAL_ERROR` | `on_subgraph_error()` | Inner で予期しない例外が発生（実装バグ等）。`status = ERROR` を返す唯一の経路 | 最小限の例外レポート（§7-7） |

**判定順序（⓪ 内・固定）**: `_intake_error`（unpack 失敗 = `INVALID_INNER_INPUT`）
→ `[MASKED]` 検出（`MASKED_CRITICAL_FIELD`）→ **`json.loads()`**（`INVALID_INNER_INPUT`）
→ 必須フィールド有無（`MISSING_INNER_INPUT`）。
`[MASKED]` 検出を `json.loads()` より**先**に行う（マスクで壊れた入力を "JSON 不正" と誤報告しないため）。

---

## 9. Framework 利用コンポーネント

- [x] `InvocationContext`（`correlation_id` / `session_id` / `caller_trust_level` / `secrets`）— State には保持しない
- [x] `TrustLevel`（S-1）
- [x] `SecurityViolationError`（Outer 側でのみ使用可。Inner では使用しない → §7-2）
- [x] S-2: `_extra_security_gate_input(state: AgentState) -> AgentState`（① `request_normalize_node`）
- [x] S-3: `_extra_security_gate_output(result: dict) -> dict`（④ `provenance_assemble_node`。判定本体は `execute()` → §7-3）
- [x] `GraphNode.on_subgraph_error()` + `error_strategy = "handle"`（§7-7）
- [x] `AgentStatus`（`status` の文字列値は Enum の `.value` を使い literal を直書きしない）
- [x] S-4: `emit_trace_event()`（全ノード）
- [x] `shared.security.credential_detector.detect_credentials()`
- [ ] ConnectionPolicy（外部 HTTP 呼び出しなし — LLM は AzureOpenAIClient に委譲）

> **S-2/S-3 のノード種別による挙動（ADR-017）**
> - `FunctionNode` サブクラス → framework の `@final` ゲートが常に自動実行。拡張は `_extra_*` フックのみ
> - `GraphNode` / `RemoteAgentNode` → 意図的な no-op（上流 / リモート側のゲートが既に適用済み）
> - カスタム `BaseNode` サブクラス → `_security_gate_input()` / `_security_gate_output()` を直接実装（本テンプレートでは使用しない）

### `execute()` シグネチャ

`BaseNode.execute()` の抽象シグネチャは `execute(self, state: AgentState) -> dict` であり、
`__call__()` も `self.execute(state)` と state のみで呼ぶ。本テンプレートは**基底と同じ 1 引数シグネチャに揃える**。

> **設計判断（実装計画からの逸脱 ③）**: 実装計画には `execute(self, state, config=None)` と記載があるが、
> framework の呼び出し側は `config` を渡さないため、基底シグネチャに一致させる。

### Inner graph のファイルパス

`src/examples/domain_workflow_graph_sample.py` のコメントには
「`src/graph/domain_workflow_graph.py`（only accepted path）」とあるが、
`gate-composition` は `src/**/*.py` を AST 走査して `AgentBaseGraph` / `AutonomousBaseGraph` の
継承クラスが 1 つ以上あることのみを検査し、ファイル名は検査しない。
`gate-scaffold-integrity` の `REQUIRED_STG_COMPLETE` にも Inner graph のファイル名は含まれない。
ドメインを表す名前の方が可読性が高いため **`src/graph/visitor_stats_query_graph.py`** を採用する
（同方式は既存テンプレートでもレビューを通過している）。

> **設計判断（実装計画からの逸脱 ④・⑤）**: ④ Inner graph のファイル名（上記）。
> ⑤ 実装計画 §Architecture は「Outer の `extract_input()` でドメインフィールドを JSON パックする」としているが、
> Outer の `pre_process` は no-op であり Outer State のドメインフィールドは誰も書かないため、
> そのままでは `extract_input()` が常に `null` を返し全リクエストが `MISSING_INNER_INPUT` になる。
> よって **`extract_input()` は Outer の `user_input`（呼び出し側 JSON）から intake payload を組み立てる**
> 方式に変更する（§5-2）。

---

## 10. Design Decision Record

| 決定事項 | 選択肢 A | 選択肢 B | 採用 | 根拠 |
|---|---|---|---|---|
| L1 base type | `AgentBaseGraph` | `AutonomousBaseGraph` | **A** | 自律ループ不要。決定論的な 5 ステップワークフロー |
| Composition | Standalone | GraphNode（subgraph） | **B** | Cat 2。Outer 固定 5 スロットに 5 ノードのドメインワークフローは収まらない |
| Inner base | `BaseGraph` | `AgentBaseGraph` | **A** | Inner はカスタムトポロジ。pre/main/post スロットに写像しない |
| Error strategy | `propagate`（既定） | `handle` + `on_subgraph_error()` | **B** | 通常のドメイン例外は §7-6 の `status=SUCCESS` + `error_code` で扱うが、予期しない例外では `propagate` が `exception_report` を完全に失う。多層防御として `handle` を宣言する（§7-7） |
| 内部障害時の status | `SUCCESS` に偽装してレポートを返す | `ERROR` + `exception_report` | **B** | SUCCESS 偽装は内部障害を成功と誤報告する。ERROR を正直に返しつつレポートを残す |
| 成果物の公開キー | `merge_output()` で `output` を返す | `formatted_output` に載せる | **B** | `output` は `AgentState` に存在せず LangGraph が黙って捨てるため、`invoke()` の `output` が常に `None` になる |
| Inner のドメイン例外表現 | `status=ERROR` | `status=SUCCESS` + `error_code` | **B** | ERROR だと `SubgraphError` により `merge_output()` に到達せず例外レポートが消失する |
| intake の受け渡し | Outer State のドメインフィールドを pack | Outer `user_input` から pack | **B** | Outer `pre_process` は no-op でドメインフィールドは未設定。A では常に `MISSING_INNER_INPUT` |
| intake の符号化 | 平文 JSON | sentinel + hex | **B** | 平文は `@final` S-2 の `name` PII パターンで地域名・セグメント名が `[MASKED]` に破壊される（実測） |
| 符号化方式 | base64 | hex | **B** | base64 は JSON 先頭 `{"` が `eyJ` になり `jwt` credential パターンに必ずヒットする（実測） |
| S-2 検出時 | `raise SecurityViolationError` | redact + `error_code` | **B** | フック契約が "must not raise"。かつ raise は Inner で記録消失を招く |
| credential 検出器 | 独自 regex（3 パターン） | framework `detect_credentials`（5 パターン） | **B** | 検出幅を framework の S-3 ゲートと一致させる（狭いと自ノードでの除去漏れが framework ゲートの `RuntimeError` になる） |
| データソース | JNTO ライブ API | 事前取り込み済み承認データセット | **B** | 公式 API 非提供。承認済みデータのみを読み取り専用で扱う |
| `execute()` シグネチャ | `(state, config=None)` | `(state)` | **B** | 基底の抽象シグネチャおよび `__call__()` の呼び出し形に一致させる |
| ノード戻り値 | `{**state, ...}` | 変更キーのみの delta | **B** | `node_history` / `error_log` が `operator.add` reducer チャネルのため spread すると重複増殖する |
| Inner 実行順の観測手段 | Outer `node_history` を見る | Inner `get_output()` の `node_history` を `inner_node_history` に持ち上げる | **B** | Outer `node_history` にはバックボーン 5 ノードしか入らない。`_enrich_output()` は既定 no-op のため Inner `get_output()` が唯一の経路（§4-1・DB-6） |
| `dataset_root` の供給 | ノード内でパスを直書き | `config.yaml` → 4 ホップで注入 | **B** | 直書きはテストで Fake dataset に差し替えられず PB-3 / DB-5 が成立しない（§4-3） |
| 4 ホップで運ぶもの | `dataset_root`（パス文字列）。Inner で `VisitorStatsDataset` を生成 | `VisitorStatsDataset`（インスタンス）。Outer `register_nodes()` で 1 回生成 | **B** | Inner は invoke ごとに新規生成・再 compile されるため（`graph_node.py:135` + `base_graph.py:127-130`）、A ではメモ化がリクエスト横断で効かず毎回マニフェストを読み直す（§4-3） |
| `self.config` の供給 | L1 が `agent.yaml` を読むと期待する | `server.py` / `AgentRegistry` から明示的に注入し、`.get()` + 既定値で受ける | **B** | L1 に `agent.yaml` → `BaseGraph.config` の読み込み経路は**存在しない**（`load_config()` の呼び出し元は `autonomous_base_graph.py:83` のみ）。A かつ `config["dataset_root"]` だと `server.py` の `Graph()` で **import 時 `KeyError`** となり `/health` すら応答しない（§4-3 ホップ ⓪） |
| `blocked` の発火条件 | 「除去できないとき」（曖昧） | 再スキャン残存 / 出所喪失 / 数値喪失の 3 条件を規範化 | **B** | 置換除去は原理的に常に成功するため、A では `S3_BLOCKED` が到達不能コードになり DB-1 が書けない（§7-3） |
| DB-1 が発火させる条件 | ①（再スキャン残存） | ②（出所への除去マーカー混入） | **B** | ③ が delta 投入前に除去する義務を負うため ① は本番経路から到達不能。① は ③ の除去が壊れた場合の fail-safe と位置づけ、unit テストで検証する（§7-3） |
| LLM 生応答の保持 | State チャネルに保存 | `execute()` ローカルに留める | **B** | 生応答は Title Case を含み後続 S-2 で破壊される。かつパース前の生文は再利用されない（§6） |
| PB-3 の扱い | 外部サービスなしとして waive | 承認済みデータセットへの実データ取得に読み替えて充足 | **B** | 外部 HTTP を持たないのは §1-2 の設計意図。waive せず実ファイル取得で境界を証明する（§11） |

---

## 11. テスト観点（詳細は `docs/03_test_spec.md`）

### unit / integration

| 観点 | 内容 |
|---|---|
| unit | 各ノードの正常系 / 異常系・S-2 / S-3 フック直接テスト・LLM は Mock 注入 |
| integration | Outer → Inner の invoke 経路で `node_history` 順序・`status`・最終出力を検証 |

### framework 適合テスト（TC-01〜TC-08・scaffold 正典・8 件すべて必須）

| TC | 本テンプレートでの充足方法 |
|---|---|
| TC-01 State contract enforcement | `State` が `AgentState` を継承したフラット `TypedDict` であり、Pydantic / dataclass / 任意オブジェクトを含まないことを検証する（§6 State 制約）。invoke 後の State 値がすべてプリミティブ（`str` / `int` / `float` / `bool` / `None` / `list[str]`）であることをアサートする |
| TC-02 SecurityViolationError fires | **S-5 経路で充足する**。`BaseNode.__init_subclass__()` は import 時に `scan_class_for_jwt(cls)` を実行し、JWT 形状のクラス属性を持つサブクラスの**定義時点で** `SecurityViolationError` を raise する（実測確認済み）。テストは `pytest.raises(SecurityViolationError)` の中で JWT 形状の属性を持つ `FunctionNode` サブクラスを定義して検証する。<br>⚠️ テスト値は `JWT_PATTERN`（`eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}`）を満たすこと。**3 セグメントすべてが 10 文字以上**でなければ検出されず、テストが偽陰性になる。`"mock-jwt-for-testing"` は `JWT_EXEMPTIONS` にあるため使えない。<br>⚠️ **S-1 トラストゲートでは充足できない**: 権限不足時に `BaseNode.__call__()` は raise せず `status="error"` + `error_log` を**返す**設計であるため（実測確認済み）。<br>本テンプレートのノードは §7-2 逸脱② により自ら `SecurityViolationError` を raise しない（waiver ではなく S-5 で充足する） |
| TC-03 JWT/Credential detection | `src/` に JWT / API キーの literal を置かない。CI の `gate-credential-scan` PASS が根拠。テスト側は §5-2 の packed payload が `detect_credentials()` で 0 件であることを併せて検証する（DB-6 と共有） |
| TC-04 InvocationContext via configurable only | `InvocationContext` は **`from_state(state)` 経由でのみ生成**し、State に保持しないことを検証する（§6 State 制約・§12）。<br>⚠️ secrets は `config["configurable"]` 経路では解決されない。実ソース確認済み: `config["configurable"]` は framework 全体で `base_graph.py:125` の 1 箇所のみで、中身は `thread_id` だけである。secrets は `from_state()` → `current_secrets()`（`secrets/context.py:27-32`）の **ContextVar `_SECRETS_VAR`** から解決される。<br>したがってテストは、`bound_secrets(provider)`（`secrets/context.py`）で bind したスコープ内では `ctx.secrets.require("AZURE_OPENAI_API_KEY")` が解決し、未 bind（既定の `NullProvider`・`secrets/base.py:69-79`）では **`MissingSecret`**（`ValueError` かつ `KeyError`・`secrets/base.py:15-37`）が raise されることをアサートする |
| TC-05 Audit logging | 全ノードの `execute()` で `emit_trace_event()` を 1 回以上呼ぶ（§7-4・§4 の S-4 列）。`PassThroughNode` ×2 と `VisitorStatsQueryGraphNode` も対象 |
| TC-06 / TC-07 | scaffold 同梱 `tests/unit/test_framework_compliance_tc06_tc07.py` が担う |
| TC-08 `required_trust_level` declared and enforced | 各ノードの `required_trust_level` 宣言（§7-1）と、権限不足時に実行が拒否されることを検証する。<br>⚠️ **テストは `node(state)`（= `__call__`）を呼ぶこと。`node.execute(state)` を直接呼ぶと S-1 ゲートを迂回して偽グリーンになり、scaffold 正典では blocking defect と判定される。**<br>⚠️ 期待値は例外ではなく**戻り値**である: S-1 拒否時 `BaseNode.__call__()` は raise せず `status="error"` + `error_log` を返す（実測確認済み・TC-02 の注記と同根）。`pytest.raises` で書くと必ず失敗する。<br>⚠️ `required_trust_level` の値は `ANONYMOUS` / `VERIFIED_EXTERNAL` / `INTERNAL` の 3 値のみ（CoE 基準 13。`VERIFIED_INTERNAL` 等は import 時 `AttributeError`） |

### Proof-of-Boundary（PB-1〜PB-7・scaffold 正典・採番変更禁止）

PB の採番はフレームワーク側が正典であり、**PB-1〜PB-6 は全テンプレート必須**、
PB-7 は `hitl.enabled: true` のときのみ必須である。ドメイン固有の境界テストにこの番号を再利用してはならない。

| PB | 内容 | 本テンプレートでの担い手 |
|---|---|---|
| PB-1 | BaseNode → AuditLogger（`emit_trace_event()` が発火する） | `tests/proof_of_boundary/` に追加（全ノードで発火を検証。TC-05 と対をなす） |
| PB-2 | State serialization（invoke 後の State がプリミティブのみ） | scaffold 同梱 `test_state_safety.py` |
| PB-3 | L1 → External service | 承認済みデータセット（`src/services/visitor_stats_dataset.py`）への実データ取得で充足（§4-3）。外部 HTTP サービスは持たない |
| PB-4 | Import isolation（Level 0 を import しない） | scaffold 同梱 `test_import_isolation.py` |
| PB-5 | Checkpoint safety（checkpoint に JWT / Pydantic を含まない） | scaffold 同梱 `test_state_safety.py` |
| PB-6 | Invoke execution order（S-1 → `node_start` → S-2 → `execute()` → S-3 → `node_complete`） | scaffold 同梱 `test_pb_invoke_order.py` |
| PB-7 | HITL interrupt propagation | **auto-waived — non-HITL**（`config/agent.yaml` に `hitl` ブロックを持たない）。scaffold 同梱スタブ `test_pb7_hitl_interrupt_propagation.py` は skip される |

> **PB-3 の扱い（`docs/03_test_spec.md` に明記すること・Issue #9）**:
> scaffold 正典の PB-3 は「L1 framework 経由で外部サービス（例: Qdrant）に接続し実データを取得する」だが、
> 本テンプレートは §1-2 のスコープ外宣言により**外部 HTTP / ベクトル DB を一切持たない**
> （データソースは事前取り込み済みの組織承認済みデータセットのみ）。
> したがって PB-3 は**免除（waive）ではなく、外部サービスをローカルの承認済みデータセットに読み替えて充足する**。
> 検証内容: `VisitorStatsDataset.load_manifest()` / `query()` が
> **スタブ返り値ではなく実ファイルから**データセット名・バージョン・取り込み日・集計値を取得すること。
> `docs/03_test_spec.md` にこの読み替え根拠を記載しないと、CoE レビューで
> 「PB-3 未実施」または「stub のみのテスト」（`gate-stub-check`）と判定されるおそれがある。

### ドメイン境界テスト（DB-1〜DB-6・本テンプレート固有）

§1-2 のスコープ外宣言と §8 の error_code を裏づける中核テスト。正典 PB とは**別採番**とする。
本番 invoke 経路（Outer → Inner）で検証し、Inner State への fake injection は行わない。
したがって**アサーション対象は Outer に持ち上げられるフィールドに限る**（§5-4）。

| DB | 名前 | 証明する境界 | アサーション対象 |
|---|---|---|---|
| DB-1 | CredentialBlocking（S-3） | データセット由来の credential が出所（`dataset_version` / 引用）に混入した場合、§7-3 の**条件 ②** により `S3_BLOCKED` となり成果物が公開されないこと（Fake dataset が `dataset_version` に credential を含めて返す → ③ が除去 → ④ が除去マーカーを検知 → `blocked`）。条件 ① は本番経路から到達不能な fail-safe のため ④ の unit テストで扱う（§7-3） | `error_code == "S3_BLOCKED"` / `s3_clearance == "blocked"` / `provenance_output is None`。<br>`query_result` は §5-4 により Outer に持ち上げないため**アサーション対象にしない**（invoke 経路から観測できない）。Inner 内での `None` 消去は ④ の unit テストで検証する |
| DB-2 | DisclosurePolicy | 開示ポリシー抵触クエリは `DISCLOSURE_RESTRICTED` となり**抑制・回避できない**こと | `error_code` / `provenance_output is None` / `exception_report` に理由が含まれること |
| DB-3 | SchemaBoundary | 承認スキーマ外は `UNSUPPORTED_QUERY`、曖昧は `AMBIGUOUS_QUERY` となり**暗黙の拡大解釈・指標置換をしない**こと | `error_code` / `normalized_query` が要求外の次元に置換されていないこと |
| DB-4 | OutputGuard / Provenance | 成功出力には必ずデータセット名・バージョン・取り込み日・フィルター解釈が含まれ、**引用なしの集計値を返さない**こと | `provenance_output` / `dataset_version` / `output`（= `formatted_output`） |
| DB-5 | ReadOnly | 書き込み・更新・削除の経路が存在しないこと | `VisitorStatsDataset` の公開 API に write 系メソッドが存在しないこと（§4-3）＋ Fake dataset への write 呼び出しが 0 件であること |
| DB-6 | IntakeSymmetry / InnerOrder | `pack_intake` / `unpack_intake` が対称であり、packed payload が `detect_pii` / `detect_credentials` で 0 件であること。Inner のノード実行順が設計どおりで重複がないこと | pack/unpack は純関数として直接検証。Inner の順序は **`inner_node_history`**（§6・`merge_output()` が持ち上げる）で検証する。<br>⚠️ Outer の `node_history` は バックボーン 5 ノード（`InitializeNode` / `PassThroughNode` / `VisitorStatsQueryGraphNode` / `PassThroughNode` / `FinalizeNode`）のみで Inner の順序は含まれない |

---

## 12. 必須シークレット

| キー | 用途 | 必須 |
|---|---|---|
| `AZURE_OPENAI_API_KEY` | ① `request_normalize_node` の LLM 呼び出し（AzureOpenAIClient） | ✅ |
| `AZURE_OPENAI_ENDPOINT` | 同上（Azure リソースエンドポイント） | ✅ |
| `AZURE_OPENAI_DEPLOYMENT` | 同上（デプロイ名 = モデルルーティングキー） | ✅ |

取得は `InvocationContext.from_state(state)` → `ctx.secrets.require(...)` 経由。
**State・ログ・監査 payload には保存しない。**
