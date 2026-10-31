#!/usr/bin/env python3
"""Memory probe for watermark cleaning (WM05 B2): run in a FRESH process, one configuration.

Cleans a fixed set of sample photos one after another (as the bot does) and writes JSON:
peak RSS of the whole process, RSS right after imports / after the batch / after unloading the
model, seconds per photo, the settings used and which removal methods fired.

    python scripts/wm_memory_probe.py PHOTOS_DIR --set album10 --corner lama \
        --max-side 768 --threads 2 --out probe.json

Peak RSS: Linux - VmHWM from /proc/self/status; macOS - ru_maxrss (bytes). Stdlib plus the
project code only. The photos are only read.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path

# Mixed album of ten from the QA samples: 4 Halo (corner mark), 3 4zida, 3 CityExpert.
ALBUM10 = (
    "halooglasi/halo_000", "halooglasi/halo_001", "halooglasi/halo_002", "halooglasi/halo_003",
    "4zida/zida_000", "4zida/zida_001", "4zida/zida_054",
    "cityexpert/cx_001", "cityexpert/cx_004", "cityexpert/cx_005",
)


def current_rss_bytes() -> int:
    """Resident set size of this process right now."""
    if sys.platform.startswith("linux"):
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    out = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True, check=False
    ).stdout.strip()
    return int(out) * 1024 if out else 0


def peak_rss_bytes() -> int:
    """Highest RSS since the process started."""
    if sys.platform.startswith("linux"):
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) * 1024
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def malloc_trim() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except OSError:
        return False
    return True


def pick_files(photos: Path, which: str) -> list[Path]:
    if which == "album10":
        files = []
        for stem in ALBUM10:
            matches = sorted((photos / Path(stem).parent).glob(f"{Path(stem).name}_*.jpg"))
            if not matches:
                raise SystemExit(f"sample not found: {stem}")
            files.append(matches[0])
        return files
    return sorted(p for p in photos.glob("*/*.jpg"))


def mib(value: int) -> float:
    return round(value / 1048576, 1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("photos", type=Path, help="wm-samples directory (halooglasi/ 4zida/ ...)")
    ap.add_argument("--set", dest="which", choices=["album10", "all36"], default="album10")
    ap.add_argument("--corner", choices=["skip", "lama", "telea"], default="skip")
    ap.add_argument("--max-side", type=int, default=None, help="LaMa max side (default: code's)")
    ap.add_argument("--threads", type=int, default=None, help="torch threads (default: code's)")
    ap.add_argument("--repeat", type=int, default=1, help="run the set N times (leak check)")
    ap.add_argument("--out", type=Path, default=None, help="write the JSON here as well")
    args = ap.parse_args()

    # Settings go through the environment BEFORE the project is imported (cached settings).
    os.environ["WATERMARK_HALO_CORNER_MODE"] = args.corner
    if args.threads is not None:
        os.environ["WATERMARK_TORCH_THREADS"] = str(args.threads)
        os.environ["OMP_NUM_THREADS"] = str(args.threads)
        os.environ["MKL_NUM_THREADS"] = str(args.threads)
    if args.max_side is not None:
        os.environ["WATERMARK_LAMA_MAX_SIDE"] = str(args.max_side)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    from src.watermark.lama import get_lama_inpainter
    from src.watermark.pipeline import clean_bytes

    files = pick_files(args.photos, args.which)
    rss_imports = current_rss_bytes()
    lama = get_lama_inpainter()
    if lama is not None and args.max_side is not None:
        lama.max_side = args.max_side  # older code has no setting for it
    if args.threads is not None:
        try:
            import torch

            torch.set_num_threads(args.threads)
        except ImportError:
            pass

    photos: list[dict[str, object]] = []
    for round_no in range(args.repeat):
        for path in files:
            data = path.read_bytes()
            started = time.perf_counter()
            _, report = clean_bytes(data)
            seconds = time.perf_counter() - started
            photos.append({
                "round": round_no,
                "file": f"{path.parent.name}/{path.name}",
                "seconds": round(seconds, 3),
                "methods": [f"{m.template_id}:{m.method}" for m in report.marks],
                "kept_reasons": [getattr(m, "kept_reason", None) for m in report.marks],
                "rss_after_mib": mib(current_rss_bytes()),
            })
    peak = peak_rss_bytes()
    gc.collect()
    rss_idle = current_rss_bytes()

    unload = None
    rss_unloaded = None
    trimmed = False
    try:
        from src.watermark import lama as lama_module

        unload = getattr(lama_module, "unload_lama", None)
    except ImportError:
        pass
    if unload is not None:
        unload()
        gc.collect()
        trimmed = malloc_trim()
        rss_unloaded = current_rss_bytes()

    torch_threads = None
    torch_imported = "torch" in sys.modules
    if torch_imported:
        torch_threads = sys.modules["torch"].get_num_threads()
    seconds_all = [float(p["seconds"]) for p in photos]  # type: ignore[arg-type]
    result = {
        "platform": f"{platform.system()} {platform.machine()}",
        "python": platform.python_version(),
        "set": args.which,
        "photos_count": len(files),
        "repeat": args.repeat,
        "corner_mode": args.corner,
        "lama_available": lama is not None,
        "lama_max_side": getattr(lama, "max_side", None),
        "torch_imported": torch_imported,
        "torch_threads": torch_threads,
        "rss_after_imports_mib": mib(rss_imports),
        "peak_rss_mib": mib(peak),
        "rss_idle_after_batch_mib": mib(rss_idle),
        "rss_after_unload_mib": None if rss_unloaded is None else mib(rss_unloaded),
        "malloc_trim_called": trimmed,
        "seconds_per_photo_mean": round(statistics.fmean(seconds_all), 3),
        "seconds_per_photo_median": round(statistics.median(seconds_all), 3),
        "seconds_total": round(sum(seconds_all), 2),
        "methods_fired": sorted({m for p in photos for m in p["methods"]}),  # type: ignore[union-attr]
        "photos": photos,
    }
    text = json.dumps(result, ensure_ascii=False, indent=1)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
