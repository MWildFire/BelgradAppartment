"""Real CLI spellings must be recognized before runtime swaps or rollback."""
import re

import pytest

from scripts.release import MAINTENANCE_PATTERN


@pytest.mark.parametrize("command", [
    "refetch-details --public-only --limit=500",
    "check-links --limit=250",
    "heal-urls --limit=200",
    "site-liveness --dry-run",
    "nekretnine-shadow-audit --compare-details=100",
    "nightly-refresh --incremental --no-notify",
])
def test_operator_jobs_prevent_release_swaps(command):
    for executable in ("/opt/otz/.venv/bin/python -m src.cli ", "/opt/otz/.venv/bin/otz "):
        assert re.search(MAINTENANCE_PATTERN, executable + command)


def test_unrelated_read_only_process_does_not_prevent_release():
    assert not re.search(MAINTENANCE_PATTERN, "python -m src.cli llm-report")
