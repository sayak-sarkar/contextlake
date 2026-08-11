"""Synthetic ingest tier for the retrieval-quality fixture."""


class ReadingIngestor:
    """Accepts raw readings and forwards them to the aggregation tier."""

    def __init__(self, endpoint: str):
        self.endpoint = endpoint

    def submit_reading(self, sample_id: int, reading: float) -> bool:
        """Push one reading downstream, returning whether it was accepted."""
        return self.validate_reading(reading) and sample_id > 0

    def validate_reading(self, reading: float) -> bool:
        """Reject readings outside the plausible sensor range."""
        return -50.0 <= reading <= 150.0


def rollup_daily_totals(rows: list) -> dict:
    """Collapse per-reading rows into a per-day total."""
    return {"total": len(rows)}
