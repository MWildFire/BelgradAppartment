#!/usr/bin/env python3
"""Memory probe for watermark cleaning (WM05 B2): run in a FRESH process, one configuration.

Cleans a fixed set of sample photos one after another (as the bot does) and writes JSON:
peak RSS of the process tree (the bot plus its LaMa worker), peak of the container cgroup on
Linux, RSS right after the batch and after the LaMa worker went idle, seconds per photo, the
settings used and which removal methods fired.

    python scripts/wm_memory_probe.py PHOTOS_DIR --set album10 --corner lama \
        --max-pixels 105000 --threads 2 --idle-seconds 5 --out probe.json

Memory is sampled every 50 ms over the whole process tree (parent + children), so the number is
what the bot's systemd unit would be charged for. Linux reads /proc; macOS uses `ps` (RSS there
is unreliable - memory compression). Stdlib plus the project code only; photos are only read.
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
import threading
import time
from pathlib import Path

# Mixed album of ten from the QA samples: 4 Halo (corner mark), 3 4zida, 3 CityExpert.
ALBUM10 = (
    "halooglasi/halo_000", "halooglasi/halo_001", "halooglasi/halo_002", "halooglasi/halo_003",
    "4zida/zida_000", "4zida/zida_001", "4zida/zida_054",
    "cityexpert/cx_001", "cityexpert/cx_004", "cityexpert/cx_005",
)
LINUX = sys.platform.startswith("linux")


def _linux_children() -> dict[int, list[int]]:
    tree: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text()
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        tree.setdefault(ppid, []).append(int(entry))
    return tree


def _linux_rss(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def tree_rss_bytes() -> tuple[int, int]:
    """(RSS of this process, RSS of this process plus all its descendants), in bytes."""
    me = os.getpid()
    if LINUX:
        tree = _linux_children()
        stack, total = [me], 0
        while stack:
            pid = stack.pop()
            total += _linux_rss(pid)
            stack.extend(tree.get(pid, []))
        return _linux_rss(me), total
    out = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,rss="], capture_output=True, text=True, check=False
    ).stdout
    rows = [tuple(int(x) for x in line.split()) for line in out.splitlines() if line.strip()]
    kids: dict[int, list[int]] = {}
    rss = {pid: kb * 1024 for pid, _ppid, kb in rows}
    for pid, ppid, _kb in rows:
        kids.setdefault(ppid, []).append(pid)
    stack, total = [me], 0
    while stack:
        pid = stack.pop()
        total += rss.get(pid, 0)
        stack.extend(kids.get(pid, []))
    return rss.get(me, 0), total


def parent_peak_bytes() -> int:
    if LINUX:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) * 1024
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def cgroup_peak_bytes() -> int | None:
    """Peak memory of the whole container (cgroup v2), Linux only."""
    for name in ("/sys/fs/cgroup/memory.peak", "/sys/fs/cgroup/memory/memory.max_usage_in_bytes"):
        try:
            return int(Path(name).read_text().strip())
        except (OSError, ValueError):
            continue
    return None


class Sampler(threading.Thread):
    """Polls the tree RSS; keeps the maximum."""

    def __init__(self, period: float = 0.05) -> None:
        super().__init__(daemon=True)
        self.period = period
        self.peak_tree = 0
        self.peak_parent = 0
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.wait(self.period):
            parent, tree = tree_rss_bytes()
            self.peak_parent = max(self.peak_parent, parent)
            self.peak_tree = max(self.peak_tree, tree)

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=5)


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
    ap.add_argument("--max-side", type=int, default=None, help="LaMa max side (default: settings)")
    ap.add_argument("--max-pixels", type=int, default=None, help="LaMa crop pixel budget; 0 = off")
    ap.add_argument("--threads", type=int, default=None, help="torch threads (default: settings)")
    ap.add_argument("--isolation", choices=["process", "inline"], default=None)
    ap.add_argument("--idle-seconds", type=int, default=None,
                    help="LaMa idle limit; the probe then waits for the worker to exit")
    ap.add_argument("--repeat", type=int, default=1, help="run the set N times (leak check)")
    ap.add_argument("--out", type=Path, default=None, help="write the JSON here as well")
    args = ap.parse_args()

    # Settings go through the environment BEFORE the project is imported (cached settings).
    os.environ["WATERMARK_HALO_CORNER_MODE"] = args.corner
    for flag, env in (
        (args.max_side, "WATERMARK_LAMA_MAX_SIDE"), (args.max_pixels, "WATERMARK_LAMA_MAX_PIXELS"),
        (args.threads, "WATERMARK_TORCH_THREADS"), (args.isolation, "WATERMARK_LAMA_ISOLATION"),
        (args.idle_seconds, "WATERMARK_LAMA_IDLE_UNLOAD_SECONDS"),
    ):
        if flag is not None:
            os.environ[env] = str(flag)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    from src.watermark import lama
    from src.watermark.pipeline import clean_bytes
    from src.watermark.settings import get_watermark_settings

    settings = get_watermark_settings()
    files = pick_files(args.photos, args.which)
    rss_imports = tree_rss_bytes()[1]
    sampler = Sampler()
    sampler.start()

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
                "rss_tree_after_mib": mib(tree_rss_bytes()[1]),
            })
    peak_cgroup = cgroup_peak_bytes()
    gc.collect()
    time.sleep(0.2)
    rss_parent_idle, rss_tree_idle = tree_rss_bytes()

    after_idle = None
    if args.idle_seconds:  # let the LaMa worker (or the in-process model) go idle by itself
        time.sleep(args.idle_seconds + max(2.0, min(30.0, args.idle_seconds / 10)) + 1.5)
        gc.collect()
        after_idle = tree_rss_bytes()[1]
    sampler.stop()

    torch_imported = "torch" in sys.modules
    instance = lama._instance
    seconds_all = [float(p["seconds"]) for p in photos]  # type: ignore[arg-type]
    result = {
        "platform": f"{platform.system()} {platform.machine()}",
        "python": platform.python_version(),
        "set": args.which,
        "photos_count": len(files),
        "repeat": args.repeat,
        "corner_mode": args.corner,
        "isolation": settings.watermark_lama_isolation,
        "lama_available": instance is not None,
        "lama_max_side": getattr(instance, "max_side", None),
        "lama_max_pixels": getattr(instance, "max_pixels", None),
        "torch_threads_setting": settings.watermark_torch_threads,
        "torch_imported_in_the_bot_process": torch_imported,
        "rss_tree_after_imports_mib": mib(rss_imports),
        "peak_rss_tree_mib": mib(sampler.peak_tree),
        "peak_rss_parent_mib": mib(max(sampler.peak_parent, parent_peak_bytes())),
        "peak_cgroup_mib": None if peak_cgroup is None else mib(peak_cgroup),
        "rss_parent_after_batch_mib": mib(rss_parent_idle),
        "rss_tree_after_batch_mib": mib(rss_tree_idle),
        "rss_tree_after_idle_mib": None if after_idle is None else mib(after_idle),
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
    lama.reset_lama_inpainter()


if __name__ == "__main__":
    main()
