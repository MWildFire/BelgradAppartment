"""The interactive bot stays lightweight until actual watermark work is requested."""

from __future__ import annotations

import subprocess
import sys

import pytest

from src.config import PROJECT_ROOT


@pytest.mark.parametrize(
    "setup",
    [
        "import src.telegram.wm_bot",
        "from src.config import get_settings; "
        "s = get_settings(); s.watermark_bot_enabled = False; "
        "s.subscription_bot_enabled = True; "
        "from src.telegram.bot_daemon import build_dispatcher; build_dispatcher()",
    ],
)
def test_bot_imports_do_not_load_opencv(setup):
    probe = f"import sys; {setup}; print('cv2' in sys.modules)"
    done = subprocess.run(
        [sys.executable, "-c", probe], cwd=PROJECT_ROOT,
        capture_output=True, text=True, check=True, timeout=120,
    )
    assert done.stdout.strip() == "False"
