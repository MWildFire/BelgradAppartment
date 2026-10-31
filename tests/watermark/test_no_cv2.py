"""The watermark code must run without OpenCV (and torch only when LaMa is really used).

Production has no OpenCV and the "code only" release route cannot add a dependency, so R04's
`cv2` calls were replaced by `src/watermark/imgops.py`. The dev venv still has OpenCV, which is
why the second test blocks it explicitly instead of uninstalling the shared package.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROOTS = ("src", "scripts")


def _imports_cv2(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name.split(".")[0] == "cv2"]
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "cv2":
            found.append(node.module or "cv2")
    return found


def test_no_source_file_imports_cv2():
    offenders = {}
    for root in ROOTS:
        for path in sorted((REPO / root).rglob("*.py")):
            names = _imports_cv2(path)
            if names:
                offenders[str(path.relative_to(REPO))] = names
    assert offenders == {}


def test_release_files_do_not_mention_opencv_packages():
    for name in ("pyproject.toml", "uv.lock"):
        text = (REPO / name).read_text(encoding="utf-8").lower()
        assert "opencv" not in text, name


PROBE = textwrap.dedent(
    """
    import importlib, pkgutil, sys, tempfile
    from pathlib import Path

    sys.path.insert(0, sys.argv[2])
    sys.modules["cv2"] = None  # any `import cv2` now raises ImportError

    import src.watermark
    for module in pkgutil.iter_modules(src.watermark.__path__):
        importlib.import_module(f"src.watermark.{module.name}")
    import src.telegram.bot_daemon, src.telegram.wm_bot  # noqa: E401
    import src.site.photos, src.site.og_image  # noqa: E401

    import numpy as np
    from PIL import Image

    import src.watermark.lama as lama
    from src.watermark import pipeline
    from src.watermark.settings import get_watermark_settings
    from src.watermark.templates import load_templates
    from tests.watermark.synth import apply_mark, jpeg_roundtrip, make_background

    class FakeLama:
        name = "lama"

        def inpaint(self, img, mask):
            out = img.copy()
            out[mask > 0] = img[mask == 0].mean(axis=0).astype(np.uint8)
            return out

    lama.get_lama_inpainter = lambda: FakeLama()
    get_watermark_settings().watermark_halo_corner_mode = sys.argv[1]
    sizes = {"cityexpert_center": (1920, 1080), "zida_logo": (1440, 1080),
             "halo_center": (1000, 750), "halo_corner": (1000, 750)}
    results = {}
    with tempfile.TemporaryDirectory() as tmp:
        for tpl in load_templates():
            size = sizes[tpl.id]
            img = jpeg_roundtrip(apply_mark(make_background(*size, seed=3), tpl))
            src = Path(tmp) / f"{tpl.id}.jpg"
            Image.fromarray(img).save(src, "JPEG", quality=92)
            dst = Path(tmp) / f"{tpl.id}.clean.jpg"
            report = pipeline.clean_file(src, dst)
            results[tpl.id] = [(m.method, m.kept_reason) for m in report.marks]
            assert dst.exists() == report.changed
    print("RESULT", sorted(results.items()))
    print("TORCH_LOADED", "torch" in sys.modules)
    """
)


def _run_probe(corner_mode: str) -> str:
    done = subprocess.run(
        [sys.executable, "-I", "-c", PROBE, corner_mode, str(REPO)],
        cwd=REPO, env={"PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=300, check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    return done.stdout


def test_pipeline_runs_for_every_template_with_opencv_blocked_and_no_torch():
    out = _run_probe("telea")
    assert "TORCH_LOADED False" in out
    assert "('cityexpert_center', [('reverse_blend', None)])" in out
    assert "inpaint:lama" in out and "inpaint:telea" in out  # centres via the fake LaMa, corner classic


def test_default_corner_mode_keeps_the_corner_without_opencv():
    out = _run_probe("skip")
    assert "('halo_corner', [('skip', 'corner_mode')])" in out
    assert "TORCH_LOADED False" in out
