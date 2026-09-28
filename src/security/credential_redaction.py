"""AgentCore Platform v1.0 — S-3 credential removal (docs/02_design.md §7-3)

Shared by every node that puts externally-sourced text into its return delta
(⓪ caller-derived, ① LLM output, ③ dataset-derived, ④ assembled output).

Why removal must happen inside execute(), before returning:
    FunctionNode._security_gate_output() is @final and scans every str value of
    the returned dict with detect_credentials(), raising RuntimeError on a hit
    (function_node.py:93-114). BaseNode.__call__() converts that into
    status="error" (base_node.py:248-263), and §7-6's exception report — the
    reviewable artifact this template exists to produce — is lost. Removing
    first keeps the gate a defence-in-depth backstop instead of a trip wire.

Removal is *replacement*, never truncation of the whole field: the surrounding
analyst request has to stay readable for the exception report.
"""

import json
from typing import Any, Optional

from shared.security.credential_detector import detect_credentials

# ④ detects this prefix in dataset_version / provenance citations to decide
# `blocked` vs `redacted` (§7-3 condition ②), so the shape is a contract.
REDACTION_MARKER_PREFIX = "[REDACTED:"

# Removal is re-run until the detector is quiet. One merged pass is provably
# enough for today's patterns, but the contract worth enforcing is "the output
# contains no detectable credential" — provable by test — rather than "my
# analysis of the current pattern set says one pass suffices". The cap keeps a
# pathological pattern (one whose replacement re-creates a match) from spinning.
_MAX_PASSES = 4

# Label used when the cap is exhausted and the whole value is discarded.
_UNRESOLVED_LABEL = "unresolved"


def _merge_spans(findings: list[dict[str, Any]]) -> list[tuple[int, int, str]]:
    """Collapse overlapping findings into disjoint ``(start, end, label)`` spans.

    Two patterns routinely match overlapping substrings of the same text — a
    `conn_string` strictly contains the `openai_key` embedded in its password
    field, and a `bearer_token` contains the key it carries. Replacing them
    independently is wrong in both directions: applied inner-first the outer
    span's tail survives and still matches, applied outer-first the inner
    replacement splices into a marker already written. Merging first makes the
    order irrelevant.

    The label is taken from the earliest-starting, longest finding of the group,
    so the widest classification wins (`conn_string` over the key inside it).
    """
    ordered = sorted(findings, key=lambda f: (f["start"], -f["end"]))
    merged: list[tuple[int, int, str]] = []
    for finding in ordered:
        start, end, label = finding["start"], finding["end"], finding["type"]
        if merged and start <= merged[-1][1]:
            prev_start, prev_end, prev_label = merged[-1]
            merged[-1] = (prev_start, max(prev_end, end), prev_label)
        else:
            merged.append((start, end, label))
    return merged


def redact_credentials(text: Optional[str]) -> tuple[Optional[str], int]:
    """Replace each credential span in *text* with ``[REDACTED:<type>]``.

    Returns ``(redacted_text, removed_count)``. Non-str and empty input passes
    through with count 0 so callers can redact optional channels unconditionally
    without branching on None.

    Use :func:`redact_json_document` instead for a serialized JSON string — a
    flat replacement here can swallow the quote and comma that follow a value
    (`conn_string`'s trailing `[^\\s]{8,}` is greedy) and leave the document
    unparseable for downstream nodes.
    """
    if not isinstance(text, str) or not text:
        return text, 0

    redacted = text
    removed = 0
    for _ in range(_MAX_PASSES):
        findings = detect_credentials(redacted)
        if not findings:
            return redacted, removed
        redacted, count = _apply_spans(redacted, findings)
        removed += count
    # Cap exhausted — a pattern whose replacement re-creates a match. Discard the
    # whole value rather than return text the detector still flags: the caller
    # feeds this straight into a delta the @final S-3 gate rejects, and a partial
    # result there costs the exception report. Unreachable with today's patterns.
    return f"{REDACTION_MARKER_PREFIX}{_UNRESOLVED_LABEL}]", removed + 1


def _apply_spans(text: str, findings: list[dict[str, Any]]) -> tuple[str, int]:
    """Replace *findings* in *text*, merged, right-to-left.

    Right-to-left because replacing left-to-right shifts every later offset by
    the length delta between the match and the marker.
    """
    redacted = text
    removed = 0
    for start, end, label in reversed(_merge_spans(findings)):
        redacted = redacted[:start] + f"{REDACTION_MARKER_PREFIX}{label}]" + redacted[end:]
        removed += 1
    return redacted, removed


def _serialization_tail(depth: int) -> str:
    """Longest non-space run ``json.dumps`` can append after a string leaf.

    A leaf is followed by its closing quote, one closer per enclosing container,
    and possibly a comma; the `, ` separator's space then ends the run. `mysql://`
    + 7 characters is invisible to `detect_credentials()` on the leaf alone but
    matches `conn_string`'s greedy `[^\\s]{8,}` once `",` is appended — and it is
    the *serialized* document the @final S-3 gate scans, so leaf-local detection
    lets a benign data-source identifier trip the gate.

    Probing with the longest possible tail is deliberately conservative: a leaf a
    couple of characters short of a threshold gets redacted at a position where it
    would in fact have been safe. Over-redacting removes and keeps the document
    parseable; under-redacting costs the exception report.
    """
    return '"' + "}" * depth + ","


def _redact_leaf(leaf: str, depth: int) -> tuple[str, int]:
    """Redact *leaf* judged in the context the S-3 gate will see it in."""
    tail = _serialization_tail(depth)
    redacted = leaf
    removed = 0
    for _ in range(_MAX_PASSES):
        # Only findings that begin inside the leaf are ours to remove — no
        # pattern can start in the tail (each needs a literal prefix that the
        # tail's `"`, `}` and `,` cannot supply).
        findings = [
            {"type": f["type"], "start": f["start"], "end": min(f["end"], len(redacted))}
            for f in detect_credentials(redacted + tail)
            if f["start"] < len(redacted)
        ]
        if not findings:
            return redacted, removed
        redacted, count = _apply_spans(redacted, findings)
        removed += count
    return f"{REDACTION_MARKER_PREFIX}{_UNRESOLVED_LABEL}]", removed + 1


def redact_json_document(raw: Optional[str]) -> tuple[Optional[str], int]:
    """Redact the string leaves of a serialized JSON document, keeping it parseable.

    §7-3 requires removal at string-leaf granularity. Redacting the serialized
    form instead destroys the document: `conn_string` matches every non-space
    character after the scheme, so one credential in a value eats the closing
    quote, the comma and the following key — and the caller then hands a broken
    schema to ② with no error_code raised.

    Each leaf is nonetheless judged *in serialized context* (see
    :func:`_serialization_tail`), because that is the string the @final gate
    scans. Removal is leaf-local, the verdict is not.

    Falls back to a flat :func:`redact_credentials` when *raw* is not a JSON
    object/array, because the caller's own validation is what reports malformed
    input; this function must still strip the credential either way.
    """
    if not isinstance(raw, str) or not raw:
        return raw, 0

    try:
        parsed = json.loads(raw)
    except ValueError:
        return redact_credentials(raw)
    if not isinstance(parsed, (dict, list)):
        return redact_credentials(raw)

    removed = 0

    def walk(node: Any, depth: int) -> Any:
        nonlocal removed
        if isinstance(node, str):
            cleaned, count = _redact_leaf(node, depth)
            removed += count
            return cleaned
        if isinstance(node, dict):
            # Keys are redacted too: a credential pasted into a key would
            # otherwise survive re-serialization and trip the @final gate.
            return {walk(key, depth + 1): walk(value, depth + 1) for key, value in node.items()}
        if isinstance(node, list):
            return [walk(item, depth + 1) for item in node]
        return node

    scrubbed = json.dumps(walk(parsed, 1), ensure_ascii=False)
    if removed == 0:
        # Nothing to remove — return the caller's exact bytes rather than a
        # re-serialized equivalent, so key order and spacing survive untouched.
        return raw, 0
    return scrubbed, removed
