"""Real pinned Leaflet bytes for deterministic browser tests without CDN access."""

import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

VENDOR = Path(__file__).parent / "vendor"


def load_assets(folder=VENDOR):
    result = {}
    for name, metadata in json.loads((folder / "manifest.json").read_text()).items():
        if Path(name).name != name:
            raise ValueError("unsafe vendor filename")
        raw = (folder / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != metadata["sha256"]:
            raise ValueError("vendored browser asset checksum differs")
        if name.endswith((".css", ".js")):
            result[metadata["url"]] = (raw, "text/css" if name.endswith(".css")
                                      else "application/javascript")
    return result


def offline_context(browser, **options):
    assets = load_assets()
    context = browser.new_context(**(options | {"service_workers": "block"}))

    def route(request):
        url = request.request.url
        if url in assets:
            body, content_type = assets[url]
            request.fulfill(body=body, content_type=content_type,
                            headers={"Access-Control-Allow-Origin": "*"})
        elif urlsplit(url).hostname in {"127.0.0.1", "localhost", "::1"}:
            request.continue_()
        else:
            request.abort()

    context.route("**/*", route)
    return context
