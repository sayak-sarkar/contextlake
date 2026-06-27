"""The styled, right-aligned summary lines for the `status` command."""

from contextlake import core, style


def test_status_summary_labels_counts_and_alignment():
    lines = core._status_summary(128, 127, 126, 2, 1)
    assert len(lines) == 5
    text = style.strip_ansi("\n".join(lines))
    for label in ("GitLab projects (active)", "Local repositories",
                  "Synchronized", "Missing", "Extra"):
        assert label in text
    assert "✓" in text  # synchronized check glyph
    # counts are flush-right -> each plain line ends with its number
    plains = [style.strip_ansi(line).rstrip() for line in lines]
    assert plains[0].endswith("128")
    assert plains[1].endswith("127")
    assert plains[2].endswith("126")
    assert plains[3].endswith("2")
    assert plains[4].endswith("1")


def test_status_summary_glyphs_flag_problems():
    missing_row = style.strip_ansi(core._status_summary(1, 1, 0, 3, 0)[3])
    assert "⚠" in missing_row and missing_row.rstrip().endswith("3")
    clean_row = style.strip_ansi(core._status_summary(1, 1, 1, 0, 0)[3])
    assert "⚠" not in clean_row and "·" in clean_row
