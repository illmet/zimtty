"""Shared fixtures.

Tests that need real ZIM files look for them in ~/Downloads/zim (or $ZIMTTY_TEST_ZIM_DIR)
and are skipped when absent:

  small  wikipedia_en_sociology_nopic_*.zim   ~240 MB, new-style HTML (<section> tags)
  big    wikipedia_en_all_nopic_2025-07.zim   ~46 GB, old-style flat HTML (mwoffliner 1.16)
  other/ any non-Wikipedia ZIMs (Wiktionary, Alpine wiki, ...)
"""
from __future__ import annotations

import os
from pathlib import Path

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
def small_zim() -> Zim:
    return _zim("wikipedia_en_sociology_nopic_*.zim")


@pytest.fixture(scope="session")
def big_zim() -> Zim:
    return _zim("wikipedia_en_all_nopic_*.zim")


@pytest.fixture(scope="session", params=["small", "big"])
def any_wikipedia(request) -> Zim:
    """Both HTML generations: every structural test runs on each."""
    return _zim("wikipedia_en_sociology_nopic_*.zim" if request.param == "small"
                else "wikipedia_en_all_nopic_*.zim")


@pytest.fixture
def pal() -> Palette:
    return Palette()


def other_zims() -> list[Path]:
    return sorted((ZIM_DIR / "other").glob("*.zim"))
