"""Machine-wide back-off for a reviewer provider that is rate limiting this account.

The review engine stops a round with REVIEW_PROVIDER_DOWN_EXIT when its provider answers 429 to
consecutive rubrics. The worker already refuses to charge the PR for that, but nothing remembered it:
a fleet manager that launches a fresh one-shot round every minute walked straight back into the same
limit, cloning and launching the engine each time for nothing, and the in-process loop back-off never
saw it because every round is a new process.

So the event is recorded on disk, in the operator's real home (rounds run under an isolated $HOME),
where every worker on this machine reads it before launching that provider. Each consecutive
rate-limited round doubles the wait (BACKOFF_STEPS_S); one successful review clears it.

Only rate limits land here. An authentication failure is not cured by waiting and is reported by the
quota pacer as a hard block; an exhausted subscription window is likewise the pacer's to report."""

from __future__ import annotations

import json
import math
import os
import pwd
import re
import time
from pathlib import Path

BACKOFF_STEPS_S = (300, 600, 1200, 2400, 3600)

# The engine's final line for this abort (TauCetiReview runner/review.py _PROVIDER_DOWN_PHRASE):
#   "review aborted: the provider is rate limiting this account (every configured provider — codex —
#    failed 2 consecutive attempts)"
_RATE_LIMIT_LINE = re.compile(r"review aborted: the provider is rate limiting this account(?P<rest>.*)")
_PROVIDERS = re.compile(r"every configured provider\s*[—-]+\s*(?P<names>[^—]+?)\s*[—-]+")


def _dir() -> Path:
    override = os.environ.get("TAUCETI_PROVIDER_BACKOFF_DIR")
    if override:
        return Path(override)
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".cache" / "tauceti" / "provider-backoff"


def _path(provider: str) -> Path:
    return _dir() / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', provider)}.json"


def _read(provider: str) -> dict:
    try:
        d = json.loads(_path(provider).read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _finite(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def remaining(provider: str, now: float | None = None) -> tuple[float, int] | None:
    """(seconds left, consecutive rate-limited rounds) while a back-off is in force, else None."""
    d = _read(provider)
    until, strikes = d.get("until"), d.get("strikes")
    if not _finite(until):
        return None
    left = min(until - (time.time() if now is None else now), max(BACKOFF_STEPS_S))
    if left <= 0:
        return None
    return left, int(strikes) if _finite(strikes) else 1


def record(provider: str, now: float | None = None) -> float:
    """Note one more consecutive rate-limited round and return the wait now in force. Never shortens a
    longer wait another worker already set. A write failure is not fatal: the round still reports it."""
    now = time.time() if now is None else now
    d = _read(provider)
    prev = d.get("strikes")
    strikes = (int(prev) if _finite(prev) and prev >= 0 else 0) + 1
    wait = BACKOFF_STEPS_S[min(strikes, len(BACKOFF_STEPS_S)) - 1]
    until = now + wait
    if _finite(d.get("until")) and d["until"] > until:
        until = d["until"]
    path = _path(provider)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"until": until, "strikes": strikes, "recorded": now}))
        os.replace(tmp, path)
    except OSError:
        pass
    return until - now


def clear(provider: str) -> None:
    """A review on this provider succeeded: the limit has lifted, so the next one starts from the bottom."""
    try:
        _path(provider).unlink()
    except OSError:
        pass


def rate_limited_providers(log_text: str, configured: list[str]) -> list[str]:
    """The providers the engine's log says aborted the round for rate limiting, or [] when the abort had
    another cause. The engine names them on its final line; `configured` covers an engine that does not."""
    found: list[str] = []
    for m in _RATE_LIMIT_LINE.finditer(log_text):
        names = _PROVIDERS.search(m.group("rest"))
        if names:
            found += [n.strip() for n in names.group("names").split(",") if n.strip()]
        else:
            found += configured
    return sorted(set(found))


def describe(provider: str, left: float, strikes: int, now: float | None = None) -> str:
    at = time.strftime("%H:%M", time.localtime((time.time() if now is None else now) + left))
    rounds = "round" if strikes == 1 else f"{strikes} consecutive rounds"
    return (
        f"{provider} rate-limited by the provider (its last {rounds} stopped on 429s) — "
        f"backing off {math.ceil(left / 60)}m, retry at {at}"
    )
