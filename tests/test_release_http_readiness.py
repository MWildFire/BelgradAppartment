"""Cold startup receives a bounded real HTTP gate; failure never becomes readiness."""

from __future__ import annotations

import argparse
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scripts import release


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, value):
        self.now += value


def test_startup_slower_than_old_six_second_window_is_actually_verified(monkeypatch):
    clock, attempts = Clock(), []
    monkeypatch.setattr(release.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(release.time, "sleep", clock.sleep)

    def request(command, **kwargs):
        attempts.append(clock.now)
        assert command[:2] == ["curl", "--disable"]
        assert kwargs["capture"] and kwargs["timeout"] <= 3
        assert "--max-time" in command and "--connect-timeout" in command
        if clock.now < 8:
            raise subprocess.CalledProcessError(7, command)
        return "200"

    monkeypatch.setattr(release, "run", request)
    result = release.wait_for_http_health(
        "http://127.0.0.1:8001/healthz", "http://127.0.0.1:8001/readyz"
    )
    assert result == {
        "statuses": {"/healthz": 200, "/readyz": 200},
        "attempts": 10,
        "elapsed_sec": 8.0,
    }
    assert attempts == [*range(9), 8]


@pytest.mark.parametrize("failure", ["503", "302", "invalid", "hung"])
def test_permanent_failure_or_hung_request_has_one_total_deadline(monkeypatch, failure):
    clock, attempts = Clock(), []
    monkeypatch.setattr(release.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(release.time, "sleep", clock.sleep)

    def request(command, **kwargs):
        attempts.append(kwargs["timeout"])
        assert kwargs["timeout"] <= min(3, 90 - clock.now)
        if failure == "hung":
            clock.now += kwargs["timeout"]
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return failure

    monkeypatch.setattr(release, "run", request)
    with pytest.raises(release.ReleaseError, match=r"HTTP 200.*90"):
        release.wait_for_http_health("http://127.0.0.1:8001/healthz")
    assert attempts and clock.now == 90


def test_real_loopback_http_rejects_errors_and_redirect_before_accepting_200():
    statuses = [503, 302, 200]
    observed = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            status = 200 if self.path == "/healthz" else statuses[len(observed)]
            if self.path == "/readyz":
                observed.append(status)
            self.send_response(status)
            if status == 302:
                self.send_header("Location", "/healthz")
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        result = release.wait_for_http_health(
            f"http://127.0.0.1:{server.server_port}/healthz",
            f"http://127.0.0.1:{server.server_port}/readyz",
            timeout=3,
            poll_interval=0.01,
            request_timeout=0.5,
        )
        assert observed == statuses
        assert result["statuses"] == {"/healthz": 200, "/readyz": 200}
        assert result["attempts"] == 6
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=1)


def test_readyz_shares_one_deadline_even_when_healthz_always_succeeds(monkeypatch):
    clock, urls = Clock(), []
    monkeypatch.setattr(release.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(release.time, "sleep", clock.sleep)

    def request(command, **kwargs):
        urls.append(command[-1])
        if command[-1].endswith("/readyz"):
            assert kwargs["timeout"] <= 90 - clock.now
            clock.now += kwargs["timeout"]
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return "200"

    monkeypatch.setattr(release, "run", request)
    with pytest.raises(release.ReleaseError, match=r"HTTP 200.*90"):
        release.wait_for_http_health(
            "http://127.0.0.1:8001/healthz", "http://127.0.0.1:8001/readyz"
        )
    assert clock.now == 90 and any(url.endswith("/readyz") for url in urls)


def test_ambient_curlrc_cannot_redirect_unready_database_to_healthz(tmp_path, monkeypatch):
    (tmp_path / ".curlrc").write_text("location\n")
    monkeypatch.setenv("CURL_HOME", str(tmp_path))
    requested = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requested.append(self.path)
            self.send_response(302 if self.path == "/readyz" else 200)
            if self.path == "/readyz":
                self.send_header("Location", "/healthz")
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with pytest.raises(release.ReleaseError, match="HTTP 200"):
            release.wait_for_http_health(
                f"http://127.0.0.1:{server.server_port}/healthz",
                f"http://127.0.0.1:{server.server_port}/readyz",
                timeout=0.3,
                poll_interval=0.01,
                request_timeout=0.1,
            )
        assert "/readyz" in requested
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=1)


def test_old_schema_backup_cannot_unlock_retry_after_additive_migration(tmp_path, monkeypatch):
    root = tmp_path / "app"
    (root / "data/state").mkdir(parents=True)
    uv = tmp_path / "uv"
    uv.touch()
    receipt = tmp_path / "backup.json"
    receipt.write_text(json.dumps({"database": {"revisions": ["0011"]}}))
    original = release.require

    def isolated_require(condition, message):
        if message.startswith("canonical units target"):
            return
        original(condition, message)

    monkeypatch.setattr(release, "require", isolated_require)
    monkeypatch.setattr(release.os, "geteuid", lambda: 0)
    monkeypatch.setattr(release, "validate_backup", lambda _receipt: None)
    monkeypatch.setattr(release, "unpack_packet", lambda *_args: {"revision": "a" * 40})
    monkeypatch.setattr(release, "current_revisions", lambda _path: ["0013"])
    monkeypatch.setattr(release, "run", lambda *_args, **_kwargs: pytest.fail("host command ran"))
    with pytest.raises(release.ReleaseError, match="live schema no longer matches"):
        release.activate(
            argparse.Namespace(
                remote_root=root,
                backup_receipt=receipt,
                uv_bin=uv,
                packet=tmp_path / "packet",
            )
        )
    assert not (root / ".releases").exists()
