"""AgentCore Platform v1.0 — S-3 credential removal tests (docs/02_design.md §7-3)"""

import json

import pytest
from shared.security.credential_detector import detect_credentials

from src.security import credential_redaction as mod
from src.security.credential_redaction import (
    REDACTION_MARKER_PREFIX,
    redact_credentials,
    redact_json_document,
)

# Fabricated, structurally-valid samples. Never use a real credential in tests —
# and never a JWT shape in a class body or module constant (S-5 scans those).
DUMMY_OPENAI_KEY = "sk-abcdefghijklmnopqrstuvwx"
FAKE_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"


def test_sample_credentials_are_actually_detected():
    """Guards every other test here: a sample the detector ignores proves nothing.

    If the detector's patterns change and these stop matching, the redaction
    tests below would pass vacuously on text that never contained a credential.
    """
    assert detect_credentials(DUMMY_OPENAI_KEY), "sample no longer matches any pattern"
    assert detect_credentials(FAKE_AWS_KEY), "sample no longer matches any pattern"


def test_credential_is_replaced_and_counted():
    text = f"query for {DUMMY_OPENAI_KEY} please"
    redacted, removed = redact_credentials(text)

    assert removed == 1
    assert DUMMY_OPENAI_KEY not in redacted
    assert REDACTION_MARKER_PREFIX in redacted
    # Replacement, not truncation: the surrounding request must stay reviewable.
    assert redacted.startswith("query for ")
    assert redacted.endswith(" please")


def test_redacted_output_passes_the_detector():
    """The point of the exercise — the @final S-3 gate must find nothing left."""
    redacted, _ = redact_credentials(f"a {DUMMY_OPENAI_KEY} b {FAKE_AWS_KEY} c")
    assert detect_credentials(redacted) == []


def test_every_credential_in_a_multi_hit_string_is_removed():
    """Right-to-left application: a left-to-right loop corrupts later offsets."""
    text = f"first {DUMMY_OPENAI_KEY} then {FAKE_AWS_KEY} end"
    redacted, removed = redact_credentials(text)

    assert removed == 2
    assert DUMMY_OPENAI_KEY not in redacted
    assert FAKE_AWS_KEY not in redacted
    assert redacted.startswith("first ")
    assert redacted.endswith(" end")
    # Order preserved — proof the spans were mapped back to the right places.
    assert redacted.index("first") < redacted.index("then") < redacted.index("end")


def test_nested_findings_are_merged_into_one_marker():
    """`Bearer sk-…` matches bearer_token *and* openai_key over the same tail.

    The spans are merged before replacement, so the widest classification wins
    and exactly one marker is written.
    """
    text = f"Authorization: Bearer {DUMMY_OPENAI_KEY}"
    spans = [(f["start"], f["end"]) for f in detect_credentials(text)]
    assert len(spans) == 2, f"input no longer produces overlapping findings: {spans}"

    redacted, removed = redact_credentials(text)
    assert removed == 1
    assert redacted == f"Authorization: {REDACTION_MARKER_PREFIX}bearer_token]"
    assert detect_credentials(redacted) == []


def test_a_strictly_contained_finding_does_not_orphan_the_outer_span():
    """The regression that shipped once: conn_string ⊃ openai_key.

    conn_string(0,74) strictly contains openai_key(18,45) and ends *later*. The
    earlier implementation applied the inner span first and then dropped the
    outer one as "overlapping", leaving `postgresql://user:[REDACTED:…]@host/db`
    — which still matches conn_string. The @final S-3 gate then raised, BaseNode
    turned that into status="error", and the exception report was lost.
    """
    text = f"postgresql://user:{DUMMY_OPENAI_KEY}@db.example.com:5432/visitors"
    kinds = {f["type"] for f in detect_credentials(text)}
    assert kinds == {"conn_string", "openai_key"}, f"input shape changed: {kinds}"

    redacted, removed = redact_credentials(text)
    assert removed == 1
    assert detect_credentials(redacted) == [], "the S-3 gate would raise on this"
    assert redacted == f"{REDACTION_MARKER_PREFIX}conn_string]"


def test_two_credentials_that_merely_touch_are_still_collapsed_into_one_marker():
    """Spans that abut without overlapping (`start == prev_end`) merge as well.

    Both readings remove everything — replacement runs right-to-left, so two
    adjacent spans replaced separately do not disturb each other. What differs
    is the marker: merging writes one, keeping the boundary condition on the
    single side that cannot splice a replacement into a span not yet written.
    Pinned because the difference is invisible to every other test here.
    """
    text = FAKE_AWS_KEY + DUMMY_OPENAI_KEY
    spans = sorted((f["start"], f["end"]) for f in detect_credentials(text))
    assert len(spans) == 2 and spans[0][1] == spans[1][0], f"no longer adjacent: {spans}"

    redacted, removed = redact_credentials(text)
    assert removed == 1
    assert redacted == f"{REDACTION_MARKER_PREFIX}aws_key]"
    assert detect_credentials(redacted) == []


def test_no_two_findings_can_begin_at_the_same_offset():
    """The premise behind ``-f["end"]`` in the merge sort key.

    Which label a merged group carries is decided by the first finding in sort
    order, so the tie-break only matters if a tie is reachable. Every pattern
    is anchored on a distinct literal prefix, so it is not — the descending
    length is defence against a future pattern, not live behaviour. Asserted
    rather than reasoned about, because the last time a claim about these
    patterns went unchecked (`Bearer` and whitespace) the reasoning was wrong.
    """
    fragments = [
        DUMMY_OPENAI_KEY, FAKE_AWS_KEY, "eyJ" + "x" * 12, "Bearer " + "y" * 20,
        "mysql://" + "z" * 12, "postgresql://u:" + DUMMY_OPENAI_KEY + "@h/db",
        "redis://" + FAKE_AWS_KEY + ":6379", " ", "-", "訪問者数",
    ]
    for left in fragments:
        for right in fragments:
            for middle in ("", " ", "-"):
                text = left + middle + right
                starts = [f["start"] for f in detect_credentials(text)]
                assert len(starts) == len(set(starts)), (text, starts)


@pytest.mark.parametrize("depth", [1, 2, 5])
def test_no_finding_can_begin_inside_the_serialization_tail(depth):
    """The premise behind ``if f["start"] < len(redacted)`` in _redact_leaf.

    The probe appends `"`, `}` and `,` — none of which can open any pattern —
    so a finding that starts in the tail cannot occur and the filter never
    actually excludes anything. It stays because the tail is a contract with
    json.dumps, not with the pattern set.
    """
    tail = mod._serialization_tail(depth)
    leaves = ["", "訪問者数", DUMMY_OPENAI_KEY, "mysql://" + "z" * 8, "Bearer " + "y" * 20,
              "mysql://abc", 'ends with a quote "', "ends with a brace }", "ends with a comma ,"]
    for leaf in leaves:
        for f in detect_credentials(leaf + tail):
            assert f["start"] < len(leaf), (leaf, tail, f)


def test_partially_overlapping_findings_are_merged_not_skipped(monkeypatch):
    """Merging is structural, not tuned to today's pattern set.

    No current pattern pair overlaps *partially* (each containment case ends at
    or after the span it contains), so a fabricated detector is the only way to
    exercise that branch. Skipping the second span would leave its tail (offsets
    20-30) in place; splicing it in after the first would write a marker inside a
    marker and leave a dangling `[REDACTED:` fragment — which is exactly what ④
    inspects to choose `blocked` over `redacted` (§7-3 condition ②).

    The fake answers once and then reports clean, mirroring a real detector after
    removal; a constant fake would instead exercise the re-scan cap.
    """
    text = "0123456789abcdefghijklmnopqrstuvwxyz"
    calls: list[str] = []

    def fake_detect(value):
        calls.append(value)
        if len(calls) > 1:
            return []
        return [
            {"type": "early", "value": "", "start": 5, "end": 20},
            {"type": "late", "value": "", "start": 15, "end": 30},
        ]

    monkeypatch.setattr(mod, "detect_credentials", fake_detect)
    redacted, removed = redact_credentials(text)

    assert removed == 1, "the overlapping pair must collapse into a single span"
    assert redacted == f"01234{REDACTION_MARKER_PREFIX}early]{text[30:]}"
    assert redacted.count(REDACTION_MARKER_PREFIX) == redacted.count("]")


def test_removal_is_re_scanned_until_the_detector_is_quiet(monkeypatch):
    """The contract is "no detectable credential left", not "one pass ran".

    A pattern whose replacement re-creates a match would otherwise return dirty
    text straight into the @final gate. The cap bounds the loop rather than
    trusting the pattern set to converge.
    """
    calls: list[str] = []

    def fake_detect(value):
        calls.append(value)
        # Report a fresh single-char finding on the first two passes only.
        if len(calls) <= 2:
            return [{"type": f"pass{len(calls)}", "value": "", "start": 0, "end": 1}]
        return []

    monkeypatch.setattr(mod, "detect_credentials", fake_detect)
    redacted, removed = redact_credentials("abcdef")

    assert removed == 2, "each pass's findings must be counted"
    assert redacted.startswith(f"{REDACTION_MARKER_PREFIX}pass2]")
    assert len(calls) == 3, "the loop must stop as soon as the detector is quiet"


def test_the_rescan_loop_is_bounded_and_fails_closed(monkeypatch):
    """A never-quiet detector must terminate — and must not return dirty text.

    The caller feeds this straight into a delta the @final S-3 gate rejects, so
    "best effort, still flagged" costs the exception report. The whole value is
    discarded instead. Unreachable with today's patterns; this pins the contract
    the module docstring claims, rather than trusting the pattern set to converge.
    """
    monkeypatch.setattr(mod, "detect_credentials", lambda _: [
        {"type": "forever", "value": "", "start": 0, "end": 1},
    ])
    redacted, removed = redact_credentials("0123456789")

    assert redacted == f"{REDACTION_MARKER_PREFIX}unresolved]"
    assert removed == mod._MAX_PASSES + 1
    # No surviving original text, and no spliced marker fragments.
    assert redacted.count(REDACTION_MARKER_PREFIX) == redacted.count("]") == 1


def test_clean_text_is_returned_unchanged():
    text = "Osaka Prefecture の Foreign Visitors 数を月次で"
    assert redact_credentials(text) == (text, 0)


def test_none_and_empty_pass_through_without_branching():
    """Callers redact optional channels unconditionally, so None must survive."""
    assert redact_credentials(None) == (None, 0)
    assert redact_credentials("") == ("", 0)


# --- redact_json_document (§7-3 "文字列リーフ単位") --------------------------


def test_flat_redaction_really_does_destroy_a_json_document():
    """The premise of redact_json_document — assert it, don't assume it.

    conn_string's trailing `[^\\s]{8,}` is greedy over every non-space character,
    so a credential in a value swallows the closing quote, the comma and the
    following key. If this ever stops being true the leaf-wise path is dead code.
    """
    document = json.dumps({"source": "postgresql://u:dummy-pw@host/db", "metrics": ["visitors"]})
    flat, _ = redact_credentials(document)

    with pytest.raises(ValueError):
        json.loads(flat)


def test_leaf_wise_redaction_keeps_the_document_parseable():
    document = json.dumps({
        "source": f"postgresql://u:{DUMMY_OPENAI_KEY}@host/visitors",
        "dimensions": ["region", "month"],
        "metrics": ["visitors"],
        "limit": 500,
    })
    scrubbed, removed = redact_json_document(document)

    assert removed == 1
    parsed = json.loads(scrubbed)
    assert parsed["source"].startswith(REDACTION_MARKER_PREFIX)
    # Every sibling survives, including the non-str leaf.
    assert parsed["dimensions"] == ["region", "month"]
    assert parsed["metrics"] == ["visitors"]
    assert parsed["limit"] == 500
    assert detect_credentials(scrubbed) == [], "the S-3 gate would raise on this"


def test_nested_and_key_positions_are_redacted():
    """Credentials hide in list items, nested objects and even keys."""
    document = json.dumps({
        "filters": [{"note": f"token {FAKE_AWS_KEY}"}],
        DUMMY_OPENAI_KEY: "value",
    })
    scrubbed, removed = redact_json_document(document)

    assert removed == 2
    assert FAKE_AWS_KEY not in scrubbed
    assert DUMMY_OPENAI_KEY not in scrubbed
    assert detect_credentials(scrubbed) == []
    assert json.loads(scrubbed)["filters"][0]["note"].startswith("token ")


def test_a_clean_document_is_returned_byte_for_byte():
    """No credential means no re-serialization — key order and spacing survive."""
    document = '{"z": 1,   "a": "Osaka Prefecture"}'
    assert redact_json_document(document) == (document, 0)


@pytest.mark.parametrize("raw", [
    "not json at all",
    '{"unterminated": ',
    '"a bare string"',
    "42",
])
def test_non_object_input_still_gets_its_credential_removed(raw):
    """Malformed input is the caller's error to report — but the credential must
    still go. Falling through without redacting would hand the @final gate a live
    credential on precisely the inputs that are already suspicious."""
    text = f"{raw} {DUMMY_OPENAI_KEY}"
    scrubbed, removed = redact_json_document(text)

    assert removed == 1
    assert detect_credentials(scrubbed) == []


def test_json_document_none_and_empty_pass_through():
    assert redact_json_document(None) == (None, 0)
    assert redact_json_document("") == ("", 0)


# --- leaves judged in serialized context, because that is what the gate scans ---

# `mysql://` + N non-space characters. conn_string needs 8 after the scheme, and
# serialization appends `",` — so the 6-7 band is invisible on the leaf alone yet
# matches once the document is assembled. This is the shape that shipped broken.
_BAND = [
    ("mysql://dw", False),        # 2 + tail(2) = 4 — under threshold either way
    ("mysql://dw_pro", True),     # 6 + tail(2) = 8 — the near-miss that matters
    ("mysql://dw_prod", True),    # 7 + tail(2) = 9 — likewise
    ("mysql://reporting", True),  # 9 — already visible on the leaf alone
]


@pytest.mark.parametrize("leaf,expect_removed", _BAND)
def test_a_leaf_is_judged_in_the_context_the_gate_will_see(leaf, expect_removed):
    """The regression this closes: `{"source": "mysql://dw_prod", …}` contains no
    credential at all, yet the assembled string matches conn_string. Leaf-local
    detection removed nothing, the @final gate raised, and the benign request
    lost its exception report."""
    document = json.dumps({"source": leaf, "dimensions": ["region"], "metrics": ["visitors"]})
    scrubbed, removed = redact_json_document(document)

    assert bool(removed) is expect_removed, f"leaf-alone hit: {bool(detect_credentials(leaf))}"
    # The property the @final S-3 gate actually tests, on the string it sees.
    assert detect_credentials(scrubbed) == []
    assert json.loads(scrubbed)["dimensions"] == ["region"]


def test_the_near_miss_band_is_invisible_to_leaf_local_detection():
    """Guards the test above: if the detector ever flags these leaves on their
    own, the parametrization stops testing serialized-context judgement."""
    assert detect_credentials("mysql://dw_pro") == []
    assert detect_credentials("mysql://dw_prod") == []


def test_deep_nesting_appends_more_closers_than_a_comma():
    """The tail is one closer per enclosing container, not a fixed `",`."""
    document = json.dumps({"a": {"b": {"c": {"d": "mysql://dw_pro"}}}})
    scrubbed, removed = redact_json_document(document)

    assert removed == 1
    assert detect_credentials(scrubbed) == []
    assert json.loads(scrubbed)["a"]["b"]["c"]["d"].startswith(REDACTION_MARKER_PREFIX)


@pytest.mark.parametrize("shape", [
    {"source": "redis://cache1", "dimensions": ["region"]},
    {"a": {"b": ["mongodb://dw_pro", {"c": "mysql://dw_prod"}]}},
    {"postgresql://key_pos": "v", "metrics": ["visitors", "mysql://dw_prod"]},
    ["mysql://dw_pro", {"x": [["redis://cache1"]]}],
    # Necessity boundary. The tail is one closer per enclosing container, so a
    # leaf too short to match on its own becomes a match purely by being deeply
    # nested: `dw_pr` needs 3 appended characters and two closing braces plus the
    # quote supply exactly that. A fixed-width tail misses this.
    {"a": {"b": "mysql://dw_pr"}},
    # The same argument for list nesting, which appends `]` rather than `}`:
    # three levels give the 5-character tail `"]]]}` that a 3-character leaf needs.
    {"a": [[["mysql://dwp"]]]},
])
def test_every_position_survives_the_gate_and_stays_parseable(shape):
    """Value, list item, nested object and key positions all reach the gate as
    part of one serialized string, so all must be judged in context."""
    raw = json.dumps(shape)
    # Not vacuous: the document really does read as a credential before removal.
    assert detect_credentials(raw), f"case no longer exercises the gate: {raw}"

    scrubbed, _ = redact_json_document(raw)

    assert detect_credentials(scrubbed) == []
    assert json.loads(scrubbed) is not None


def test_leaf_removal_is_re_scanned_until_the_detector_is_quiet(monkeypatch):
    """The leaf path needs its own loop, not just the flat one.

    A stateful fake stands in for a pattern whose replacement re-creates a match:
    the first two scans report a hit, the third is clean. Removing once and
    trusting the result would hand the gate text it still flags.
    """
    def fake(text):
        # Content-based, so the verdict does not depend on how many leaves the
        # walk visits or in what order: two markers deep, the "pattern" is spent.
        # Counting the marker's tail rather than its prefix: each replacement
        # starts at offset 0 and eats the previous `[`, so only the tails survive.
        if text.count("Z]") >= 2:
            return []
        return [{"type": "Z", "start": 0, "end": 1}]

    monkeypatch.setattr(mod, "detect_credentials", fake)
    scrubbed, removed = redact_json_document(json.dumps(["anything"]))

    assert removed == 2, "second pass skipped — the leaf loop ran once"
    assert fake(json.loads(scrubbed)[0]) == []


def test_the_leaf_rescan_loop_is_bounded_and_fails_closed(monkeypatch):
    """Cap exhaustion on the leaf path discards the value, same as the flat path.

    Returning the partially-removed leaf would leave the document parseable but
    dirty — which is precisely the shape the @final gate rejects, and the shape
    the node's cleanliness mirror then has to turn into a lost request.
    """
    monkeypatch.setattr(mod, "detect_credentials", lambda text: [{"type": "x", "start": 0, "end": 1}])
    scrubbed, removed = redact_json_document(json.dumps(["anything"]))

    leaf = json.loads(scrubbed)[0]
    assert leaf == f"{REDACTION_MARKER_PREFIX}unresolved]"
    assert leaf.count(REDACTION_MARKER_PREFIX) == leaf.count("]") == 1
    assert removed == mod._MAX_PASSES + 1
