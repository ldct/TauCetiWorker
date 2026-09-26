#!/usr/bin/env python3
"""Authentication boundaries, JWT signing, cache refresh and worker-scoped gh shim."""

import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import github_app as app
from tauceti_worker.github import _OPEN_PRS_QUERY, _PR_PROGRESS_QUERY

repos = ["TauCetiProject/TauCeti", "ldct/TauCeti"]
reads = [
    ["api", "--paginate", "/repos/TauCetiProject/TauCeti/pulls/123/comments?per_page=100"],
    ["api", "repos/TauCetiProject/TauCeti/issues/123/comments", "--jq", ".[]"],
    ["api", "repos/ldct/TauCeti/commits/abcd/status"],
    ["pr", "view", "123", "--repo", repos[0], "--json", "title,headRefOid,body"],
    ["pr", "diff", "123", "-R", repos[0]],
    ["pr", "list", "-R", repos[0], "--author", "ldct", "--state", "open"],
    ["issue", "list", "-R", repos[0], "--search", 'in:title "Review stuck"', "--json", "number,title,body"],
    [
        "api",
        "graphql",
        "-f",
        f"query={_OPEN_PRS_QUERY}",
        "-F",
        "owner=TauCetiProject",
        "-F",
        "repo=TauCeti",
        "-F",
        "n=100",
    ],
    [
        "api",
        "graphql",
        "-f",
        f"query={_PR_PROGRESS_QUERY}",
        "-F",
        "owner=TauCetiProject",
        "-F",
        "name=TauCeti",
        "-F",
        "pr=123",
    ],
]
personal = [
    ["api", "user"],
    ["auth", "token"],
    ["auth", "status"],
    ["api", "rate_limit"],
    ["api", "repos/TauCetiProject/TauCeti", "--jq", ".permissions.push"],
    ["api", "repos/TauCetiProject/TauCeti/notifications"],
    ["api", "repos/TauCetiProject/TauCeti/subscription"],
    ["api", "repos/private/project/pulls"],
    ["api", "https://evil.example/repos/TauCetiProject/TauCeti/pulls"],
    ["api", "repos/TauCetiProject/TauCeti/contents/../../../user"],
    ["api", "repos/TauCetiProject/TauCeti/pulls", "-X", "POST"],
    ["api", "repos/TauCetiProject/TauCeti/pulls", "--method=DELETE"],
    ["api", "repos/TauCetiProject/TauCeti/pulls", "-XPOST"],
    ["api", "repos/TauCetiProject/TauCeti/pulls", "--input", "-"],
    ["api", "repos/TauCetiProject/TauCeti/pulls", "-f", "body=hi"],
    ["api", "repos/TauCetiProject/TauCeti/pulls", "-fbody=hi"],
    ["api", "repos/TauCetiProject/TauCeti/pulls", "--field=body=hi"],
    ["api", "repos/TauCetiProject/TauCeti/pulls", "--hostname", "other.example"],
    ["api", "repos/TauCetiProject/TauCeti/pulls", "-H", "Authorization: other"],
    ["api", "graphql", "-f", "query=mutation { deleteIssue(input:{}){clientMutationId}}"],
    ["api", "graphql", "-f", "query=query {viewer {login}}"],
    ["api", "graphql", "-f", f"query={_OPEN_PRS_QUERY}", "-F", "owner=private", "-F", "repo=project"],
    ["pr", "create", "-R", repos[0]],
    ["pr", "review", "123", "-R", repos[0], "--approve"],
    ["pr", "view", "123", "-R", repos[0], "--json", "viewerDidAuthor"],
    ["pr", "view", "123"],
    ["pr", "view", "https://github.com/private/project/pull/1", "-R", repos[0]],
    ["pr", "list", "-R", repos[0], "--author", "@me"],
    ["pr", "list", "-R", repos[0], "--search", "involves:ldct"],
    ["repo", "fork", repos[0]],
    ["repo", "list", "--fork"],
]
with patch.dict(os.environ, {"GH_HOST": "github.com"}):
    for argv in reads:
        assert app.public_read(argv, repos), argv
    for argv in personal:
        assert not app.public_read(argv, repos), argv
with patch.dict(os.environ, {"GH_HOST": "enterprise.example"}):
    assert not app.public_read(reads[0], repos)

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    key = root / "app.pem"
    subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], check=True, capture_output=True)
    key.chmod(0o600)
    conf = {"app_id": 123, "installation_id": 456, "private_key": str(key), "public_repositories": repos}
    path = root / "github-app.json"
    app.private_write(path, json.dumps(conf))
    with patch.object(app.time, "time", return_value=1000):
        jwt = app.app_jwt(conf)
    header, payload, signature = jwt.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    assert claims == {"iat": 940, "exp": 1540, "iss": "123"}
    public = root / "public.pem"
    subprocess.run(["openssl", "rsa", "-in", str(key), "-pubout", "-out", str(public)], check=True, capture_output=True)
    sig = root / "signature"
    sig.write_bytes(base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)))
    verified = subprocess.run(
        ["openssl", "dgst", "-sha256", "-verify", str(public), "-signature", str(sig)],
        input=f"{header}.{payload}".encode(),
        capture_output=True,
    )
    assert verified.returncode == 0

    reply = {"token": "ghs_test_secret", "expires_at": "2030-01-01T00:00:00Z"}
    with patch.object(app, "api", return_value=reply) as mint, patch.object(app, "app_jwt", return_value="jwt"):
        assert app.installation_token(path) == reply["token"]
        assert app.installation_token(path) == reply["token"]
        assert mint.call_count == 1
        assert mint.call_args.args[2] == {"permissions": app.READ_PERMISSIONS}
        cache = next((root / "github-app-cache").glob("*.json"))
        assert cache.stat().st_mode & 0o777 == 0o600
        app.private_write(cache, json.dumps({"token": "expired", "expires": 0}))
        assert app.installation_token(path) == reply["token"] and mint.call_count == 2

    # Exercise real shim -> child executable with a seeded token cache. The fake gh
    # reports only which identity was used; no network or real credentials.
    fake = root / "real-gh"
    app.private_write(
        fake,
        '#!/bin/sh\ncase "$GH_TOKEN" in ghs_test_secret) echo app;; personal-test) echo personal;; *) exit 9;; esac\n',
        0o700,
    )
    env = {
        app.CONFIG_ENV: str(path),
        "TAUCETI_REAL_GH": str(fake),
        "GH_TOKEN": "personal-test",
        "GH_HOST": "github.com",
    }
    with patch.dict(os.environ, env):
        app.enable()
        for argv in reads + personal:
            result = subprocess.run(["gh", *argv], capture_output=True, text=True)
            expected = "app" if argv in reads else "personal"
            assert result.returncode == 0 and result.stdout.strip() == expected, (argv, result.stderr)
        assert os.environ["GH_TOKEN"] == "personal-test"
        # Failure must not silently spend the personal quota.
        key.chmod(0o644)
        result = subprocess.run(["gh", *reads[0]], capture_output=True, text=True)
        assert (
            result.returncode != 0 and "personal-test" not in result.stderr and "ghs_test_secret" not in result.stderr
        )
        key.chmod(0o600)
    with patch.dict(os.environ, {app.CONFIG_ENV: "off"}):
        before = dict(os.environ)
        app.enable()
        assert dict(os.environ) == before

print("GitHub App auth routing, JWT signature, cache refresh and shim integration: PASS")
