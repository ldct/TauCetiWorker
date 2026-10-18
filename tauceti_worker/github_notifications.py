"""Private, shared personal notification thread IDs; never cache mute success.

The identity-cache path is anchored before HOME isolation. Credential-specific
sibling files reuse its secure opens and process lock, held across lookup/write
so concurrent workers cannot stampede a cold or negative cache. Unsupported
hosts and unavailable caches retain the original bounded personal operation.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import secrets
import time
from collections.abc import Iterator
from pathlib import Path

from . import github_identity as identity

THREAD_TTL = 24 * 60 * 60
REFRESH_TTL = 60
MAX_ENTRIES = 2000
_MAX_BYTES = 2 * 1024 * 1024


def valid_thread_id(value: object) -> bool:
    return type(value) in (str, int) and re.fullmatch(r"[0-9]{1,30}", str(value)) is not None


def _fresh(saved: object, ttl: int) -> bool:
    try:
        return type(saved) in (int, float) and math.isfinite(saved) and 0 <= time.time() - saved < ttl
    except OverflowError:
        return False


class ThreadCache:
    def __init__(self) -> None:
        self.threads: dict[str, dict] = {}
        self.refreshed: dict[str, float] = {}
        self.dirty = False

    @staticmethod
    def _key(repo: str, pr: int) -> str:
        return f"{repo.lower()}/pulls/{pr}"

    def get(self, repo: str, pr: int) -> str | None:
        record = self.threads.get(self._key(repo, pr))
        return record["id"] if record and _fresh(record["saved_at"], THREAD_TTL) else None

    def can_refresh(self, repo: str) -> bool:
        return not _fresh(self.refreshed.get(repo.lower()), REFRESH_TTL)

    def mark_refreshed(self, repo: str) -> None:
        # Even empty/failed bounded scans are shared for a minute. A later review
        # can retry; an error must never cause another immediate scan here.
        self.refreshed[repo.lower()] = time.time()
        self.dirty = True

    def remember(self, repo: str, pr: int, thread_id: str) -> None:
        self.threads[self._key(repo, pr)] = {"id": thread_id, "saved_at": time.time()}
        self.dirty = True

    def evict(self, repo: str, pr: int) -> None:
        self.threads.pop(self._key(repo, pr), None)
        # A definite stale ID permits the NEXT review to rediscover immediately.
        self.refreshed.pop(repo.lower(), None)
        self.dirty = True

    def _bounded(self) -> None:
        self.threads = dict(
            sorted(
                ((key, value) for key, value in self.threads.items() if _fresh(value["saved_at"], THREAD_TTL)),
                key=lambda item: item[1]["saved_at"],
            )[-MAX_ENTRIES:]
        )
        self.refreshed = dict(
            sorted(
                ((key, value) for key, value in self.refreshed.items() if _fresh(value, REFRESH_TTL)),
                key=lambda item: item[1],
            )[-MAX_ENTRIES:]
        )


def _read(directory: int, name: str, key: str) -> ThreadCache:
    cache = ThreadCache()
    try:
        fd = identity._private_file(directory, name, os.O_RDONLY)
    except FileNotFoundError:
        return cache
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        try:
            record = json.loads(stream.read(_MAX_BYTES))
        except (ValueError, UnicodeError, RecursionError):
            return cache
    if not isinstance(record, dict) or record.get("version") != 1 or record.get("key") != key:
        return cache
    threads, refreshed = record.get("threads"), record.get("refreshed")
    if not isinstance(threads, dict) or not isinstance(refreshed, dict):
        return cache
    for name, entry in threads.items():
        if (
            re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+/pulls/[0-9]{1,20}", name)
            and len(name) <= 250
            and isinstance(entry, dict)
            and valid_thread_id(entry.get("id"))
            and _fresh(entry.get("saved_at"), THREAD_TTL)
        ):
            cache.threads[name] = {"id": str(entry["id"]), "saved_at": entry["saved_at"]}
    for name, saved in refreshed.items():
        if re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", name) and len(name) <= 220 and _fresh(saved, REFRESH_TTL):
            cache.refreshed[name] = saved
    cache._bounded()
    return cache


def _write(directory: int, name: str, key: str, cache: ThreadCache) -> None:
    cache._bounded()
    temporary = ".notifications-" + secrets.token_hex(16)
    fd = identity._private_file(directory, temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "key": key, "threads": cache.threads, "refreshed": cache.refreshed}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory)


@contextlib.contextmanager
def notification_threads() -> Iterator[ThreadCache]:
    """Yield a locked persistent cache, or an empty ephemeral fallback.

    Never catch/replay caller operations: in particular, a failed write must not
    accidentally be retried as part of falling back from a filesystem error.
    """
    configured = os.environ.get(identity.CACHE_ENV)
    if not configured or configured == "off" or not Path(configured).is_absolute():
        yield ThreadCache()
        return
    key = identity._credential_key()
    if key is None:
        yield ThreadCache()
        return
    with contextlib.ExitStack() as stack:
        try:
            anchor = Path(configured)
            path = anchor.with_name(anchor.name + ".notifications-" + key + ".json")
            directory = stack.enter_context(identity._locked_directory(path))
            cache = _read(directory, path.name, key)
        except (OSError, ValueError):
            yield ThreadCache()
            return
        try:
            yield cache
        finally:
            # A token may rotate while a read waits out a rate limit. Do not
            # persist its response under the old account's fingerprint.
            if cache.dirty and identity._credential_key() == key:
                with contextlib.suppress(OSError, ValueError):
                    _write(directory, path.name, key, cache)
