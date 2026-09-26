#!/usr/bin/env python3
"""Real freshness caches under a deterministically overlapping, entirely offline survey.

Exercise the actual ownership contract rather than assuming ReviewState is generally thread-safe:
observe once before the pool, distinct PRs in the pool, then forced reads/bust after it has joined.
"""

import dataclasses
import importlib
import json
import sys
import tempfile
import threading
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tauceti_worker as tc

survey_module = importlib.import_module("tauceti_worker.survey")
N = 6


class GitHub:
    def __init__(self):
        self.generation = {n: 1 for n in range(1, N + 1)}
        self.failed = set()
        self.calls = Counter()
        self.lock = threading.Lock()
        self.read_barrier = threading.Barrier(N)

    def clock(self, n):
        return f"2026-09-26T00:{n:02d}:{self.generation[n]:02d}Z"

    def open_prs(self):
        return [
            {
                "number": n,
                "headRefOid": f"head{n}",
                "updatedAt": self.clock(n),
                "author": {"login": "other"},
                "statusCheckRollup": [{"context": "build", "state": "SUCCESS"}],
            }
            for n in range(1, N + 1)
        ]

    def issue_comments(self, n):
        with self.lock:
            self.calls["issue", n] += 1
        if self.read_barrier is not None:
            self.read_barrier.wait(timeout=10)
        if n in self.failed:
            return None
        # Distinct payloads reveal any cross-PR contamination. Half the PRs also read contest state.
        meta = {
            "head_sha": f"head{n}" if n % 2 == 0 else f"old{n}",
            "runs": [{"verdict": "approve"}],
            "pr": n,
            "generation": self.generation[n],
        }
        return [{"body": f"<!--tauceti-scoreboard--> <!--tauceti-meta:v1 {json.dumps(meta)}-->"}]

    def review_comments(self, n):
        with self.lock:
            self.calls["review", n] += 1
        return [
            {"id": n, "body": "<!--tauceti-rubric:reuse-->"},
            {"id": 100 + n, "in_reply_to_id": n, "body": f"contest {n}"},
        ]

    def fresh_claim_age(self, _reply):
        return None


with tempfile.TemporaryDirectory() as tmp:
    cfg = SimpleNamespace(wid="concurrency", state=Path(tmp), store_dir=Path(tmp), sbcache=Path(tmp) / "sb")
    gh = GitHub()
    owners = {}
    owner_lock = threading.Lock()
    caller = threading.get_ident()
    write_barrier = threading.Barrier(N)
    observed = []

    class AuditedState(tc.ReviewState):
        def observe(self, prs):
            assert threading.get_ident() == caller
            with owner_lock:
                assert not owners, "observe raced active PR tasks"
            super().observe(prs)
            observed.append(dict(self._observed))

        def _write_sidecar(self, path, pr, payload):
            with owner_lock:
                if threading.get_ident() == caller:
                    assert not owners, "forced dispatch read raced survey"
                else:
                    assert owners.get(pr) == threading.get_ident(), "cache write escaped its PR owner"
            # Force all initial key sidecar writes to overlap. The real upstream mkdir, UUID temp
            # file and os.replace code runs below; this does not mock away filesystem races.
            if write_barrier is not None and path == self._key_path(pr):
                write_barrier.wait(timeout=10)
            super()._write_sidecar(path, pr, payload)

        def bust(self, pr):
            assert threading.get_ident() == caller
            with owner_lock:
                assert not owners, "cache invalidation raced survey"
            super().bust(pr)

    rs = AuditedState(cfg, gh)
    original = survey_module._survey_review

    def audited_review(p, *args):
        with owner_lock:
            assert p.number not in owners, "two concurrent jobs share a PR's memo/cache"
            assert observed and rs._observed == {n: gh.clock(n) for n in gh.generation}
            owners[p.number] = threading.get_ident()
        try:
            return original(p, *args)
        finally:
            with owner_lock:
                del owners[p.number]

    def survey():
        result = tc.survey(cfg, gh, rs, tc.Counters(cfg))
        assert not owners, "survey returned before joining its PR tasks"
        return result

    with (
        patch.object(survey_module, "SURVEY_WORKERS", N),
        patch.object(survey_module, "_survey_review", side_effect=audited_review),
        patch.object(survey_module, "me", return_value="me"),
        patch.object(survey_module, "progress_due", return_value=(False, "not due")),
        patch.object(survey_module, "_review_rounds_today", return_value=0),
    ):
        cold = survey()
        gh.read_barrier = write_barrier = None
        first_calls = gh.calls.copy()
        assert first_calls == Counter(
            {**{("issue", n): 1 for n in range(1, N + 1)}, **{("review", n): 1 for n in (2, 4, 6)}}
        )
        for n in range(1, N + 1):
            sidecar = json.loads(rs._key_path(n).read_text())
            assert sidecar["updated_at"] == gh.clock(n)
            assert sidecar["meta"]["pr"] == n and sidecar["meta"]["generation"] == 1
            assert rs.gh_meta(n).provenance == "assumed", "cache must not authorize mutation"
            if n % 2 == 0:
                contest = json.loads(rs._contest_path(n).read_text())
                assert contest["updated_at"] == gh.clock(n) and contest["contest"]["id"] == 100 + n
        assert not list(cfg.sbcache.glob("*.tmp")), "concurrent sidecar write leaked temporary files"
        assert dataclasses.asdict(survey()) == dataclasses.asdict(cold)
        assert gh.calls == first_calls, "unchanged parallel pass did not use freshness sidecars"

        # A moved clock invalidates its warm memo even when a fetch fails. The old sidecar remains
        # old, not entitled to answer for the new clock; every other PR remains untouched.
        old_key = rs._key_path(1).read_bytes()
        gh.generation[1] = 2
        gh.failed.add(1)
        survey()
        assert gh.calls == first_calls + Counter({("issue", 1): 1})
        assert rs._key_path(1).read_bytes() == old_key
        assert rs.gh_meta(1).provenance == "stale"
        assert gh.calls == first_calls + Counter({("issue", 1): 1}), "failed fetch was not memoized"

        # Dispatch's forced refresh runs only after join; it must defeat that cached failure.
        gh.failed.clear()
        forced = rs.gh_meta(1, force=True)
        assert forced.provenance == "fresh" and forced.data["generation"] == 2
        assert gh.calls == first_calls + Counter({("issue", 1): 2})
        assert json.loads(rs._key_path(1).read_text())["updated_at"] == gh.clock(1)
        rs.bust(1)
        assert not rs._key_path(1).exists() and 1 not in rs._comments
        assert all(rs._key_path(n).exists() for n in range(2, N + 1))

print("PASS: concurrent ReviewState ownership, atomic sidecars, memo isolation, failed freshness and forced dispatch")
