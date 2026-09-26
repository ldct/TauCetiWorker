#!/usr/bin/env python3
"""Bounded remote reads preserve review decisions, order, memoization and progress output."""

import contextlib
import dataclasses
import importlib
import io
import json
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tauceti_worker as tc

survey_module = importlib.import_module("tauceti_worker.survey")


class FakeGitHub:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = self.peak = 0
        self.calls = Counter()
        self.clocks = {}
        self.forced_holds = set()

    def open_prs(self):
        return [
            {
                "number": n,
                "updatedAt": self.clocks.get(n, "2026-09-26T00:00:00Z"),
                "headRefOid": f"head{n}",
                "author": {"login": "other"},
                "statusCheckRollup": [{"context": "build", "state": "SUCCESS"}],
            }
            for n in range(1, 13)
        ]

    def issue_comments(self, n):
        with self.lock:
            self.active += 1
            self.peak = max(self.active, self.peak)
            self.calls[n] += 1
        try:
            # Vary latency to ensure completion order differs from PR-list order.
            time.sleep(0.005 * (7 - n % 6))
            if n % 4 == 0:
                meta = {"head_sha": f"head{n}", "runs": [{"verdict": "approve"}]}
                return [{"body": f"<!--tauceti-scoreboard--> <!--tauceti-meta:v1 {json.dumps(meta)}-->"}]
            if n % 4 == 2 or n in self.forced_holds:
                marker = {"head": f"head{n}", "providers": ["codex"], "expires_at": int(time.time()) + 3600}
                return [{"body": f"<!--tauceti-review-in-progress {json.dumps(marker)}-->"}]
            return []
        finally:
            with self.lock:
                self.active -= 1

    def review_comments(self, n):
        return [
            {"id": 1, "body": "<!--tauceti-rubric:reuse-->"},
            {"id": n + 100, "in_reply_to_id": 1, "body": "contest"},
        ]

    def fresh_claim_age(self, _reply):
        return None


def run(workers, deep=True):
    with tempfile.TemporaryDirectory() as tmp:
        cfg = SimpleNamespace(wid="test", state=Path(tmp), store_dir=Path(tmp), sbcache=Path(tmp) / "sb")
        gh = FakeGitHub()
        counters = tc.Counters(cfg)
        counters.write("review-err-7", tc.MAX_REVIEW_ERRORS)
        messages = []
        caller = threading.get_ident()

        def report(message):
            assert threading.get_ident() == caller, "progress callback left the survey caller thread"
            messages.append(message)

        with (
            patch.object(survey_module, "SURVEY_WORKERS", workers),
            patch.object(survey_module, "me", return_value="me"),
            patch.object(survey_module, "progress_due", return_value=(True, "due")),
            patch.object(survey_module, "_review_rounds_today", side_effect=lambda _, n: 100 if n == 11 else 0),
        ):
            rs = tc.ReviewState(cfg, gh)
            sv = tc.survey(cfg, gh, rs, counters, deep=deep, progress=report)
            if deep:
                first_calls = gh.calls.copy()
                repeat = tc.survey(cfg, gh, rs, counters, deep=deep)
                assert dataclasses.asdict(repeat) == dataclasses.asdict(sv)
                assert gh.calls == first_calls, "unchanged clocks must skip concurrent comment reads"
                for n in range(1, 13):
                    sidecar = json.loads((cfg.sbcache / f"{n}.key.json").read_text())
                    assert sidecar["updated_at"] == "2026-09-26T00:00:00Z"
                # A clock change during a warm memo must force exactly that PR to be fetched again.
                gh.clocks[1] = "2026-09-26T00:01:00Z"
                gh.forced_holds.add(1)
                changed = tc.survey(cfg, gh, rs, counters, deep=deep)
                assert gh.calls == first_calls + Counter({1: 1})
                assert [n for n, _ in changed.review_inflight] == [1, 2, 6, 10]
                gh.calls = first_calls  # retain the first-pass comparison below
        return sv, gh, messages


serial, serial_gh, _ = run(1)
parallel, parallel_gh, progress = run(6)
assert dataclasses.asdict(serial) == dataclasses.asdict(parallel)
assert [c.pr for c in parallel.reviewable.actionable] == [1, 3, 4, 5, 8, 9, 12]
assert parallel.review_stuck == [7]
assert [n for n, _ in parallel.review_capped] == [11]
assert [n for n, _ in parallel.review_inflight] == [2, 6, 10]
assert 1 < parallel_gh.peak <= 6 and serial_gh.peak == 1
assert serial_gh.calls == parallel_gh.calls == Counter(range(1, 13)), "one issue fetch per PR"
assert "Checking PR reviews: 12/12" in progress
shallow, shallow_gh, _ = run(6, deep=False)
assert len(shallow.reviewable.actionable) == 12 and not shallow_gh.calls

# JSON stays clean, and both long-running stages report immediately to stderr.
stdout, stderr = io.StringIO(), io.StringIO()


def fake_survey(*_args, **kwargs):
    kwargs["progress"]("Fetching open PRs from GitHub…")
    return tc.Survey(worker_id="test")


with (
    patch.object(
        tc.cli.Config, "resolve", return_value=SimpleNamespace(sbcache=Path("/unused"), state=Path("/unused"))
    ),
    patch.object(tc.cli, "survey", side_effect=fake_survey),
    patch.object(tc.cli, "Quota") as quota,
    contextlib.redirect_stdout(stdout),
    contextlib.redirect_stderr(stderr),
):
    quota.return_value.choose.return_value = (None, {})
    assert tc.cli.cmd_status(SimpleNamespace(json=True)) == 0
assert json.loads(stdout.getvalue())["survey"]["worker_id"] == "test"
assert "Fetching open PRs" in stderr.getvalue() and "Checking quota" in stderr.getvalue()
print("PASS: bounded concurrent survey matches serial decisions, caches and ordering; JSON remains clean")
