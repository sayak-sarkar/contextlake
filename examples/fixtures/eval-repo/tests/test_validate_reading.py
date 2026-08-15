"""Noise the retrieval gate could not previously see.

Nothing in this fixture used to repeat a symbol's name across `name`,
`qualified_name` AND `file` at once, which is exactly the shape that buried a real
definition 32nd of 153 on a live index: FTS5's default ranking weights all three
columns equally, so rows that repeat the term everywhere outrank the definition that
merely defines it.

These functions are deliberately named so that they contain the token
`validate_reading` without BEING it. A nested function actually named
`validate_reading` would satisfy a name-matched golden query and hide the very
regression this file exists to expose.
"""


def test_validate_reading_rejects_high_values():
    return True

def test_validate_reading_rejects_low_values():
    return True

def test_validate_reading_accepts_zero():
    return True

def test_validate_reading_accepts_boundaries():
    return True

def test_validate_reading_is_pure():
    return True

def test_validate_reading_handles_nan():
    return True

def test_validate_reading_rejects_none():
    return True

def test_validate_reading_roundtrip():
    return True
