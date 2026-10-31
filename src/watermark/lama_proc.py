"""LaMa in a separate worker process: the bot never imports torch.

Why: measured on Linux (WM05), `import torch` + the model keep ~0.5 GiB resident and the model's
working memory is never given back to the OS inside a process (after unloading the model and
`malloc_trim(0)` 0.8-1.25 GiB stay). A worker that exits after an idle period returns all of it,
and an out-of-memory kill of the worker costs one photo, not the bot.

The parent (`LamaWorkerClient`) starts the worker lazily, talks to it over pipes, allows one
request at a time, kills it on a timeout, and stops it after `idle_seconds` without requests.
The worker (`python -m src.watermark.lama_proc`) runs the ordinary `LamaInpainter` in-process.

Protocol (binary, over the worker's stdin/stdout):
  request  = JSON line {"op", "image": [h, w, 3], "mask": [h, w]} + image bytes + mask bytes (uint8)
  response = JSON line {"ok": true, "shape": [h, w, 3]} + bytes, or {"ok": false, "error": "..."}
The worker's own stdout is redirected to stderr so a stray print cannot corrupt the stream.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import logging
import os
import select
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import IO

import numpy as np

from src.watermark.lama_model import LamaError

logger = logging.getLogger(__name__)

Handler = Callable[[str, np.ndarray, np.ndarray], np.ndarray]
DEFAULT_TIMEOUT_SECONDS = 180.0
DEFAULT_IDLE_SECONDS = 600.0
_CHUNK = 1 << 20


class LamaWorkerError(LamaError):
    """The worker failed (died, timed out, or reported an error); the mark is kept."""


# ---- framing (shared by the parent, the worker and the tests) -----------------------------------


def write_request(stream: IO[bytes], op: str, image: np.ndarray, mask: np.ndarray) -> None:
    header = {"op": op, "image": list(image.shape), "mask": list(mask.shape)}
    stream.write(json.dumps(header).encode("ascii") + b"\n")
    stream.write(np.ascontiguousarray(image, dtype=np.uint8).tobytes())
    stream.write(np.ascontiguousarray(mask, dtype=np.uint8).tobytes())
    stream.flush()


def _read_exact(stream: IO[bytes], count: int) -> bytes:
    chunks: list[bytes] = []
    while count > 0:
        chunk = stream.read(count)
        if not chunk:
            raise EOFError("stream ended in the middle of a frame")
        chunks.append(chunk)
        count -= len(chunk)
    return b"".join(chunks)


def read_request(stream: IO[bytes]) -> tuple[str, np.ndarray, np.ndarray] | None:
    """None at a clean end of the stream (the parent closed it)."""
    line = stream.readline()
    if not line:
        return None
    meta = json.loads(line)
    image_shape, mask_shape = tuple(meta["image"]), tuple(meta["mask"])
    image = np.frombuffer(_read_exact(stream, int(np.prod(image_shape))), np.uint8)
    mask = np.frombuffer(_read_exact(stream, int(np.prod(mask_shape))), np.uint8)
    return str(meta["op"]), image.reshape(image_shape).copy(), mask.reshape(mask_shape).copy()


def _write_response(stream: IO[bytes], result: np.ndarray) -> None:
    result = np.ascontiguousarray(result, dtype=np.uint8)
    stream.write(json.dumps({"ok": True, "shape": list(result.shape)}).encode("ascii") + b"\n")
    stream.write(result.tobytes())
    stream.flush()


def _write_error(stream: IO[bytes], error: BaseException) -> None:
    text = f"{type(error).__name__}: {error}"[:500]
    stream.write(json.dumps({"ok": False, "error": text}).encode("utf-8") + b"\n")
    stream.flush()


def serve(
    handler: Handler, stdin: IO[bytes] | None = None, stdout: IO[bytes] | None = None
) -> None:
    """Worker loop: answer requests until the parent closes the pipe."""
    source = stdin if stdin is not None else sys.stdin.buffer
    if stdout is None:
        out_fd = os.dup(1)
        os.dup2(2, 1)  # whatever a library prints must not reach the protocol stream
        sink: IO[bytes] = os.fdopen(out_fd, "wb")
    else:
        sink = stdout
    while True:
        request = read_request(source)
        if request is None:
            return
        op, image, mask = request
        try:
            result = handler(op, image, mask)
        except Exception as error:  # reported to the parent; the worker stays usable
            logger.exception("[lama-worker] %s failed", op)
            _write_error(sink, error)
            continue
        _write_response(sink, result)


# ---- the parent side ----------------------------------------------------------------------------


class LamaWorkerClient:
    """One lazily started worker; one request at a time (a queue, in call order)."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: Path | str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        idle_seconds: float = DEFAULT_IDLE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        auto_reap: bool = True,
    ) -> None:
        self.command = list(command)
        self.cwd = cwd
        self.env = env or {}
        self.timeout = timeout
        self.idle_seconds = idle_seconds
        self._clock = clock
        self._auto_reap = auto_reap
        self._lock = threading.Lock()  # held for the whole request, and while stopping
        self._proc: subprocess.Popen[bytes] | None = None
        self._last_used = clock()
        self._closed = threading.Event()
        self._reaper: threading.Thread | None = None
        self.spawn_count = 0
        atexit.register(self.close)

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # -- process management (callers hold self._lock) --

    def _spawn(self) -> subprocess.Popen[bytes]:
        env = {**os.environ, **self.env}
        self._proc = subprocess.Popen(  # the command is built by our own code
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd=self.cwd, env=env,
            bufsize=0, close_fds=True,
        )
        assert self._proc.stdin is not None
        os.set_blocking(self._proc.stdin.fileno(), False)  # writes obey the request deadline
        self.spawn_count += 1
        logger.info("[lama-worker] started pid=%s", self._proc.pid)
        if self._auto_reap and self.idle_seconds > 0 and self._reaper is None:
            self._reaper = threading.Thread(target=self._reap_loop, name="lama-reaper", daemon=True)
            self._reaper.start()
        return self._proc

    def _stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - a kill -9 does not take long
            logger.warning("[lama-worker] pid=%s did not exit after kill", proc.pid)
        for pipe in (proc.stdin, proc.stdout):
            if pipe is not None:
                with contextlib.suppress(OSError):
                    pipe.close()

    # -- pipe I/O with a deadline --

    @staticmethod
    def _write_all(fd: int, data: bytes, deadline: float) -> None:
        view = memoryview(data)
        while view:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError
            _, writable, _ = select.select([], [fd], [], left)
            if not writable:
                raise TimeoutError
            try:  # the pipe is non-blocking: a worker that stopped reading cannot hold us here
                view = view[os.write(fd, view[:_CHUNK]):]
            except BlockingIOError:
                continue

    @staticmethod
    def _read_exact(fd: int, count: int, deadline: float) -> bytes:
        chunks: list[bytes] = []
        while count > 0:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError
            readable, _, _ = select.select([fd], [], [], left)
            if not readable:
                raise TimeoutError
            chunk = os.read(fd, min(count, _CHUNK))
            if not chunk:
                raise EOFError
            chunks.append(chunk)
            count -= len(chunk)
        return b"".join(chunks)

    def _read_line(self, fd: int, deadline: float) -> bytes:
        line = bytearray()
        while True:
            byte = self._read_exact(fd, 1, deadline)
            if byte == b"\n":
                return bytes(line)
            line += byte

    def _died(self, proc: subprocess.Popen[bytes]) -> LamaWorkerError:
        try:
            code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            code = None
        self._stop()
        return LamaWorkerError(f"LaMa worker died (exit code {code})")

    # -- public API --

    def call(self, op: str, image: np.ndarray, mask: np.ndarray) -> np.ndarray:
        """Send one request and wait for the answer. Raises LamaWorkerError on any failure."""
        header = json.dumps({"op": op, "image": list(image.shape), "mask": list(mask.shape)})
        payload = (
            header.encode("ascii") + b"\n"
            + np.ascontiguousarray(image, dtype=np.uint8).tobytes()
            + np.ascontiguousarray(mask, dtype=np.uint8).tobytes()
        )
        with self._lock:
            if self._closed.is_set():
                raise LamaWorkerError("LaMa worker client is closed")
            proc = self._proc if self.alive else None
            if proc is None:
                self._stop()
                proc = self._spawn()
            deadline = time.monotonic() + self.timeout
            assert proc.stdin is not None and proc.stdout is not None
            try:
                self._write_all(proc.stdin.fileno(), payload, deadline)
                fd = proc.stdout.fileno()
                reply = json.loads(self._read_line(fd, deadline))
                if not reply.get("ok"):
                    raise LamaWorkerError(str(reply.get("error", "unknown worker error")))
                shape = tuple(reply["shape"])
                result = self._read_exact(fd, int(np.prod(shape)), deadline)
            except TimeoutError:
                self._stop()
                raise LamaWorkerError(f"LaMa worker timeout after {self.timeout:g} s") from None
            except (EOFError, OSError, KeyError, TypeError, ValueError):
                # dead worker, broken pipe or a reply we cannot parse: stop it, so the next
                # request starts from a clean protocol state
                raise self._died(proc) from None
            finally:
                self._last_used = self._clock()
        return np.frombuffer(result, np.uint8).reshape(shape).copy()

    def reap_if_idle(self) -> bool:
        """Stop the worker if it has been idle for `idle_seconds`. Never during a request.

        `idle_seconds` <= 0 means "never stop it for being idle".
        """
        if self.idle_seconds <= 0 or not self._lock.acquire(blocking=False):
            return False
        try:
            if not self.alive or self._clock() - self._last_used < self.idle_seconds:
                return False
            logger.info("[lama-worker] idle for %.0f s: stopping", self._clock() - self._last_used)
            self._stop()
            return True
        finally:
            self._lock.release()

    def _reap_loop(self) -> None:
        tick = min(30.0, max(1.0, self.idle_seconds / 10))
        while not self._closed.wait(tick):
            self.reap_if_idle()

    @property
    def is_closed(self) -> bool:
        return self._closed.is_set()

    def close_worker(self) -> None:
        """Stop the worker process now; a later call starts a new one."""
        with self._lock:
            self._stop()

    def close(self) -> None:
        """Stop the worker and refuse further calls."""
        self._closed.set()
        with self._lock:
            self._stop()


# ---- the worker entry point ---------------------------------------------------------------------


class WorkerRunner:
    """The same `predict` as `ModelRunner`, executed by the worker process."""

    def __init__(self, client: LamaWorkerClient) -> None:
        self.client = client

    def predict(self, image: np.ndarray, mask: np.ndarray) -> np.ndarray:
        return self.client.call("predict", image, mask)

    @property
    def loaded(self) -> bool:
        return self.client.alive

    def unload(self) -> None:
        self.client.close_worker()

    close = unload


_workers: dict[tuple[str, int, float, float], LamaWorkerClient] = {}
_workers_lock = threading.Lock()


def shared_worker(
    model_path: Path | str, *, threads: int, timeout: float, idle_seconds: float
) -> LamaWorkerClient:
    """One worker per configuration for the whole bot process (one inference at a time)."""
    from src.config import PROJECT_ROOT

    key = (str(model_path), threads, timeout, idle_seconds)
    with _workers_lock:
        client = _workers.get(key)
        if client is None or client.is_closed:
            client = LamaWorkerClient(
                build_worker_command(model_path, threads),
                cwd=PROJECT_ROOT,
                env={"PYTHONPATH": str(PROJECT_ROOT), "OMP_NUM_THREADS": str(threads)},
                timeout=timeout,
                idle_seconds=idle_seconds,
            )
            _workers[key] = client
        return client


def shared_worker_count() -> int:
    """How many worker clients exist in this process (0 if LaMa was never needed)."""
    with _workers_lock:
        return len(_workers)


def close_all_workers() -> None:
    with _workers_lock:
        clients = list(_workers.values())
        _workers.clear()
    for client in clients:
        client.close()


def build_worker_command(model_path: Path | str, threads: int) -> list[str]:
    """Command line of the worker; it needs the project root as cwd / on PYTHONPATH."""
    return [
        sys.executable, "-m", "src.watermark.lama_proc",
        "--model", str(model_path), "--threads", str(threads),
    ]


def _runner_handler(model_path: Path, threads: int) -> Handler:
    """Handler around the torch-only ModelRunner (torch is imported here, in the worker)."""
    from src.watermark.lama_model import ModelRunner

    runner = ModelRunner(model_path, threads=threads)

    def handle(op: str, image: np.ndarray, mask: np.ndarray) -> np.ndarray:
        if op != "predict":
            raise ValueError(f"unknown LaMa operation {op!r}")
        return runner.predict(image, mask)

    return handle


def main(argv: Sequence[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="LaMa worker process (internal)")
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="[lama-worker] %(message)s")
    serve(_runner_handler(args.model, args.threads))


if __name__ == "__main__":
    main()
