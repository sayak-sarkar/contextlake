"""Tests for stable IDs and label sanitization."""

from contextlake.kb.ids import make_id, normalize_id
from contextlake.kb.security import MAX_LABEL_LEN, sanitize_label


def test_normalize_id_is_idempotent():
    for s in ["Foo.Bar", "a  b--c", "Team/Service-API", "café"]:
        once = normalize_id(s)
        assert normalize_id(once) == once


def test_normalize_id_canonicalizes_punctuation_and_case():
    assert normalize_id("Foo.Bar") == normalize_id("foo__bar") == "foo_bar"
    assert normalize_id("Team/Service-API") == "team_service_api"


def test_normalize_id_preserves_non_latin_letters():
    # Non-Latin letters must survive (not collapse to an empty/shared id).
    assert normalize_id("café") != ""
    assert normalize_id("日本語") != ""


def test_make_id_joins_and_normalizes():
    assert make_id("team/api", "Module", "func") == "team_api_module_func"
    assert make_id("a", "", "b") == "a_b"  # empty parts dropped
    assert make_id("_a_", ".b.") == "a_b"  # stray separators trimmed


def test_sanitize_label_strips_control_and_ansi():
    dirty = "name\x1b[31mRED\x1b[0m\x00\x07"  # ANSI + NUL + BEL
    clean = sanitize_label(dirty)
    assert "\x1b" not in clean and "\x00" not in clean and "\x07" not in clean
    assert "name" in clean and "RED" in clean


def test_sanitize_label_keeps_ordinary_whitespace():
    assert sanitize_label("a\tb\nc") == "a\tb\nc"


def test_sanitize_label_caps_length_and_handles_none():
    assert sanitize_label(None) == ""
    assert len(sanitize_label("x" * (MAX_LABEL_LEN + 50))) == MAX_LABEL_LEN
