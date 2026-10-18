#!/usr/bin/env python3
"""Worker reviews post scoreboards without creating or uploading a TauCetiData archive.

Drive host and bubble paths with stubbed engines. Posted reviews still clear error streaks,
invalidate scoreboard caches, and charge contest counters; genuine engine failures are unchanged.
Historical outboxes are never walked, read, modified, or uploaded by the worker.
"""

import os
import socket
import subprocess
import sys
import tempfile
import types
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc
from tauceti_worker.review_diagnostics import read_review_failure

wu = tc.work_units
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}: got {got!r} want {want!r}")


class FakeCounters:
    def __init__(self):
        self.d = {}

    def read(self, name):
        return self.d.get(name, 0)

    def write(self, name, value):
        self.d[name] = value

    def incr(self, name):
        self.d[name] = self.read(name) + 1
        return self.d[name]


class FakeRS:
    def __init__(self):
        self.busted = []

    def review_rounds(self, pr, counters):
        return 0

    def bust(self, pr):
        self.busted.append(pr)


def worker(root):
    return types.SimpleNamespace(
        cfg=types.SimpleNamespace(wid="test", state=root / "state", store_dir=root / "store", logdir=root / "logs"),
        counters=FakeCounters(),
        rs=FakeRS(),
        gh=types.SimpleNamespace(ignore_pr_notifications=lambda pr: True),
    )


def seed_outbox(outbox):
    (outbox / "older" / "nested").mkdir(parents=True)
    (outbox / "empty-directory").mkdir()
    (outbox / "record.json").write_bytes(b'{"pr":726}\n')
    (outbox / "older" / "nested" / "provenance.bin").write_bytes(bytes(range(256)))
    (outbox / "older" / "empty.jsonl").write_bytes(b"")


def snapshot(root):
    """Capture historical directories, bytes and modification times outside the no-walking guard."""
    if not root.exists():
        return None
    return {
        str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None, p.stat().st_mtime_ns)
        for p in [root, *root.rglob("*")]
    }


def drive(w, candidate, opts, bubble):
    """Only the stubbed review engine may launch work; no separate archive/permission operation."""
    original_open = Path.open
    outbox_accesses = []

    def open_without_archive(path, *args, **kwargs):
        if path.is_relative_to(w.cfg.store_dir / "outbox"):
            outbox_accesses.append(path)
            raise AssertionError(f"unexpected outbox read/write: {path}")
        return original_open(path, *args, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(patch.object(Path, "open", autospec=True, side_effect=open_without_archive))
        blocked = [
            stack.enter_context(patch.object(obj, name, side_effect=AssertionError(f"unexpected {name}")))
            for obj, names in (
                (tc.github, ("gh_run",)),
                (subprocess, ("run", "Popen")),
                (socket, ("socket", "create_connection")),
                (os, ("walk", "scandir", "listdir")),
                (Path, ("rglob", "glob", "iterdir")),
            )
            for name in names
        ]
        rc = wu.do_review(w, None, candidate, opts, bubble=bubble)
    check("no extra network/subprocess or outbox walk", sum(m.call_count for m in blocked), 0)
    check("no historical outbox files opened", outbox_accesses, [])
    return rc


def test_review_lifecycle(root, bubble, contest, historical):
    label = f"{'bubble' if bubble else 'host'}, {'contest' if contest else 'normal'}, historical={historical}"
    w = worker(root)
    outbox = w.cfg.store_dir / "outbox"
    if historical:
        seed_outbox(outbox)
    before = snapshot(outbox)
    candidate = tc.Candidate(726, "deadbeef", "build-green", contest=contest)
    opts = types.SimpleNamespace(work_model="codex")
    logs, warns = [], []
    with ExitStack() as stack:
        host_engine = stack.enter_context(patch.object(wu, "run_to_logfile", return_value=0))
        bubble_engine = stack.enter_context(patch.object(wu, "review_in_bubble", return_value=0))
        stack.enter_context(patch.object(wu, "me", return_value="tester"))
        stack.enter_context(patch.object(wu, "log", logs.append))
        stack.enter_context(patch.object(wu, "warn_red", warns.append))
        # These switches formerly selected different host sync subprocess paths. Neither may
        # re-enable uploading a historical outbox when review archiving is disabled.
        stack.enter_context(
            patch.dict(os.environ, {"TAUCETI_STREAM": "1", "TAUCETI_REVIEW_ENGINE_DIR": str(root / "engine")})
        )
        engine = bubble_engine if bubble else host_engine

        w.counters.write("review-err-726", 2)
        engine.return_value = 4
        check(f"{label}: engine failure propagates", drive(w, candidate, opts, bubble), 4)
        check(f"{label}: engine failure charged", w.counters.read("review-err-726"), 3)
        check(
            f"{label}: engine failure recorded",
            read_review_failure(w.cfg.state, 726).get("attempts", [{}])[-1].get("code"),
            4,
        )
        check(f"{label}: no cache invalidation after engine failure", w.rs.busted, [])
        check(f"{label}: no contest charged after engine failure", w.counters.read("review-contest-726"), 0)

        engine.return_value = 0
        check(f"{label}: posted review succeeds", drive(w, candidate, opts, bubble), 0)
        check(f"{label}: error streak cleared", w.counters.read("review-err-726"), 0)
        check(f"{label}: failure record cleared", read_review_failure(w.cfg.state, 726), {})
        check(f"{label}: scoreboard cache invalidated", w.rs.busted, [726])
        check(f"{label}: no archive warning", warns, [])
        check(f"{label}: no TauCetiData operation logged", any("TauCetiData" in line for line in logs), False)
        check(f"{label}: no outbox created or historical bytes changed", snapshot(outbox) == before, True)
        check(f"{label}: contest counter", w.counters.read("review-contest-726"), int(bool(contest)))
        if contest:
            check(f"{label}: rubric contest counter", w.counters.read(f"review-contest-726-{contest}"), 1)
        if not bubble:
            argv = host_engine.call_args.args[0]
            check(f"{label}: engine comes from the configured source", argv[:4],
                  ["uvx", "--from", wu.REVIEW_SOURCE, "tauceti-review"])
            check(f"{label}: engine still posts", "--post" in argv, True)
            check(f"{label}: engine disables archive creation and uploads", "--no-archive" in argv, True)
            check(
                f"{label}: obsolete archive sync flags absent",
                bool({"--no-sync", "--sync-only"}.intersection(argv)),
                False,
            )
        check(
            f"{label}: only selected engine launched",
            (host_engine.call_count, bubble_engine.call_count),
            (0, 2) if bubble else (2, 0),
        )

        # Reset-on-post prevents older engine failures from escalating a later, fresh failure.
        engine.return_value = 4
        check(f"{label}: later engine failure propagates", drive(w, candidate, opts, bubble), 4)
        check(f"{label}: later engine error starts fresh streak", w.counters.read("review-err-726"), 1)
        check(f"{label}: failed retry does not invalidate again", w.rs.busted, [726])
        check(f"{label}: failed retry leaves historical outbox untouched", snapshot(outbox) == before, True)


check("engine source defaults to TauCetiReview on GitHub",
      wu.REVIEW_SOURCE if "TAUCETI_REVIEW_SOURCE" not in os.environ else "git+https://github.com/" + wu.REVIEW,
      "git+https://github.com/" + wu.REVIEW)
saved_source = wu.REVIEW_SOURCE
wu.REVIEW_SOURCE = "git+file:///srv/TauCetiReview@local-fix"
try:
    with tempfile.TemporaryDirectory() as tmp:
        test_review_lifecycle(Path(tmp), False, "", False)
finally:
    wu.REVIEW_SOURCE = saved_source

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    for bubble in (False, True):
        for contest in ("", "correctness"):
            for historical in (False, True):
                test_review_lifecycle(
                    root / f"{bubble}-{contest or 'normal'}-{historical}", bubble, contest, historical
                )

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
