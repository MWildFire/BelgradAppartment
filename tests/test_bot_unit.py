"""deploy/otz-bot.service satisfies the release activation checks (scripts/release.py)."""

from __future__ import annotations

from scripts import release


def test_bot_unit_passes_the_release_effective_unit_check(tmp_path, monkeypatch):
    source = (release.ROOT / "deploy" / "otz-bot.service").read_text()
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / "otz-bot.service").write_text(source)
    configured = dict(
        line.split("=", 1)
        for line in source.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    assert configured["ExecStart"] == "/opt/otz/.venv/bin/python -m src.cli bot"
    effective = {
        "ExecStart": f"{{ argv[]={configured['ExecStart']} ; ignore_errors=no ; }}",
        "ExecStartPre": "",
        "User": "otz",
        "Type": "simple",
        "WorkingDirectory": "/opt/otz",
        "TimeoutStartUSec": "1min 30s",
        "DropInPaths": "",
    }
    monkeypatch.setattr(
        release, "run", lambda args, **_kw: effective[args[3].removeprefix("--property=")]
    )
    assert release.effective_units(deploy)["otz-bot.service"]["User"] == "otz"
    assert "otz-bot" in release.SERVICES
