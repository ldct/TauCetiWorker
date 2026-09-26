"""One-time loopback-only GitHub App manifest setup. No secrets enter terminal/browser logs."""

from __future__ import annotations

import argparse
import html
import json
import re
import secrets
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

from .github_app import READ_PERMISSIONS, AppError, api, app_jwt, config_path, installation_token, private_write
from .paths import ensure_ssl_cert_file


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fork", required=True, help="your public fork, e.g. ldct/TauCeti")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", args.fork):
        parser.error("--fork must be owner/repository")
    ensure_ssl_cert_file()
    destination = config_path()
    if destination.exists():
        parser.error(f"already configured at {destination}; move it aside explicitly before creating another app")
    nonce = secrets.token_urlsafe(32)
    owner = args.fork.split("/")[0]
    origin = f"http://127.0.0.1:{args.port}"
    pending = destination.parent / "github-app-pending.json"
    key = destination.parent / "github-app.pem"
    if pending.exists():
        saved = json.loads(pending.read_text())
        if saved.get("fork") != args.fork or saved.get("setup_port") != args.port:
            parser.error("resume the pending App setup using the same --fork and --port")
        nonce = saved["setup_nonce"]
    manifest = {
        "name": f"{owner}-tauceti-reader",
        "url": "https://github.com/ldct/TauCetiWorker",
        "description": "Read public TauCeti data with a separate installation API budget. Contributions use personal credentials.",
        "public": False,
        "hook_attributes": {"url": "https://github.com/ldct/TauCetiWorker", "active": False},
        "redirect_url": origin + "/callback",
        "setup_url": origin + "/installed?state=" + nonce,
        "default_permissions": READ_PERMISSIONS,
        "default_events": [],
        "request_oauth_on_install": False,
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # callback codes must never enter access logs

        def page(self, body: str, status: int = 200) -> None:
            data = (
                "<!doctype html><meta charset=utf-8><title>TauCeti GitHub App setup</title>"
                "<body><h1>TauCeti GitHub App setup</h1>" + body + "</body>"
            ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; form-action https://github.com; frame-ancestors 'none'; base-uri 'none'",
            )
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.headers.get("Host") != f"127.0.0.1:{args.port}":
                self.page("Host denied", 403)
                return
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            if not secrets.compare_digest(query.get("state", [""])[0], nonce):
                self.page("Use the setup link printed in your terminal.", 403)
                return
            try:
                if url.path == "/":
                    if pending.exists():
                        saved = json.loads(pending.read_text())
                        self.install_page(saved["slug"])
                        return
                    self.page(
                        f"<p>Create a private App owned by <b>{html.escape(owner)}</b>, then install it on "
                        f"<b>only {html.escape(args.fork)}</b>.</p>"
                        "<p>Permissions: read-only metadata, contents, issues, pull requests, checks, commit statuses, "
                        "and Actions. No writes, account permissions, webhooks, or user authorization.</p>"
                        '<form method="post" action="https://github.com/settings/apps/new?state=' + nonce + '">'
                        '<input type="hidden" name="manifest" value="'
                        + html.escape(json.dumps(manifest), quote=True)
                        + '">'
                        '<button type="submit">Review App registration on GitHub</button></form>'
                    )
                elif url.path == "/callback":
                    code = query.get("code", [""])[0]
                    if pending.exists() or not re.fullmatch(r"[A-Za-z0-9_-]+", code):
                        raise AppError("setup callback invalid or already used; return to the initial setup link")
                    reply = api(f"/app-manifests/{code}/conversions", body={})
                    if reply["owner"]["login"].lower() != owner.lower():
                        raise AppError("App was created under a different account; configure it explicitly instead")
                    private_write(key, reply["pem"])
                    saved = {
                        "app_id": reply["id"],
                        "private_key": str(key),
                        "slug": reply["slug"],
                        "setup_nonce": nonce,
                        "setup_port": args.port,
                        "fork": args.fork,
                    }
                    private_write(pending, json.dumps(saved))
                    print("App registered; awaiting installation on the selected fork.", flush=True)
                    self.install_page(reply["slug"])
                elif url.path == "/installed":
                    installation = query.get("installation_id", [""])[0]
                    if not installation.isdigit():
                        raise AppError("GitHub did not provide an installation ID")
                    saved = json.loads(pending.read_text())
                    info = api(f"/app/installations/{installation}", app_jwt(saved))
                    if info["account"]["login"].lower() != owner.lower() or info["app_id"] != saved["app_id"]:
                        raise AppError("installation does not match the selected App/account")
                    conf = {
                        "app_id": saved["app_id"],
                        "installation_id": int(installation),
                        "private_key": str(key),
                        "public_repositories": [args.fork, "TauCetiProject/TauCeti", "TauCetiProject/TauCetiRoadmap"],
                    }
                    staged = destination.parent / "github-app-staged.json"
                    private_write(staged, json.dumps(conf, indent=2))
                    token = installation_token(staged)
                    repos = api("/installation/repositories?per_page=100", token)
                    if (
                        repos["total_count"] != 1
                        or repos["repositories"][0]["full_name"].lower() != args.fork.lower()
                        or repos["repositories"][0]["private"]
                    ):
                        raise AppError("install the App on ONLY your public fork, then retry installation")
                    # Prove access to upstream public data without granting upstream write access.
                    api("/repos/TauCetiProject/TauCeti/pulls?state=open&per_page=1", token)
                    private_write(destination, json.dumps(conf, indent=2) + "\n")
                    staged.unlink(missing_ok=True)
                    pending.unlink(missing_ok=True)
                    self.page(
                        "<p>Connected. Verified read access to public upstream pull requests. "
                        "New worker processes will use the App for recognized public reads and your personal account for writes. "
                        "You can close this page.</p>"
                    )
                    print(f"GitHub App connected and public upstream reads verified. Config: {destination}", flush=True)
                    self.server.connected = True
                else:
                    self.page("Not found", 404)
            except (AppError, KeyError, ValueError, OSError, IndexError, TypeError):
                self.page(
                    "<p>Setup could not complete. Check that the App belongs to the fork owner, is installed on "
                    "only the public fork, and has the listed read permissions. Return to the setup link to retry. "
                    "No existing personal GitHub credentials were changed.</p>",
                    400,
                )
                print("GitHub App setup could not complete; no secret details logged.", flush=True)

        def install_page(self, slug: str) -> None:
            if not re.fullmatch(r"[a-z0-9-]+", slug):
                raise AppError("invalid App slug")
            self.page(
                f"<p>App registered. Select <b>Only select repositories → {html.escape(args.fork)}</b>.</p>"
                f'<p><a href="https://github.com/apps/{slug}/installations/new">Install on your fork</a></p>'
            )

    with HTTPServer(("127.0.0.1", args.port), Handler) as server:
        server.connected = False
        server.timeout = 1
        print(f"Open {origin}/?state={nonce}", flush=True)
        deadline = time.monotonic() + 3600
        while not server.connected and time.monotonic() < deadline:
            server.handle_request()


if __name__ == "__main__":
    main()
