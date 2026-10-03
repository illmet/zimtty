"""Shared fixtures.

Tests that need real ZIM files look for them in ~/Downloads/zim (or $ZIMTTY_TEST_ZIM_DIR)
and are skipped when absent:

  wikipedia_en_all_nopic_*.zim   full English Wikipedia
  archlinux_en_all_maxi_*.zim    English ArchWiki

Synthetic parser tests cover both flat and section-based MediaWiki HTML without
requiring separate sample archives.
"""
from __future__ import annotations

import os
from pathlib import Path
from random import Random

import pytest

from zimtty.zimdoc import Palette, Zim

ZIM_DIR = Path(os.environ.get("ZIMTTY_TEST_ZIM_DIR", Path.home() / "Downloads/zim"))


def _find(pattern: str) -> Path | None:
    hits = sorted(ZIM_DIR.glob(pattern))
    return hits[-1] if hits else None


def _zim(pattern: str) -> Zim:
    p = _find(pattern)
    if p is None:
        pytest.skip(f"no {pattern} in {ZIM_DIR}")
    try:
        return Zim(str(p))
    except Exception as e:  # e.g. still downloading
        pytest.skip(f"{p.name} not readable: {e}")


@pytest.fixture(scope="session")
def big_zim() -> Zim:
    return _zim("wikipedia_en_all_nopic_*.zim")


@pytest.fixture(scope="session")
def archwiki_zim() -> Zim:
    return _zim("archlinux_en_all_maxi_*.zim")


@pytest.fixture
def pal() -> Palette:
    return Palette()


def sampled_article_paths(zim: Zim, count: int, seed: int) -> list[str]:
    """Repeatable HTML samples for a particular archive, including redirects.

    libzim's native random-entry generator is not controlled by random.seed().
    Its Python binding exposes indexed path-order access only through this
    private method; keep that dependency isolated here.
    """
    rng = Random(seed)
    paths: dict[str, None] = {}
    for _ in range(count * 20):
        entry = zim.z._get_entry_by_id(rng.randrange(zim.z.entry_count))
        mime = entry.get_item().mimetype.split(";", 1)[0].strip().lower()
        if mime not in {"text/html", "application/xhtml+xml"}:
            continue
        resolved = zim.resolve(entry.path)
        assert resolved is not None, entry.path
        paths[resolved[0]] = None
        if len(paths) == count:
            return list(paths)
    raise AssertionError(f"could not sample {count} distinct HTML entries")
