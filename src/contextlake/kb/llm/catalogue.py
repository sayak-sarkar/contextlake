"""The built-in wiki models a user can pick from, and what each needs and costs.

One family, the family of the long-standing default: Qwen2.5-Coder Instruct, int4, as
pre-converted OpenVINO IR. Sizes and commits were read from the Hub API on 2026-10-14.

The RAM and time figures were measured on one machine (``REFERENCE_MACHINE``) with a
real `kb wiki` page: contextlake's own source, 47,838 nodes. RAM is the peak of the whole
run, store included, so it is what a large repository needs. 7B and 14B were not run
there (7B needs most of that machine's memory); their figures are extrapolated from the
three measured models and carry a 10% margin. A machine's own times differ, which is
why every time is shown as a range with this machine named.
"""

from __future__ import annotations

from dataclasses import dataclass

REFERENCE_MACHINE = "a WSL2 VM with 22 vCPUs and 16 GB RAM (2026-10-14)"
MB = 1_000_000
# A model fits when its peak RAM is at most this share of the RAM free now, and its
# download leaves this much disk free.
FIT_SHARE = 0.8
DISK_SPARE_MB = 1024


@dataclass(frozen=True)
class CatalogueModel:
    label: str
    repo_id: str
    revision: str        # the commit downloaded; see kb/model_cache.py
    download_mb: int
    ram_mb: int          # peak RAM of a whole `kb wiki` run with this model
    draft_s: float       # one page draft on REFERENCE_MACHINE
    review_s: float      # one council review on REFERENCE_MACHINE
    measured: bool       # False: extrapolated, not run

    def page_seconds(self, council_size: int) -> float:
        """One page: a draft and one review per council member."""
        return self.draft_s + council_size * self.review_s


def _repo(size: str) -> str:
    return f"OpenVINO/Qwen2.5-Coder-{size}-Instruct-int4-ov"


MODELS = (
    CatalogueModel("0.5B", _repo("0.5B"), "339876973306da551969e2d88991b7053be2029c",
                   349, 2500, 35, 8, True),
    CatalogueModel("1.5B", _repo("1.5B"), "3727da92d6202a996e555911c9725afd7ea54c4a",
                   941, 3900, 40, 25, True),
    CatalogueModel("3B", _repo("3B"), "3ed0cd90e9b8abb148df6d0c570be70a7a6fb840",
                   1835, 5900, 130, 49, True),
    CatalogueModel("7B", _repo("7B"), "25d797085dbbe41b1aa8d32bc9ab1e35b7ac10f4",
                   4509, 13000, 320, 120, False),
    CatalogueModel("14B", _repo("14B"), "7083d68370fe5f8ac99ae67327008c1ed11e87c9",
                   8453, 22800, 600, 225, False),
)
DEFAULT = MODELS[0]


def by_repo(repo_id: str | None) -> CatalogueModel | None:
    return next((m for m in MODELS if m.repo_id == repo_id), None)


def pinned_revision(repo_id: str) -> str | None:
    """The commit to download for ``repo_id``, or ``None`` for a model outside this list."""
    model = by_repo(repo_id)
    return model.revision if model else None


def fits(model: CatalogueModel, machine, *, on_disk: bool) -> bool:
    """Whether ``machine`` can run ``model`` now. An unknown free-RAM figure fits only the
    default model: guessing larger is how a run gets killed half way."""
    if machine.ram_available is None:
        return model is DEFAULT
    if model.ram_mb * MB > FIT_SHARE * machine.ram_available:
        return False
    if on_disk:
        return True
    return (machine.disk_free is not None
            and model.download_mb * MB <= machine.disk_free - DISK_SPARE_MB * MB)


def largest_fitting(machine, on_disk: dict[str, bool]) -> CatalogueModel | None:
    """The largest model that fits, or ``None`` when not even the smallest does."""
    for model in reversed(MODELS):
        if fits(model, machine, on_disk=on_disk.get(model.repo_id, False)):
            return model
    return None
