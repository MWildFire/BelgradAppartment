"""Queue-aware rollback declarations must bind to the exact prior runtime."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import release
from scripts.rehearse_validation_release import rehearse

HEAD = "0016_identity_edges"
CRITICAL = [
    "src/matching/matcher.py",
    "src/matching/llm_validator.py",
    "src/matching/validation_jobs.py",
    "src/matching/identity.py",
    "src/telegram/digest_jobs.py",
]


def prior_runtime(tmp_path: Path):
    root = tmp_path / "app"
    staged = tmp_path / "release"
    contract = {
        "version": 1,
        "compatible_revisions": [HEAD],
        "semantics": ["fail_closed_validation", "durable_digest", "identity_edges"],
    }
    files = {}
    for name in [*CRITICAL, "scripts/runtime_contract.json"]:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(contract) if name.endswith("json") else "synthetic code")
        files[name] = release.hash_bytes(path.read_bytes())
    release.write_json(root / "release.json", {"files": files})
    return root, staged, {"swapped": []}


@pytest.mark.parametrize(
    "fault", ["marker", "contract", "runtime", "manifest_entry", "schema", "semantics", "version"]
)
def test_incompatible_prior_runtime_is_rejected(tmp_path, fault):
    root, staged, state = prior_runtime(tmp_path)
    contract_path = root / "scripts/runtime_contract.json"
    if fault == "marker":
        (root / "release.json").unlink()
    elif fault == "contract":
        contract_path.write_text("{}")  # hash mismatch, before parsing the declaration
    elif fault == "runtime":
        (root / CRITICAL[0]).write_text("old fail-open code")
    elif fault == "manifest_entry":
        marker = release.read_json(root / "release.json")
        del marker["files"][CRITICAL[0]]
        release.write_json(root / "release.json", marker)
    else:
        contract = release.read_json(contract_path)
        contract[
            {"schema": "compatible_revisions", "semantics": "semantics", "version": "version"}[
                fault
            ]
        ] = []
        release.write_json(contract_path, contract)
        marker = release.read_json(root / "release.json")
        marker["files"]["scripts/runtime_contract.json"] = release.hash_bytes(
            contract_path.read_bytes()
        )
        release.write_json(root / "release.json", marker)
    with pytest.raises(release.ReleaseError):
        release.verify_rollback_compatibility(root, staged, state, [HEAD])


def test_prior_runtime_resolves_already_swapped_paths(tmp_path):
    root, staged, state = prior_runtime(tmp_path)
    (staged / "rollback").mkdir(parents=True)
    (root / "src").rename(staged / "rollback/src")
    (root / "src").mkdir()
    (root / "src/matcher.py").write_text("new runtime is not the fallback")
    state["swapped"] = ["src"]
    release.verify_rollback_compatibility(root, staged, state, [HEAD])
    with pytest.raises(release.ReleaseError, match="incompatible"):
        release.verify_rollback_compatibility(root, staged, state, ["0017_unknown_future"])


def test_synthetic_rehearsal_preserves_pending_and_unknown_rows(tmp_path):
    output = tmp_path / "rehearsal"
    receipt = rehearse(output)
    assert receipt["result"] == "synthetic_rehearsal_passed"
    assert receipt["preserved_rows"]["validation_jobs"] == 1
    assert receipt["preserved_rows"]["digest_work_items"] == 1
    assert receipt["preserved_rows"]["match_deliveries"] == 2
    assert receipt["actual_system_commands"] == 0
    with pytest.raises(FileExistsError):
        rehearse(output)
    with pytest.raises(release.ReleaseError):
        release.validate_backup(receipt)  # Cannot masquerade as a real recovery receipt.
