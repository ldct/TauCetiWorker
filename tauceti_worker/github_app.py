"""Optional installation-token routing for public GitHub reads, never personal writes.

Only worker descendants see the gh shim. Unknown commands deliberately retain personal
authentication. Tokens live in a locked private cache, not the worker/agent environment.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

CONFIG_ENV = "TAUCETI_GITHUB_APP_CONFIG"
READ_PERMISSIONS = dict.fromkeys(
    ("metadata", "contents", "issues", "pull_requests", "checks", "statuses", "actions"), "read"
)


class AppError(Exception):
    pass


def config_path() -> Path:
    return Path(os.environ.get(CONFIG_ENV) or Path.home() / ".config/tauceti/github-app.json").expanduser().resolve()


def private_write(path: Path, data: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            os.fchmod(out.fileno(), mode)
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_config(path: Path) -> dict:
    try:
        conf = json.loads(path.read_text())
        if not all(str(conf[k]).isdigit() for k in ("app_id", "installation_id")):
            raise ValueError
        key = Path(conf["private_key"]).expanduser()
        info = key.stat()
        if (
            not key.is_absolute()
            or not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or info.st_uid != os.getuid()
        ):
            raise ValueError
        repos = conf["public_repositories"]
        if (
            not isinstance(repos, list)
            or not repos
            or not all(isinstance(r, str) and re.fullmatch(r"[\w.-]+/[\w.-]+", r) for r in repos)
        ):
            raise ValueError
        return conf
    except (OSError, ValueError, KeyError, TypeError):
        raise AppError("invalid GitHub App config or private key (key must be owned by you, mode 0600)") from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api(path: str, token: str | None = None, body: dict | None = None) -> dict:
    """Fixed GitHub API origin; never follow a redirect with credentials or echo its response."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "TauCetiWorker-GitHub-App"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request("https://api.github.com" + path, data=data, headers=headers)
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise AppError(
            f"GitHub App API returned HTTP {exc.code}; check installation, permissions, and rate limits"
        ) from None
    except (OSError, ValueError):
        raise AppError("GitHub App API connection or response failed") from None


def app_jwt(conf: dict) -> str:
    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode().rstrip("=")

    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 540, "iss": str(conf["app_id"])}
    message = b64(b'{"alg":"RS256","typ":"JWT"}') + "." + b64(json.dumps(payload).encode())
    try:
        signed = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", str(Path(conf["private_key"]).expanduser())],
            input=message.encode(),
            capture_output=True,
            timeout=15,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        raise AppError("could not sign GitHub App JWT; check openssl and the private key") from None
    return message + "." + b64(signed)


def installation_token(path: Path) -> str:
    conf = read_config(path)
    # Shared across rounds/worker IDs. A changed app, installation, or key has a new cache.
    identity = hashlib.sha256(json.dumps(conf, sort_keys=True).encode()).hexdigest()[:24]
    directory = path.parent / "github-app-cache"
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    cache = directory / f"{identity}.json"
    fd = os.open(directory / f"{identity}.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            saved = json.loads(cache.read_text())
            if float(saved["expires"]) > time.time() + 120 and isinstance(saved["token"], str) and saved["token"]:
                return saved["token"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        reply = api(
            f"/app/installations/{conf['installation_id']}/access_tokens",
            app_jwt(conf),
            {"permissions": READ_PERMISSIONS},
        )
        try:
            token = reply["token"]
            expiry = datetime.fromisoformat(reply["expires_at"].replace("Z", "+00:00")).timestamp()
            if not isinstance(token, str) or not token or expiry <= time.time() + 120:
                raise ValueError
        except (KeyError, ValueError, TypeError, AttributeError):
            raise AppError("GitHub returned an invalid installation token response") from None
        private_write(cache, json.dumps({"token": token, "expires": expiry}))
        return token


# Restrict both paths and CLI grammar. GET alone is insufficient: /user and repo
# permissions depend on identity, and arbitrary GraphQL documents can contain mutations.
_REST = re.compile(
    r"(?:pulls(?:/\d+(?:/(?:comments|reviews|files|commits))?|/comments/\d+(?:/reactions)?)?"
    r"|issues(?:/\d+(?:/(?:comments|events|timeline|labels|reactions))?|/comments/\d+(?:/reactions)?)?"
    r"|commits(?:/[\w.-]+(?:/(?:status|statuses|check-runs|check-suites|comments|pulls))?)?"
    r"|compare/[\w.%-]+|contents(?:/[\w./-]+)?|labels|tags|branches(?:/[\w.-]+)?"
    r"|actions/runs(?:/\d+(?:/jobs)?)?|check-runs/\d+|check-suites/\d+(?:/check-runs)?)"
)
_JSON_FIELDS = frozenset(
    "number title body state isDraft author labels comments reviews files commits headRefOid headRefName headRepository headRepositoryOwner baseRefName baseRefOid url createdAt updatedAt closedAt mergedAt mergeable mergeStateStatus statusCheckRollup additions deletions changedFiles reviewDecision milestone assignees id".split()
)


def public_read(args: list[str], repos: list[str]) -> bool:
    """Fail closed to PERSONAL auth for unknown syntax, identity-sensitive reads and writes."""
    if os.environ.get("GH_HOST", "github.com") != "github.com" or not args:
        return False
    allowed = {r.lower() for r in repos}
    if args[0] == "api":
        endpoint = None
        fields = {}
        i = 1
        while i < len(args):
            arg = args[i]
            if arg in ("--paginate", "--slurp", "--silent", "--include", "-i"):
                i += 1
                continue
            if arg in ("--jq", "-q", "--template", "-t", "--method", "-X", "-f", "-F", "--raw-field", "--field"):
                if i + 1 == len(args):
                    return False
                value = args[i + 1]
                if arg in ("--method", "-X") and value != "GET":
                    return False
                if arg in ("-f", "-F", "--raw-field", "--field"):
                    key, sep, val = value.partition("=")
                    if not sep or key in fields or val.startswith("@"):
                        return False
                    fields[key] = val
                i += 2
                continue
            if arg.startswith("-") or endpoint is not None:
                return False
            endpoint = arg
            i += 1
        if endpoint == "graphql":
            # Only the two built-in, reviewed documents, compared verbatim. No heuristic
            # 'not mutation' test that could accidentally switch viewer/account identity.
            from .github import _OPEN_PRS_QUERY, _PR_PROGRESS_QUERY

            query = fields.get("query")
            name = fields.get("repo") if query == _OPEN_PRS_QUERY else fields.get("name")
            return (
                query in (_OPEN_PRS_QUERY, _PR_PROGRESS_QUERY)
                and set(fields) <= {"query", "owner", "repo", "name", "n", "cursor", "pr"}
                and f"{fields.get('owner')}/{name}".lower() in allowed
            )
        if fields or not endpoint:
            return False
        match = re.fullmatch(r"/?repos/([\w.-]+/[\w.-]+)/([^?#]+)(?:\?[^#]*)?", endpoint)
        return bool(
            match and match[1].lower() in allowed and _REST.fullmatch(match[2]) and ".." not in match[2].split("/")
        )
    if len(args) < 2 or args[0] not in ("pr", "issue") or args[1] not in ("list", "view", "diff", "checks"):
        return False
    if args[0] == "issue" and args[1] not in ("list", "view"):
        return False
    repo = None
    i = 2
    while i < len(args):
        arg = args[i]
        if arg in ("--comments", "--patch", "--name-only", "--required"):
            i += 1
            continue
        if arg in (
            "--repo",
            "-R",
            "--json",
            "--jq",
            "-q",
            "--template",
            "-t",
            "--state",
            "--limit",
            "-L",
            "--label",
            "-l",
            "--author",
            "-A",
            "--search",
            "-S",
            "--base",
            "-B",
            "--head",
            "-H",
        ):
            if i + 1 == len(args):
                return False
            value = args[i + 1]
            if arg in ("--repo", "-R"):
                if repo is not None:
                    return False
                repo = value.lower()
            if arg == "--json" and not set(value.split(",")) <= _JSON_FIELDS:
                return False
            if "@me" in value or re.search(r"\b(?:viewer|involves|assignee|review-requested):", value):
                return False
            i += 2
            continue
        if not arg.isdigit():  # no inferred repo, URL, branch, flag or alias
            return False
        i += 1
    return repo in allowed


def enable() -> None:
    """Prepend a private, interpreter-pinned shim only for this worker's process tree."""
    if os.environ.get(CONFIG_ENV) == "off":
        return
    path = config_path()
    if not path.exists():
        if os.environ.get(CONFIG_ENV):
            raise AppError("configured GitHub App file does not exist")
        return
    read_config(path)
    os.environ[CONFIG_ENV] = str(path)  # stable even when Linux workers isolate HOME
    real = os.environ.get("TAUCETI_REAL_GH") or shutil.which("gh")
    if not real or not Path(real).is_absolute():
        raise AppError("could not resolve the real gh executable")
    os.environ["TAUCETI_REAL_GH"] = real
    # Version each shim by interpreter/module so upgrades don't change a running worker.
    module_root = str(Path(__file__).resolve().parent.parent)
    ident = hashlib.sha256((sys.executable + module_root).encode()).hexdigest()[:16]
    directory = path.parent / "gh-read-shims" / ident
    code = f"import sys; sys.path.insert(0, {module_root!r}); from tauceti_worker.github_app import gh_main; gh_main()"
    private_write(
        directory / "gh",
        "#!/bin/sh\nexec " + shlex.quote(sys.executable) + " -c " + shlex.quote(code) + ' "$@"\n',
        0o700,
    )
    parts = os.environ.get("PATH", "").split(os.pathsep)
    os.environ["PATH"] = os.pathsep.join([str(directory), *(p for p in parts if p != str(directory))])


def gh_main() -> None:
    try:
        args = sys.argv[1:]
        path = config_path()
        conf = read_config(path)
        env = os.environ.copy()
        if public_read(args, conf["public_repositories"]):
            env["GH_TOKEN"] = installation_token(path)
            env.pop("GITHUB_TOKEN", None)
            # Don't allow HTTP-debug logging to print an App token or response headers.
            env.pop("GH_DEBUG", None)
        real = os.environ["TAUCETI_REAL_GH"]
        os.execve(real, [real, *args], env)
    except (AppError, OSError, KeyError) as exc:
        message = str(exc) if isinstance(exc, AppError) else "could not launch gh"
        print(f"tauceti GitHub App: {message}; no personal-token fallback", file=sys.stderr)
        raise SystemExit(1) from None
