#!/usr/bin/env python3
"""Loopback manifest handshake: state checks, restricted install, no key in browser output."""

import json
import queue
import socket
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import github_app_setup as setup


def request(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


with tempfile.TemporaryDirectory() as tmp, socket.socket() as probe:
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    path = Path(tmp) / "github-app.json"
    messages = queue.Queue()
    requests = []

    def api(endpoint, token=None, body=None):
        requests.append((endpoint, token, body))
        if endpoint.startswith("/app-manifests/"):
            return {"owner": {"login": "ldct"}, "id": 123, "slug": "ldct-test-reader", "pem": "PRIVATE-KEY-SENTINEL"}
        if endpoint.startswith("/app/installations/"):
            return {"account": {"login": "wrong" if endpoint.endswith("999") else "ldct"}, "app_id": 123}
        if endpoint.startswith("/installation/repositories"):
            return {"total_count": 1, "repositories": [{"full_name": "ldct/TauCeti", "private": False}]}
        if endpoint.startswith("/repos/TauCetiProject/TauCeti/pulls"):
            return []
        raise AssertionError(endpoint)

    with (
        patch.object(sys, "argv", ["setup", "--fork", "ldct/TauCeti", "--port", str(port)]),
        patch.object(setup, "config_path", return_value=path),
        patch.object(setup, "api", side_effect=api),
        patch.object(setup, "app_jwt", return_value="JWT-SENTINEL"),
        patch.object(setup, "installation_token", return_value="TOKEN-SENTINEL"),
        patch.object(setup, "print", side_effect=lambda value, **kw: messages.put(value)),
    ):
        worker = threading.Thread(target=setup.main, daemon=True)
        worker.start()
        link = messages.get(timeout=30).removeprefix("Open ")
        origin, state = link.split("/?state=")
        status, body = request(link)
        assert status == 200 and "Review App registration" in body and "read" in body
        assert request(origin + "/callback?code=bad&state=wrong")[0] == 403
        assert not requests and not path.exists()
        status, body = request(origin + f"/callback?code=test-code&state={state}")
        assert status == 200 and "Install on your fork" in body
        assert "PRIVATE-KEY-SENTINEL" not in body and not path.exists()
        assert request(origin + f"/installed?installation_id=999&state={state}")[0] == 400
        assert not path.exists(), "wrong account must not enable routing"
        status, body = request(origin + f"/installed?installation_id=456&state={state}")
        assert status == 200 and "Connected" in body
        assert all(secret not in body for secret in ("PRIVATE-KEY-SENTINEL", "JWT-SENTINEL", "TOKEN-SENTINEL"))
        worker.join(timeout=5)
        assert not worker.is_alive()
        conf = json.loads(path.read_text())
        assert conf["installation_id"] == 456 and path.stat().st_mode & 0o777 == 0o600
        assert any(r[0].startswith("/repos/TauCetiProject/TauCeti/pulls") for r in requests)

print("GitHub App setup handshake and installation validation: PASS")
