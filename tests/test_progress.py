"""Tests for style.Progress: the stdlib CLI progress reporter."""

import io

from contextlake import style


class _Tty(io.StringIO):
    def isatty(self):
        return True


class _NotTty(io.StringIO):
    def isatty(self):
        return False


def _clock(values):
    """A scripted monotonic clock: each call returns the next value."""
    it = iter(values)
    return lambda: next(it)


def _quiet_env(monkeypatch):
    # Deterministic terminal width / colour regardless of the host shell.
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)


def test_exact_math(monkeypatch):
    _quiet_env(monkeypatch)
    stream = _Tty()
    clock = _clock([0, 10, 20, 30])
    p = style.Progress(10, now=clock, stream=stream)
    p.advance()
    p.advance()
    p.advance()
    frame = style.strip_ansi(stream.getvalue().split("\r")[-1])
    assert "3/10" in frame
    assert "(30%)" in frame
    assert "00:30" in frame
    assert "6.0/min" in frame
    assert "~01:10" in frame


def test_first_advance_always_renders_then_throttles(monkeypatch):
    _quiet_env(monkeypatch)
    stream = _Tty()
    clock = _clock([0, 0.1, 0.2])
    p = style.Progress(5, now=clock, stream=stream, min_interval=0.5)
    p.advance()  # first advance always renders
    p.advance()  # only 0.1s later -> throttled, no new frame
    assert stream.getvalue().count("\r") == 1


def test_throttle_lets_a_later_frame_through(monkeypatch):
    _quiet_env(monkeypatch)
    stream = _Tty()
    clock = _clock([0, 0.1, 0.2, 0.7])
    p = style.Progress(5, now=clock, stream=stream, min_interval=0.5)
    p.advance()  # renders (first)
    p.advance()  # throttled, only 0.1s after the last render
    assert stream.getvalue().count("\r") == 1
    p.advance()  # 0.6s after the last render -> renders
    assert stream.getvalue().count("\r") == 2


def test_non_tty_summary_every(monkeypatch):
    _quiet_env(monkeypatch)
    stream = _NotTty()
    clock = _clock([0, 1, 2, 3, 4, 5])
    p = style.Progress(10, now=clock, stream=stream, summary_every=5, summary_seconds=30.0)
    for _ in range(5):
        p.advance()
    out = stream.getvalue()
    assert "\r" not in out
    assert out.count("\n") == 1
    assert "5/10" in out


def test_non_tty_summary_seconds_triggers_early(monkeypatch):
    _quiet_env(monkeypatch)
    stream = _NotTty()
    # summary_every=100 (won't fire on count), but a big time jump should.
    clock = _clock([0, 40])
    p = style.Progress(10, now=clock, stream=stream, summary_every=100, summary_seconds=30.0)
    p.advance()
    out = stream.getvalue()
    assert out.count("\n") == 1
    assert "\r" not in out


def test_unknown_total(monkeypatch):
    _quiet_env(monkeypatch)
    stream = _Tty()
    clock = _clock([0, 5])
    p = style.Progress(None, now=clock, stream=stream)
    p.advance()
    frame = style.strip_ansi(stream.getvalue().split("\r")[-1])
    assert "%" not in frame
    assert "left" not in frame
    assert "1 done" in frame
    assert "00:05" in frame
    assert "/min" in frame


def test_label_prefixes_the_line(monkeypatch):
    _quiet_env(monkeypatch)
    stream = _Tty()
    clock = _clock([0, 1])
    p = style.Progress(10, label="indexing", now=clock, stream=stream)
    p.advance()
    frame = style.strip_ansi(stream.getvalue().split("\r")[-1])
    assert frame.startswith("indexing ")


def test_done_tty_clears_and_writes_summary(monkeypatch):
    _quiet_env(monkeypatch)
    stream = _Tty()
    clock = _clock([0, 10, 10])
    p = style.Progress(5, now=clock, stream=stream)
    p.advance()
    p.done("wrote 10")
    out = stream.getvalue()
    width = style.terminal_width(stream)
    assert out.endswith("\r" + (" " * width) + "\r" + "wrote 10\n")


def test_done_non_tty_final_summary(monkeypatch):
    _quiet_env(monkeypatch)
    stream = _NotTty()
    clock = _clock([0, 3])
    p = style.Progress(3, now=clock, stream=stream)
    p.done()
    out = stream.getvalue()
    assert "\r" not in out
    assert out.endswith("\n")
    assert "0/3" in out


def test_stream_isolation(monkeypatch):
    _quiet_env(monkeypatch)
    fake_stdout = io.StringIO()
    fake_stderr = io.StringIO()
    monkeypatch.setattr("sys.stdout", fake_stdout)
    monkeypatch.setattr("sys.stderr", fake_stderr)

    target = _Tty()
    clock = _clock([0, 1, 1])
    p = style.Progress(5, now=clock, stream=target)
    p.advance()
    p.done("finished")

    assert fake_stdout.getvalue() == ""
    assert fake_stderr.getvalue() == ""
    assert target.getvalue() != ""


def test_zero_and_degenerate_do_not_raise(monkeypatch):
    _quiet_env(monkeypatch)

    # total=0, never advanced.
    stream = _Tty()
    style.Progress(0, now=_clock([0, 0]), stream=stream).done()

    # total=0, advance still called -> no ZeroDivisionError.
    stream2 = _Tty()
    clock2 = _clock([0, 1, 1])
    p2 = style.Progress(0, now=clock2, stream=stream2)
    p2.advance()
    p2.done()

    # known total, never advanced, non-tty done().
    stream3 = _NotTty()
    style.Progress(5, now=_clock([0, 0]), stream=stream3).done()

    # unknown total, never advanced.
    stream4 = _NotTty()
    style.Progress(None, now=_clock([0, 0]), stream=stream4).done()


def test_line_clamps_to_terminal_width(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("COLUMNS", "10")
    stream = _Tty()
    clock = _clock([0, 1])
    p = style.Progress(10, now=clock, stream=stream)
    p.advance()
    frame = stream.getvalue().split("\r")[-1]
    assert style.visible_width(frame) <= 10
