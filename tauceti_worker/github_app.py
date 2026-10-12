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
    r"|actions/runs(?:/\d+(?:/jobs)?)?|actions/jobs/\d+|check-runs/\d+|check-suites/\d+(?:/check-runs)?"
    r"|git/(?:ref|refs|matching-refs)/[\w./-]+|git/(?:trees|blobs|commits)/[\w.-]+)"
)
_JSON_FIELDS = frozenset(
    "number title body state isDraft author labels comments reviews files commits headRefOid headRefName headRepository headRepositoryOwner baseRefName baseRefOid url createdAt updatedAt closedAt mergedAt mergeable mergeStateStatus statusCheckRollup additions deletions changedFiles reviewDecision milestone assignees id".split()
)


# TauCetiReview runner/post.py's public thread lookup. Keep the document exact:
# the following resolveReviewThread mutation must remain personal.
_REVIEW_THREADS_QUERY = (
    "query($owner:String!,$name:String!,$pr:Int!){repository(owner:$owner,name:$name){"
    "pullRequest(number:$pr){reviewThreads(first:100){nodes{id isResolved "
    "comments(first:1){nodes{databaseId}}}}}}}"
)
_API_VALUES = frozenset(
    (
        "--jq",
        "-q",
        "--template",
        "-t",
        "--method",
        "-X",
        "-f",
        "-F",
        "--raw-field",
        "--field",
        "--hostname",
        "--header",
        "-H",
    )
)
_GET_FIELDS = frozenset(("per_page", "page", "state", "since", "sort", "direction", "base", "head"))
_SAFE_ACCEPT = frozenset(
    (
        "application/vnd.github+json",
        "application/vnd.github.v3+json",
        "application/vnd.github.raw+json",
        "application/vnd.github.v3.raw",
        "application/vnd.github.diff",
    )
)


def _expand_options(args: list[str], valued: frozenset[str]) -> list[str]:
    """Normalize only known value-taking options; unknown grammar stays rejected."""
    out = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in valued:
            out.append(arg)
            i += 1
            if i < len(args):
                out.append(args[i])
        elif arg.startswith("--") and "=" in arg and arg.split("=", 1)[0] in valued:
            out.extend(arg.split("=", 1))
        elif len(arg) > 2 and arg[:2] in valued and not arg.startswith("--"):
            out.extend((arg[:2], arg[2:]))
        else:
            out.append(arg)
        i += 1
    return out


def _api_public_read(args: list[str], allowed: set[str]) -> bool:
    args = _expand_options(args, _API_VALUES)
    endpoint = None
    fields = {}
    method = None
    host_seen = False
    headers_seen = set()
    i = 1
    while i < len(args):
        arg = args[i]
        if arg in ("--paginate", "--slurp", "--silent", "--include", "-i"):
            i += 1
            continue
        if arg in _API_VALUES:
            if i + 1 == len(args):
                return False
            value = args[i + 1]
            if arg in ("--method", "-X"):
                if method is not None or value != "GET":
                    return False
                method = value
            elif arg == "--hostname":
                if host_seen or value != "github.com":
                    return False
                host_seen = True
            elif arg in ("--header", "-H"):
                name, sep, val = value.partition(":")
                name, val = name.strip().lower(), val.strip()
                if not sep or name in headers_seen or "\n" in value or "\r" in value:
                    return False
                headers_seen.add(name)
                if not (
                    name == "accept" and val in _SAFE_ACCEPT or name == "x-github-api-version" and val == "2022-11-28"
                ):
                    return False
            elif arg in ("-f", "-F", "--raw-field", "--field"):
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
        from .github import _OPEN_PRS_QUERY, _PR_PROGRESS_QUERY
        from .github_comments import COMMENTS_QUERIES

        query = fields.get("query")
        # Exact trusted documents only: variables cannot introduce fields or mutations.
        for size, document in COMMENTS_QUERIES.items():
            if query == document:
                required = {"query", "owner", "name"} | {f"pr{i}" for i in range(size)}
                optional = {f"cursor{i}" for i in range(size)}
                return (
                    required <= set(fields) <= required | optional
                    and all(re.fullmatch(r"[1-9][0-9]*", fields[f"pr{i}"]) for i in range(size))
                    and f"{fields.get('owner')}/{fields.get('name')}".lower() in allowed
                )
        name = fields.get("repo") if query == _OPEN_PRS_QUERY else fields.get("name")
        return (
            query in (_OPEN_PRS_QUERY, _PR_PROGRESS_QUERY, _REVIEW_THREADS_QUERY)
            and set(fields) <= {"query", "owner", "repo", "name", "n", "cursor", "pr"}
            and f"{fields.get('owner')}/{name}".lower() in allowed
        )
    if not endpoint:
        return False
    # gh defaults to POST when any field is present. Only explicit GET permits
    # safe query fields; never classify an implicit write as an App read.
    if fields and (method != "GET" or not set(fields) <= _GET_FIELDS):
        return False
    match = re.fullmatch(r"/?repos/([\w.-]+/[\w.-]+)/([^?#]+)(?:\?[^#]*)?", endpoint)
    return bool(
        match
        and match[1].lower() in allowed
        and _REST.fullmatch(match[2])
        and ".." not in match[2].split("/")
        and "%" not in match[2]
    )


def public_read(args: list[str], repos: list[str]) -> bool:
    """Fail closed to PERSONAL auth for unknown syntax, identity-sensitive reads and writes."""
    if os.environ.get("GH_HOST", "github.com") != "github.com" or not args:
        return False
    allowed = {r.lower() for r in repos}
    if args[0] == "api":
        return _api_public_read(args, allowed)
    args = _expand_options(
        args,
        frozenset(
            (
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
            )
        ),
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
        env.pop("GH_DEBUG", None)
        real = os.environ["TAUCETI_REAL_GH"]
        # Reuse the shared PERSONAL identity cache for trusted review/progress
        # subprocesses too. Never cache arbitrary /user fields or use App auth.
        if args in (["api", "user", "--jq", ".login"], ["api", "/user", "--jq", ".login"]):
            from .github_identity import cached_personal_login

            def fetch_login() -> str:
                result = subprocess.run([real, *args], env=env, text=True, capture_output=True, check=True)
                return result.stdout.strip()

            try:
                print(cached_personal_login(fetch_login))
                return
            except subprocess.CalledProcessError as exc:
                # Preserve real-gh diagnostics, notably primary/secondary limits,
                # so the Worker's retry policy and scheduler can recognize them.
                sys.stdout.write(exc.stdout or "")
                sys.stderr.write(exc.stderr or "")
                raise SystemExit(exc.returncode if exc.returncode > 0 else 128 - exc.returncode) from None
        if public_read(args, conf["public_repositories"]):
            env["GH_TOKEN"] = installation_token(path)
            env.pop("GITHUB_TOKEN", None)
            # Don't allow HTTP-debug logging to print an App token or response headers.
            env.pop("GH_DEBUG", None)
        os.execve(real, [real, *args], env)
    except (AppError, OSError, KeyError) as exc:
        message = str(exc) if isinstance(exc, AppError) else "could not launch gh"
        print(f"tauceti GitHub App: {message}; no personal-token fallback", file=sys.stderr)
        raise SystemExit(1) from None
