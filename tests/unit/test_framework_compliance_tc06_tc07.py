"""TC-06 / TC-07 — S-2/S-3 セキュリティゲートの非バイパス性（framework compliance）.

docs/03_test_spec.md の TC-06 / TC-07 を担う。フレームワークの契約:

- TC-06: `_security_gate_input()`（S-2）は `FunctionNode` で `@final`。サブクラスが
  これを override するとクラス定義時に `TypeError` が送出される（非バイパス）。ドメイン
  固有チェックは `_extra_security_gate_input()` でのみ追加する。
- TC-07: `_security_gate_output()`（S-3）も同様に `@final`・override で `TypeError`。

本テンプレートの leaf ノードはすべて `FunctionNode` サブクラス（Outer PassThrough /
Inner ⓪①②③④）。よって以下の 2 系統で falsifiable に検証する:

1. **設計側**: 実装ノード各クラスが `@final` ゲートを自前の `__dict__` で override して
   いない（＝ framework の `@final` メソッドをそのまま継承している）こと。override して
   いれば下の 2. の import 時点で既に TypeError で落ちているが、設計意図を明示的に固定する。
2. **framework 契約側**: `@final` ゲートを override するサブクラスを動的に定義すると
   `TypeError` になること（`FunctionNode.__init_subclass__` の実行時 final 検査）。
   `_extra_security_gate_*` を override するサブクラスは TypeError にならないこと（対照）。
"""

import pytest
from framework.nodes.function_node import FunctionNode
from framework.schemas.trust_level import TrustLevel

from src.nodes.input_expand_node import InputExpandNode
from src.nodes.pass_through_node import PassThroughNode
from src.nodes.provenance_assemble_node import ProvenanceAssembleNode
from src.nodes.query_validate_node import QueryValidateNode
from src.nodes.readonly_query_node import ReadonlyQueryNode
from src.nodes.request_normalize_node import RequestNormalizeNode

# 本テンプレートの全 leaf ノード（すべて FunctionNode サブクラス）。
IMPL_NODE_CLASSES = [
    PassThroughNode,
    InputExpandNode,
    RequestNormalizeNode,
    QueryValidateNode,
    ReadonlyQueryNode,
    ProvenanceAssembleNode,
]


@pytest.mark.parametrize("node_cls", IMPL_NODE_CLASSES)
def test_tc06_impl_nodes_do_not_override_final_input_gate(node_cls):
    """TC-06: 実装ノードは `@final` の `_security_gate_input` を自前で override しない.

    クラスの MRO 全体（framework の FunctionNode を除く src 側クラス）に
    `_security_gate_input` の再定義が無いことを確認する。再定義があれば import 時点で
    TypeError になるはずだが、設計制約として明示的に固定しておく。
    """
    for klass in node_cls.__mro__:
        if klass is FunctionNode:
            break
        assert "_security_gate_input" not in klass.__dict__, (
            f"{node_cls.__name__} が @final の _security_gate_input を override している"
            "（_extra_security_gate_input を使うこと）"
        )


@pytest.mark.parametrize("node_cls", IMPL_NODE_CLASSES)
def test_tc07_impl_nodes_do_not_override_final_output_gate(node_cls):
    """TC-07: 実装ノードは `@final` の `_security_gate_output` を自前で override しない."""
    for klass in node_cls.__mro__:
        if klass is FunctionNode:
            break
        assert "_security_gate_output" not in klass.__dict__, (
            f"{node_cls.__name__} が @final の _security_gate_output を override している"
            "（_extra_security_gate_output を使うこと）"
        )


def test_tc06_overriding_final_input_gate_raises_type_error():
    """TC-06: `_security_gate_input` を override するサブクラス定義は TypeError.

    framework の `FunctionNode.__init_subclass__` が実行時に final ゲートの override を
    検出して弾く（型チェッカ任せではなく実行時強制）ことを実証する。emit を消す等の
    改変では通らない falsifiable な検証。
    """
    with pytest.raises(TypeError, match="_security_gate_input"):

        class _BypassInput(FunctionNode):
            required_trust_level = TrustLevel.VERIFIED_EXTERNAL

            def _security_gate_input(self, state):  # @final を override（禁止）
                return state

            def execute(self, state):
                return {}


def test_tc07_overriding_final_output_gate_raises_type_error():
    """TC-07: `_security_gate_output` を override するサブクラス定義は TypeError."""
    with pytest.raises(TypeError, match="_security_gate_output"):

        class _BypassOutput(FunctionNode):
            required_trust_level = TrustLevel.VERIFIED_EXTERNAL

            def _security_gate_output(self, result):  # @final を override（禁止）
                return result

            def execute(self, state):
                return {}


def test_extra_hooks_are_the_supported_extension_point():
    """対照: `_extra_security_gate_*` を override するサブクラスは TypeError にならない.

    非バイパスが「final ゲート限定」であって拡張フック（S-2/S-3 のドメイン追加）は
    正規の拡張点であることを示す。これが通らないと上の TypeError が「全 override 禁止」の
    副作用に過ぎず TC-06/07 の意味が失われるため、対照として必須。
    """

    class _ExtendsHooks(FunctionNode):
        required_trust_level = TrustLevel.VERIFIED_EXTERNAL

        def _extra_security_gate_input(self, state):
            return state

        def _extra_security_gate_output(self, result):
            return result

        def execute(self, state):
            return {}

    # 定義が成功し（例外なし）、インスタンス化もできる。
    node = _ExtendsHooks()
    assert isinstance(node, FunctionNode)
