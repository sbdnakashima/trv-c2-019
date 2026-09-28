# Test Specification

TRV-C2-019 — Destination Visitor-Statistics Query Agent（Cat 2 / Outer + Inner）

> 本仕様は Issue #9 で作成する「テスト計画の正典」である。
> ノード単体テスト（TC 充足 / Business Logic）は Issue #3〜#8 で実装済み（実測 PASS を報告）。
> Proof-of-Boundary（PB-Cat2）とドメイン境界テスト（DB-1〜DB-6）は **Issue #10 / #11 で実装する**ため、
> 本書では「計画（🔲 planned）」として担い手・入力・期待値・アサーション対象を確定させ、
> 実装後に本ファイルの結果欄を ✅ に更新する。

## Test Strategy

- Coverage target: **90%**（本仕様作成時点の実測 **94%**。`src/api/server.py`・`src/services/service.py` を除くと **99%**。境界テスト実装後（#10/#11）に server.py も被覆される見込み）
- Test types: Unit / Proof-of-Boundary（Integration 相当）/ Live 接続（STG のみ）
- 実行コマンド: `python -m pytest tests/ -v`（PB のみ: `python -m pytest tests/proof_of_boundary/ -v`）

### 本テンプレート固有の検証方針

| 方針 | 理由 |
|---|---|
| 投入は必ず `Graph.invoke(<JSON 文字列>, ctx=InvocationContext(caller_trust_level=VERIFIED_EXTERNAL))`。state へドメインフィールドを直接注入しない | Cat 2 は Outer state が Inner に引き継がれない。`extract_input()` の 1 本の JSON 経路（§5-2）が壊れると **本番だけ** データが届かず、注入テストは緑のまま通る（既存テンプレートでの実例あり）。また S-1 トラストゲート（§7-1）は既定 ANONYMOUS を拒否するため、e2e 経路は VERIFIED_EXTERNAL を明示する |
| 差し替えてよいのは外部サービスのみ（LLM・承認済みデータセット層） | 本番に存在しないデータ経路をテストのために新設しない。DB-1 は Fake dataset を注入して `dataset_version` に credential を含めて返させる（本番経路の ③→④ をそのまま通す） |
| Inner ノードは失敗しても `status=SUCCESS` を返し例外を記録する | `GraphNode.execute()` は Inner が ERROR を返すと `merge_output()` を実行しない = 記録が消える（§7-6）。異常は `error_code` + `exception_report` で表現する |
| 静的契約（TC）と実行時挙動（PB / DB）を分けて二重に見る | 「書いてあるか」と「全分岐で実際に出るか」は別の失敗モード |
| 本番 invoke 経路で検証し、Inner State への fake injection をしない。よって DB のアサーション対象は Outer に持ち上がるフィールドに限る（§5-4） | `query_result` は §5-4 により Outer に持ち上げない（§7-3 leak 防止）。invoke 経路から観測できないため境界テストのアサーション対象にしない |

### カバレッジ除外

| ファイル | Cover | 理由 |
|---|---|---|
| `src/services/service.py` | 0% | scaffold 同梱の Service 層プレースホルダ。本テンプレートはデータアクセスを `src/services/visitor_stats_dataset.py`（承認済みデータセット層）で行うため、どのノードからも参照していない |
| `src/api/server.py` | （#9 時点）0% | HTTP アダプタ（認証境界 + `user_input` 受け渡し）。API テストは境界テスト実装（#10/#11）で追加する。それまで本番経路は e2e invoke テスト（`test_graph_composition.py`）で担保 |
| `src/examples/` | — | scaffold 同梱のリファレンス実装。実行経路に載らないため契約検査の対象からも除外する |

## Framework Compliance Tests (Mandatory)

TC-01〜TC-08 は scaffold 正典。8 件すべて必須。TC-06 / TC-07 は scaffold 同梱
`tests/unit/test_framework_compliance_tc06_tc07.py`（stg-complete 必須ファイル・Issue #12 で追加）が担う。

| TC-ID | Test | Expected Result | Result |
|-------|------|----------------|--------|
| TC-01 | State contract: `AgentState` を継承したフラット `TypedDict`（Pydantic / dataclass / 任意オブジェクトを含まない） | invoke 後の State 値がすべてプリミティブ（`str` / `int` / `float` / `bool` / `None` / `list[str]`） | ✅ PASS — `tests/unit/test_state.py`（AST 解析で `AgentState` 直継承と全ドメインフィールドの primitive 性を検証。`get_type_hints(include_extras=True)` で `Annotated` を剥がさず reducer チャネル形状も確認） |
| TC-02 | SecurityViolationError fires | クラス定義時に `SecurityViolationError` が raise される | ✅ PASS — **S-5 経路で充足**。`BaseNode.__init_subclass__()` が import 時に JWT 形状（3 セグメントすべて 10 文字以上）のクラス属性を持つサブクラス定義で raise する。`test_credential_redaction.py` の該当ケースで `pytest.raises(SecurityViolationError)` を直接検証。<br>本テンプレートのノードは §7-2 逸脱② により自ら raise しない（S-1 拒否は raise ではなく `status="error"` 戻り。TC-08 と同根） |
| TC-03 | No JWT/Credential in State | CI `gate-credential-scan`: 0 violations | ✅ PASS — CI `gate-credential-scan` 0 件。加えて §5-2 packed payload が `detect_credentials()` で 0 件であることを DB-6（#10/#11）と共有して検証する。`test_input_expand_node.py` が pack/unpack の credential 0 件を単体で確認済み |
| TC-04 | InvocationContext via `from_state()` only | ノード内で `InvocationContext(...)` を直接生成せず、`from_state(state)` 経由でのみ生成する（State に保持しない） | ✅ PASS — 直接生成は認証境界 `src/api/server.py` のみ。① `request_normalize_node` は `bound_secrets(provider)` bind スコープ内で `ctx.secrets.require("AZURE_OPENAI_API_KEY")` が解決し、未 bind では `MissingSecret` が raise されることを `test_request_normalize_node.py` で検証 |
| TC-05 | Audit logging | 全ノードの `execute()` で `emit_trace_event()` を 1 回以上呼ぶ。`node_start`/`node_complete`/`node_error` は `execute()` 本体から呼ばない（重複禁止） | ✅ PASS — 各ノードテストが `caplog` で logger `agentcore.audit` の JSON 行を捕捉して falsifiable に検証（emit を消すと FAIL）。`PassThroughNode`（`test_pass_through_node.py`）と `VisitorStatsQueryGraphNode`（`test_graph_composition.py`）も対象。PB-1（#10/#11）で全ノード × 全分岐の実行時発火を再検証する |
| TC-06 / TC-07 | S-2 `_security_gate_input()` / S-3 `_security_gate_output()` を override しない（`@final`・override で `TypeError`） | override 0 件 | ✅ PASS — `tests/unit/test_framework_compliance_tc06_tc07.py`（15 件）。全 leaf ノード 6 クラスが `@final` ゲートを自クラス `__dict__` で override していないこと（設計側）に加え、`_security_gate_input` / `_security_gate_output` を override するサブクラス定義が `FunctionNode.__init_subclass__` の実行時 final 検査で `TypeError` になること（framework 契約側・emit 消去等では通らない falsifiable 検証）を検証。対照として `_extra_security_gate_input` / `_extra_security_gate_output` を override するサブクラスは TypeError にならない（正規の拡張点）ことも確認 |
| TC-08 | `required_trust_level` declared and enforced | 各ノードが `required_trust_level` を宣言し、権限不足時に実行が拒否される | ✅ PASS — 全 FunctionNode サブクラスが自クラスで `VERIFIED_EXTERNAL` を宣言（`scripts/check_trust_level.py` PASS）。実行時は各ノードテストが `node(state)`（= `__call__`）を ANONYMOUS で呼び、`status == "error"` かつ `error_log` に S-1 拒否が現れることを検証（`node.execute()` 直呼びは S-1 を迂回するため用いない）。<br>⚠️ `GraphNode`（`VisitorStatsQueryGraphNode`）は `check_trust_level.py` が走査しない（`FunctionNode` のみ対象）ため、`test_graph_composition.py` で明示宣言を検証する |

## Proof-of-Boundary Tests (Mandatory)

PB の採番はフレームワーク側が正典であり、**PB-1〜PB-6 は全テンプレート必須**、
PB-7 は `hitl.enabled: true` のときのみ必須。ドメイン固有の境界テストにこの番号を再利用しない。

| PB-ID | Boundary | Test | Expected Result | Result |
|-------|----------|------|----------------|--------|
| PB-1 | BaseNode → AuditLogger | `emit_trace_event()` が全 invocation 経路で発火する（TC-05 と対をなす） | No silent failures | ✅ PASS — 正常系のドメインイベント発火は `test_pb_cat2_route.py::test_pb_1_audit_events_fire_across_the_full_invoke_path`（Inner 5 ドメインイベント発火・監査 payload に credential を載せない）。ライフサイクル健全性（`node_start` を出したノードが必ず `node_complete` で閉じ孤立しない）とエラー経路の網羅性（`NORMALIZE_ERROR` 経路でも 5 ドメインイベントが欠落しない）は `test_pb_realdata_audit.py`（#11）で担う |
| PB-2 | State serialization | invoke 後の State がプリミティブのみ | No Pydantic/dataclass | ✅ PASS（1件）— scaffold 同梱 `tests/proof_of_boundary/test_state_safety.py` |
| PB-3 | L1 → External service | 承認済みデータセットからの実データ取得 | Data retrieved from real files | ✅ PASS — **免除ではなく読み替えで充足**（下記脚注）。本番 invoke 経路で `test_pb_realdata_audit.py::test_pb_3_success_values_come_from_real_dataset_files_not_stubs`（成果物の集計値 12,840,000・出典・版 2025.03・取り込み日 2025-04-10 が実ファイル由来）と `::test_pb_3_dataset_reads_go_through_the_real_delegate`（実 `VisitorStatsDataset` 委譲を read≧1 で通過し、版が実 manifest と一致）で再検証。単体レベルは `test_visitor_stats_dataset.py` が確認済み |
| PB-4 | Import isolation | Level 0 を import しない | AST scan: 0 violations | ✅ PASS（1件）— scaffold 同梱 `tests/proof_of_boundary/test_import_isolation.py`（CI `gate-import-isolation` も 0 件） |
| PB-5 | Checkpoint safety | checkpoint に JWT / Pydantic を含まない | Inspection pass | ✅ PASS — scaffold 同梱 `test_state_safety.py`（PB-2 と同一ファイル）。S-2 マスク（§5-3）は `[MASKED]` 置換のみで JSON を壊さず、checkpoint に credential が残らない |
| PB-6 | Invoke execution order | `__call__()`: S-1 → `node_start` → S-2 `_security_gate_input` → `execute()` → S-3 `_security_gate_output` → `node_complete` | Order verified | ✅ PASS（1件）— scaffold 同梱 `tests/proof_of_boundary/test_pb_invoke_order.py` |
| PB-7 | HITL interrupt propagation *(conditional)* | `hitl.enabled: true` のときのみ必須。それ以外は **Auto-waived — non-HITL** | `GraphInterrupt` propagates; `status` is not `error` | **Auto-waived — non-HITL**（2件 skip）— `config/agent.yaml` に `hitl` ブロックなし。本テンプレートは人手判断を「レビュー可能な例外レポート」として外部に委ね、グラフ内で待たない。scaffold 同梱スタブ `test_pb7_hitl_interrupt_propagation.py` は skip される |

> **Pre-CoE gate checklist:** PB-1〜PB-6 are mandatory. Evaluate PB-7 only for HITL-enabled templates. When neither HITL setting is true, mark PB-7 **Auto-waived — non-HITL**; its test is not required and must not block the gate.
>
> **PB-3 の読み替え根拠（CoE レビュー向け明記）**: 本テンプレートは §1-2 で外部 HTTP / ベクトル DB を持たないと宣言している。したがって PB-3 は**免除（waive）ではなく、外部サービスをローカルの承認済みデータセットに読み替えて充足する**。`VisitorStatsDataset` が**スタブではなく実ファイルから**メタデータと集計値を返すことを検証するため、`gate-stub-check`（stub のみのテスト禁止）にも抵触しない。

### PB-Cat2: Outer → Inner 受け渡し境界（本テンプレート固有・必須・Issue #10/#11）

Cat 2 の Outer state は Inner に引き継がれない（`GraphNode.extract_input()` の戻り値が Inner の
`user_input` キーにしか載らない）。この境界は PB-1〜7 のどれにも該当しないため独立して検証する
— `tests/proof_of_boundary/test_pb_cat2_route.py`（planned）。

| ID | Test | Expected Result | Result |
|---|---|---|---|
| PB-C2-1 | `analyst_request` / `approved_schema` を載せた intake が Inner に届き provenance 出力まで到達する | `inner_node_history` が Inner 5 ノード通過を示し、成果物にデータセット出所が載る | ✅ PASS — `test_pb_cat2_route.py::test_pb_c2_1_intake_reaches_provenance_with_dataset_source` |
| PB-C2-2 | ドメイン 2 フィールドが pack/unpack 境界（sentinel+hex）を越えて復元される | ① `request_normalize` に正規化対象の自然言語リクエストと承認スキーマが届く | ✅ PASS — `test_pb_cat2_route.py::test_pb_c2_2_domain_fields_cross_pack_unpack_boundary` |
| PB-C2-3 | Outer が ERROR（例: intake 不正）のとき Inner の外部サービス（LLM）に到達しない | `error_code` 設定・LLM 未呼び出し・provenance 空 | ✅ PASS — `test_pb_cat2_route.py::test_pb_c2_3_outer_error_never_reaches_inner_llm` |
| PB-C2-4 | Inner の例外記録（サポート外・開示制限・曖昧）が Outer の出力に戻る | `error_code` / `exception_report` が Outer 出力（`get_output()`）に現れる | ✅ PASS — `test_pb_cat2_route.py::test_pb_c2_4_inner_exception_report_returns_to_outer_output` |
| PB-C2-5 | S-3 ブロック（DB-1）が握り潰されずに Outer へ戻る | `error_code = S3_BLOCKED` / `s3_clearance = blocked` / `provenance_output is None` | ✅ PASS — `test_pb_cat2_route.py::test_pb_c2_5_s3_block_propagates_to_outer_unswallowed` |
| PB-C2-6 | LLM 障害が「候補なし」に倒れず記録される | `status = SUCCESS` かつ `NORMALIZE_ERROR`（記録を Outer へ届けるため）。鍵欠落は `MissingSecret` の型名のみ記録 | ✅ PASS — `test_pb_cat2_route.py::test_pb_c2_6_llm_failure_records_normalize_error_not_no_candidates` |

## Domain Boundary Tests（DB-1〜DB-6・本テンプレート固有・Issue #10/#11）

§1-2 のスコープ外宣言と §8 の error_code を裏づける中核テスト。正典 PB とは**別採番**。
本番 invoke 経路（Outer → Inner）で検証し、Inner State への fake injection は行わない。
アサーション対象は Outer に持ち上げられるフィールドに限る（§5-4）。

| DB | 名前 | 証明する境界 | アサーション対象 | Result |
|---|---|---|---|---|
| DB-1 | CredentialBlocking（S-3） | データセット由来 credential が出所（`dataset_version` / 引用）に混入した場合、§7-3 条件 ② により `S3_BLOCKED` となり成果物を公開しない（Fake dataset が `dataset_version` に credential を含めて返す → ③ が除去して `[REDACTED:<type>]` を残す → ④ が引用に除去マーカーを検知して `blocked`）。条件 ① は本番経路から到達不能な fail-safe のため ④ の unit テストで扱う | `error_code == "S3_BLOCKED"` / `s3_clearance == "blocked"` / `provenance_output is None`。`query_result` は §5-4 により持ち上げないためアサーションしない（Inner 内の `None` 消去は ④ unit で検証済み） | ✅ PASS — `test_db_boundary.py::test_db_1_credential_in_dataset_version_blocks_output`（④ unit の condition ① は `test_provenance_assemble_node.py` で実装済み） |
| DB-2 | DisclosurePolicy | 開示ポリシー抵触クエリは `DISCLOSURE_RESTRICTED` となり抑制・回避できない | `error_code` / `provenance_output is None` / `exception_report` に理由が含まれる | ✅ PASS — `test_db_boundary.py::test_db_2_disclosure_restriction_cannot_be_suppressed` / `::test_db_2_disclosure_restriction_via_filter_is_not_evaded` |
| DB-3 | SchemaBoundary | 承認スキーマ外は `UNSUPPORTED_QUERY`、曖昧は `AMBIGUOUS_QUERY`。暗黙の拡大解釈・指標置換をしない | `error_code` / `normalized_query` が要求外の次元に置換されていないこと | ✅ PASS — `test_db_boundary.py::test_db_3_unsupported_metric_is_not_silently_substituted` / `::test_db_3_ambiguous_dimension_is_reported_not_widened`（② の validation_result 契約は `test_query_validate_node.py` で実装済み） |
| DB-4 | OutputGuard / Provenance | 成功出力に必ずデータセット名・バージョン・取り込み日・フィルター解釈が含まれ、引用なしの集計値を返さない | `provenance_output` / `dataset_version` / `formatted_output` | ✅ PASS — `test_db_boundary.py::test_db_4_success_output_always_carries_provenance`（④ の組立契約は `test_provenance_assemble_node.py` で実装済み） |
| DB-5 | ReadOnly | 書き込み・更新・削除の経路が存在しない | `VisitorStatsDataset` の公開 API に write 系メソッドが無いこと（§4-3）＋ Fake dataset への write 呼び出しが 0 件 | ✅ PASS — `test_db_boundary.py::test_db_5_dataset_has_no_write_api_and_none_is_called`（read-only 構造は `test_visitor_stats_dataset.py` で実装済み） |
| DB-6 | IntakeSymmetry / InnerOrder | `pack_intake` / `unpack_intake` が対称で、packed payload が `detect_pii` / `detect_credentials` で 0 件。Inner のノード実行順が設計どおりで重複がない | pack/unpack は純関数として直接検証（`test_input_expand_node.py` で実装済み✅）。Inner 順序は `inner_node_history`（`merge_output()` が持ち上げる）で検証。⚠️ Outer の `node_history` はバックボーン 5 ノードのみで Inner 順序を含まない | ✅ PASS — `test_db_boundary.py::test_db_6_inner_node_order_is_correct_and_not_duplicated` / `::test_db_6_intake_symmetry_carries_no_credential_on_the_wire`（pack/unpack 対称性・PII/credential 0 件は `test_input_expand_node.py` で実装済み） |

## Business Logic Tests（ノード単体・Issue #3〜#8 で実装済み）

| BL-ID | Test | Input | Expected Result | Result |
|-------|------|-------|----------------|--------|
| BL-01 | ⓪ intake 展開（unpack） | packed intake（sentinel+hex） | `analyst_request` / `approved_schema` を復元。sentinel 不正・hex 破損は `INVALID_INNER_INPUT`、欠落は `MISSING_INNER_INPUT` | ✅ PASS — `test_input_expand_node.py` |
| BL-02 | ① リクエスト正規化（LLM・S-2） | 自然言語リクエスト + 承認スキーマ | 4 次元（period/region/segment/metric）+ filters に正規化。値未確定は `None`（② が曖昧判定）。LLM 障害は `NORMALIZE_ERROR`（`status=SUCCESS`・型名のみ記録） | ✅ PASS — `test_request_normalize_node.py` |
| BL-03 | ① S-2 入力ゲート | credential 形状を含む入力 | framework `detect_credentials()` で除去してから返す（生 + JSON デコード後の両方を走査）。マスク破壊は fail-closed | ✅ PASS — `test_request_normalize_node.py` |
| BL-04 | ② クエリ検証 | 承認スキーマ外 / 曖昧（`None` 次元） | `UNSUPPORTED_QUERY` / `AMBIGUOUS_QUERY`。`validation_result`（`{"valid","reason"}`）のみ返し `normalized_query` を書き戻さない（DB-3） | ✅ PASS — `test_query_validate_node.py` |
| BL-05 | ③ 読み取り専用クエリ | 検証済みクエリ | 承認済みデータセットから集計値を取得。データセット由来文字列は `redact_json_document` で除去してから delta に載せる（§7-3）。参照不能は raise せず `DATASET_UNAVAILABLE` | ✅ PASS — `test_readonly_query_node.py` |
| BL-06 | ③ S-3 除去（2 段） | credential を含むデータセット行 | 全 leaf を serialized-context で判定 + reform 残存時 wholesale フォールバック。片方だけだと #4 shape か非 citation leaf が漏れる | ✅ PASS — `test_readonly_query_node.py` / `test_credential_redaction.py` |
| BL-07 | ④ 出所組立（S-3 判定） | ③ の集計結果 + メタデータ | データセット名・バージョン・取り込み日・フィルター解釈付き Markdown を組立。除去マーカーが `dataset_version` / 引用に及べば `blocked`（条件 ②）、再スキャン残存は fail-safe（条件 ①） | ✅ PASS — `test_provenance_assemble_node.py` |
| BL-08 | ④ 例外レポート内容制約 | 検出文字列を含む状況 | `exception_report` に検出文字列そのものを引用しない（型名・フィールド名・件数のみ）。引用すると `@final` S-3 で自己ブロック | ✅ PASS — `test_provenance_assemble_node.py` |
| BL-09 | PassThroughNode（Outer pre/post） | 任意 state | state を素通しし delta を返さない。`emit_trace_event()` を発火 | ✅ PASS — `test_pass_through_node.py` |
| BL-10 | Graph 合成（Outer + Inner 4-hop） | JSON intake（VERIFIED_EXTERNAL） | Outer backbone 5 ノード → Inner 5 ノードを走破し、`inner_node_history` に 5 件。`merge_output` が status / formatted_output / ドメインフィールドを持ち上げ、`query_result` は持ち上げない | ✅ PASS — `test_graph_composition.py`（20件） |

## Test Execution Summary（Issue #11 時点）

- Execution date: 2026-07-31
- Total tests: 365（Unit 340 / Proof-of-Boundary 25）
- Pass: 363 / Fail: 0 / Skip: 2（いずれも PB-7 非 HITL・`test_pb7_hitl_interrupt_propagation.py` の 2 関数）
- Coverage: **94%**（`src/api/server.py`・`src/services/service.py` を除くと **99%**）
- 境界テストの実装完了: PB-Cat2（`test_pb_cat2_route.py`・7件）/ DB-1〜DB-6（`test_db_boundary.py`・8件）は Issue #10、PB-1 残分岐・PB-3（`test_pb_realdata_audit.py`・4件）は Issue #11。scaffold 同梱の PB-2/4/5/6（各 1 件）と合わせ PB は全 mandatory 分が ✅（PB-7 は非 HITL で Auto-waived）

### 補助検証

| 項目 | 結果 |
|---|---|
| `ruff check src/` | ✅ PASS |
| `scripts/check_trust_level.py src` | ✅ PASS |
| `scripts/check_stub_tests.py` | ✅ PASS（stub 0 件） |
| `scripts/check_dep_pinning.py` | ✅ PASS（exact pin） |
| `scripts/check_oss_license.py` | ✅ PASS |
| `scripts/check_cat_consistency.py` | ✅ PASS（TRV-C2-019 / Cat 2） |
| `mypy src/` | ⚠️ 実行経路（`src/nodes/` `src/graph/` `src/schemas/` `src/services/`）に本物のエラーなし。残るは framework py.typed 欠如の baseline（`cannot subclass ... has type "Any"` 等）と、実行経路外の未整備アノテーション（`src/examples/` の `[type-arg]` / `src/api/server.py` の `[no-untyped-def]`・#10/#11 の API テスト実装時に解消予定）。scaffold CI に mypy ゲートはない |
