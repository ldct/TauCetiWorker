"""Private, credential-specific cache of the PERSONAL GitHub login (15-minute TTL).

Enable at CLI startup, before HOME isolation. The absolute cache path is inherited by
worker children; this module never obtains or uses an App installation token. Only
SHA-256(host + personal credential), the public login, and a timestamp reach disk.
An unavailable credential/cache always falls back to the caller's personal query.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import stat
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path

CACHE_ENV = "TAUCETI_GITHUB_IDENTITY_CACHE"
CACHE_TTL = 15 * 60
_HOST = "github.com"


def enable_identity_cache() -> None:
    """Anchor the cache to the real HOME, independently of GitHub App configuration.

    TAUCETI_GITHUB_IDENTITY_CACHE may name a file in a private directory, or be
    ``off`` to disable caching. No credentials or filesystem writes at startup.
    """
    configured = os.environ.get(CACHE_ENV)
    if configured == "off":
        return
    try:
        path = (
            Path(configured).expanduser() if configured else Path.home() / ".cache/tauceti/github-identity/login.json"
        )
        # Do not resolve symlinks: the secure open below must reject a symlinked
        # cache directory or file, rather than silently accepting its target.
        os.environ[CACHE_ENV] = os.path.abspath(path)
    except (OSError, RuntimeError, ValueError):
        os.environ[CACHE_ENV] = "off"


def _credential_key() -> str | None:
    # Enterprise hosts have different token precedence. Do not guess or retrieve
    # github.com's local credential for a request addressed to another host.
    host = os.environ.get("GH_HOST") or _HOST
    if host != _HOST:
        return None
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        real = os.environ.get("TAUCETI_REAL_GH") or shutil.which("gh")
        if not real:
            return None
        env = os.environ.copy()
        env.pop("GH_DEBUG", None)
        try:
            result = subprocess.run(
                [real, "auth", "token", "--hostname", _HOST],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
                check=False,
                env=env,
            )
        except (OSError, subprocess.SubprocessError, UnicodeError):
            return None
        if result.returncode != 0:
            return None
        token = (result.stdout or "").strip()
    if not token:
        return None
    try:
        return hashlib.sha256((host + "\0" + token).encode()).hexdigest()
    except UnicodeError:
        return None


def _valid_login(login: object) -> bool:
    return (
        isinstance(login, str)
        and len(login) <= 39
        and re.fullmatch(r"[a-zA-Z0-9]+(?:-[a-zA-Z0-9]+)*", login) is not None
    )


def _private_file(directory: int, name: str, flags: int) -> int:
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=directory)
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        os.close(fd)
        raise OSError("unsafe identity cache file")
    return fd


@contextlib.contextmanager
def _locked_directory(path: Path) -> Iterator[int]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise OSError("unsafe identity cache directory")
        lock = _private_file(directory, path.name + ".lock", os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield directory
        finally:
            os.close(lock)
    finally:
        os.close(directory)


def _read_login(directory: int, name: str, key: str) -> str | None:
    try:
        fd = _private_file(directory, name, os.O_RDONLY)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        try:
            record = json.loads(stream.read(8192))
        except (ValueError, UnicodeError, RecursionError):
            return None
    if not isinstance(record, dict) or record.get("version") != 1 or record.get("key") != key:
        return None
    saved = record.get("saved_at")
    try:
        if type(saved) not in (int, float) or not math.isfinite(saved):
            return None
    except OverflowError:
        return None
    if not 0 <= time.time() - saved < CACHE_TTL or not _valid_login(record.get("login")):
        return None
    return record["login"]


def _write_login(directory: int, name: str, key: str, login: str) -> None:
    temporary = ".identity-" + secrets.token_hex(16)
    fd = _private_file(directory, temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "key": key, "login": login, "saved_at": time.time()}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory)


def cached_personal_login(fetch: Callable[[], str]) -> str:
    """Return a fresh credential-matched login, or call ``fetch`` with personal auth.

    ``fetch`` must check the real gh command's exit status and raise on failure. It
    must NOT invoke a shim that re-enters this cache (the process lock is held).
    Exceptions propagate unchanged; invalid results are returned but never cached.
    Disabled caching performs no auth lookup, preserving library/test behavior.
    """
    configured = os.environ.get(CACHE_ENV)
    if not configured or configured == "off":
        return fetch()
    path = Path(configured)
    if not path.is_absolute():  # startup must anchor it before HOME can change
        return fetch()
    key = _credential_key()
    if key is None:
        return fetch()
    with contextlib.ExitStack() as stack:
        try:
            directory = stack.enter_context(_locked_directory(path))
            login = _read_login(directory, path.name, key)
        except (OSError, ValueError):
            return fetch()
        if login is not None:
            return login
        # Deliberately outside the filesystem-error handler: a failing callback
        # must not be silently retried (or replaced with an expired login).
        login = fetch()
        # Auth can rotate while an API call waits out a rate limit. Never save
        # that response under a credential which is no longer effective.
        if _valid_login(login) and _credential_key() == key:
            try:
                _write_login(directory, path.name, key, login)
            except (OSError, ValueError):
                pass
        return login
