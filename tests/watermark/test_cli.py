"""`otz wm-clean` and `otz wm-qa` on generated photos (no network, no torch)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner
from PIL import Image

from src.cli import cli
from src.config import get_settings
from src.watermark.templates import load_templates
from tests.watermark.synth import apply_mark, make_background


@pytest.fixture
def samples(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(get_settings(), "watermark_lama_enabled", False)
    folder = tmp_path / "samples"
    (folder / "zida").mkdir(parents=True)
    tpl = {t.id: t for t in load_templates()}["zida_logo"]
    marked = apply_mark(make_background(1440, 1080, seed=1), tpl)
    Image.fromarray(marked).save(folder / "zida" / "marked.jpg", "JPEG", quality=92)
    Image.fromarray(make_background(800, 600, seed=2)).save(folder / "plain.jpg", "JPEG", quality=92)
    return folder


def test_wm_clean_writes_only_marked_files(samples, tmp_path):
    out = tmp_path / "out"
    result = CliRunner().invoke(
        cli,
        ["wm-clean", str(samples / "zida" / "marked.jpg"), str(samples / "plain.jpg"),
         "--out-dir", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert (out / "marked.clean.jpg").is_file()
    assert not (out / "plain.clean.jpg").exists()


def test_wm_clean_refuses_output_inside_inbound(samples, monkeypatch):
    monkeypatch.setattr(get_settings(), "inbound_photos_dir", samples)
    result = CliRunner().invoke(cli, ["wm-clean", str(samples / "zida" / "marked.jpg")])
    assert result.exit_code != 0
    assert "use --out-dir outside data/inbound" in result.output
    assert not list(samples.rglob("*.clean.jpg"))


def test_wm_qa_pairs_report_and_expectations(samples, tmp_path):
    good = tmp_path / "good.csv"
    good.write_text("file,marks\nzida/marked.jpg,zida_logo#1;zida_logo#0\nplain.jpg,\n")
    out = tmp_path / "qa"
    args = ["wm-qa", str(samples), "--out-dir", str(out)]
    result = CliRunner().invoke(cli, [*args, "--expect", str(good)])
    assert result.exit_code == 0, result.output
    assert "all matched" in result.output
    assert (out / "zida__marked.pair.jpg").is_file()
    assert (out / "zida__marked.marks.jpg").is_file()  # zoomed crops for the visual review
    assert not (out / "plain.marks.jpg").exists()
    assert "zida/marked.jpg,zida_logo#0" in (out / "report.csv").read_text()

    bad = tmp_path / "bad.csv"
    bad.write_text("file,marks\nzida/marked.jpg,\nplain.jpg,halo_corner#0\nmissing.jpg,\n")
    result = CliRunner().invoke(cli, [*args, "--expect", str(bad)])
    assert result.exit_code == 1
    assert result.output.count("MISMATCH") == 3
