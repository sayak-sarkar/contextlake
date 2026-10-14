"""The model catalogue, the fit rule, and the read-only `kb wiki --model-check` table."""

from __future__ import annotations

from argparse import Namespace

import pytest

from contextlake.kb.llm import catalogue, model_check
from contextlake.kb.llm.catalogue import DEFAULT, MB, MODELS, fits, largest_fitting
from contextlake.kb.llm.machine import Machine

GB = 1_000_000_000


def _machine(ram_gb=None, disk_gb=100.0):
    return Machine(cpus=8, ram_total=None,
                   ram_available=None if ram_gb is None else ram_gb * GB,
                   disk_free=disk_gb * GB)


def test_the_catalogue_is_one_ordered_family_with_the_default_first():
    from contextlake.kb.llm.builtin import DEFAULT_REPO

    assert DEFAULT is MODELS[0] and DEFAULT.repo_id == DEFAULT_REPO
    assert len({m.repo_id for m in MODELS}) == len(MODELS)
    for small, big in zip(MODELS, MODELS[1:], strict=False):
        assert small.download_mb < big.download_mb and small.ram_mb < big.ram_mb
    assert all(len(m.revision) == 40 for m in MODELS)


def test_a_model_outside_the_catalogue_has_no_pin():
    assert catalogue.pinned_revision(DEFAULT.repo_id) == DEFAULT.revision
    assert catalogue.pinned_revision("someone/their-own-model") is None


def test_fit_needs_ram_headroom():
    three_b = catalogue.by_repo(MODELS[2].repo_id)
    room = three_b.ram_mb * MB / catalogue.FIT_SHARE
    assert fits(three_b, Machine(8, None, room, 100 * GB), on_disk=True)
    assert not fits(three_b, Machine(8, None, room - 1, 100 * GB), on_disk=True)


def test_fit_needs_disk_unless_the_model_is_on_disk():
    seven_b = MODELS[3]
    tight = _machine(ram_gb=64, disk_gb=seven_b.download_mb / 1000)   # no spare GB
    assert not fits(seven_b, tight, on_disk=False)
    assert fits(seven_b, tight, on_disk=True)


def test_unknown_free_ram_fits_only_the_default():
    unknown = _machine(ram_gb=None)
    assert [m for m in MODELS if fits(m, unknown, on_disk=False)] == [DEFAULT]


@pytest.mark.parametrize("ram_gb,expected", [(64, "14B"), (12, "3B"), (3.5, "0.5B"),
                                             (1, None)])
def test_the_largest_model_that_fits(ram_gb, expected):
    got = largest_fitting(_machine(ram_gb=ram_gb), {})
    assert (got.label if got else None) == expected


def test_the_table_marks_disk_copies_fits_and_extrapolated_rows():
    disk = {m.repo_id: m is DEFAULT for m in MODELS}
    lines = model_check.render(_machine(ram_gb=12), disk, (3, 4), council_size=3)
    text = "\n".join(lines)
    assert "Wiki run: 3 repo page(s) and up to 4 module page(s), 4 model calls" in text
    assert catalogue.REFERENCE_MACHINE in text
    rows = [ln for ln in lines if ln.strip()[:1].isdigit()]
    assert len(rows) == len(MODELS)
    assert "on disk" in rows[0] and "on disk" not in rows[1]
    assert "(largest that fits)" in rows[2]
    assert rows[3].rstrip().endswith("*") and "* extrapolated" in text


def test_the_table_at_setup_has_no_run_column():
    """`init` has no store yet, so it cannot know the pages."""
    lines = model_check.render(_machine(ram_gb=12), {}, None, council_size=3)
    assert not any("Whole run" in ln or "Wiki run:" in ln for ln in lines)


def test_model_check_generates_nothing(tmp_path, monkeypatch, gls_logs):
    """No model is built, no page is written, and the store lock is not taken."""
    from contextlake.kb.cmds.wiki import cmd_wiki
    from contextlake.kb.state import check_schema
    from contextlake.kb.store.sqlite_store import SqliteStore

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    store_dir = tmp_path / "kb"
    store_dir.mkdir()
    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    store.close()
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir.as_posix()}"\n\n[llm]\nenabled = true\n'
                   f'provider = "builtin"\ncache_dir = "{(tmp_path / "models").as_posix()}"\n')

    def no_model(*a, **k):
        raise AssertionError("--model-check built a model")

    monkeypatch.setattr("contextlake.kb.llm.build_llm", no_model)
    assert cmd_wiki(Namespace(config=str(cfg), model_check=True)) == 0
    assert "Machine:" in gls_logs.text and "Wiki run: 0 repo page(s)" in gls_logs.text
    assert not (store_dir / "wiki").exists()
    assert not (store_dir / ".contextlake.lock").exists()


def test_the_loader_downloads_the_catalogue_commit(monkeypatch):
    """A catalogue model is fetched at its pinned commit; any other model unpinned."""
    import sys
    import types

    from contextlake.kb import model_cache
    from contextlake.kb.llm.builtin import BuiltinLlm

    asked = []
    monkeypatch.setattr(model_cache, "fetch_snapshot",
                        lambda repo, revision=None, **kw: asked.append((repo, revision)) or "/m")
    fake = types.ModuleType("openvino_genai")
    fake.LLMPipeline = lambda folder, device: object()
    monkeypatch.setitem(sys.modules, "openvino_genai", fake)
    BuiltinLlm(repo_id=MODELS[2].repo_id)._ensure_model()
    BuiltinLlm(repo_id="someone/their-own-model")._ensure_model()
    assert asked == [(MODELS[2].repo_id, MODELS[2].revision), ("someone/their-own-model", None)]
