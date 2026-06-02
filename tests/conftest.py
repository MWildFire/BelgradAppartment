"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def _read(rel: str) -> str:
    return (FIXTURES / rel).read_text(encoding="utf-8")


@pytest.fixture
def nekretnine_list_html() -> str:
    return _read("nekretnine/list.html")


@pytest.fixture
def nekretnine_detail_html() -> str:
    return _read("nekretnine/detail.html")


@pytest.fixture
def cetiri_zida_list_html() -> str:
    return _read("cetiri_zida/list.html")


@pytest.fixture
def cetiri_zida_detail_html() -> str:
    return _read("cetiri_zida/detail.html")
