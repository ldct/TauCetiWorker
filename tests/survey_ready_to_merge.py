#!/usr/bin/env python3
"""Ready-to-merge PRs stay visible but never enter task reads or GraphQL prefetch."""

import contextlib
import importlib
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tauceti_worker as tc

survey_module = importlib.import_module("tauceti_worker.survey")


def row(number, labels, *, mine=False, bot=False, green=True):
    return {
        "number": number,
        "labels": [{"name": label} for label in labels],
        "author": {"login": "me" if mine else "other", "__typename": "Bot" if bot else "User"},
        "headRepositoryOwner": {"login": survey_module.TAUCETI_OWNER},
        "headRefOid": f"new-head-{number}",
        "headRefName": "bump-mathlib/test",
        "mergeable": "CONFLICTING",
        "statusCheckRollup": [{"context": "build", "state": "SUCCESS" if green else "FAILURE"}],
    }


for deep in (False, True):
    for only_ready in (False, True):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = SimpleNamespace(wid="test", state=Path(tmp), store_dir=Path(tmp))
            rows = [
                row(1, ["ready-to-merge", "awaiting-review"], mine=True),
                row(2, ["READY-TO-MERGE"], bot=True),
                row(3, ["ready-to-merge", "needs-rebase"], mine=True, green=False),
            ]
            if not only_ready:
                rows.append(row(4, ["awaiting-review"]))
            gh = Mock()
            gh.open_prs.return_value = rows
            rs = Mock()
            batches = []

            @contextlib.contextmanager
            def comments(numbers, *, heads, batches=batches):
                batches.append((numbers, heads))
                yield

            rs.survey_comments.side_effect = comments

            def review(p, *_, cfg=cfg):
                assert p.number == 4, "ready PR reached a per-PR review check"
                result = survey_module.Survey(worker_id=cfg.wid)
                result.reviewable.actionable.append(survey_module.Candidate(p.number, p.head_oid, "test"))
                return result

            with (
                patch.object(survey_module, "me", return_value="me"),
                patch.object(survey_module, "can_push", side_effect=AssertionError("excluded bot permission read")),
                patch.object(survey_module, "progress_due", return_value=(False, "not due")),
                patch.object(survey_module, "_survey_review", side_effect=review),
            ):
                sv = tc.survey(cfg, gh, rs, tc.Counters(cfg), deep=deep)
            wanted = [] if only_ready else [4]
            assert [c.pr for c in sv.reviewable.actionable] == wanted
            assert sv.n_reviewable == len(wanted)
            assert [p.number for p in sv.open_prs] == [r["number"] for r in rows]
            assert sv.n_open_nondraft == len(rows)
            assert [p.number for p in sv._mine_open_prs] == [1, 3], "preserve open-PR backpressure"
            assert ("ready-to-merge", 2, 2) in sv.status_labels
            assert batches == ([(wanted, {n: f"new-head-{n}" for n in wanted})] if deep else [])
            assert gh.method_calls == [("open_prs", (), {})], "unexpected per-PR REST read"
            assert all(c[0] in ("seed_survey", "observe", "survey_comments") for c in rs.method_calls), rs.method_calls
            for stage in ("rebase", "fix", "fix-ci", "bump", "lint-repair"):
                assert not sv.kind(stage).actionable and not sv.kind(stage).suppressed

print("Ready-to-merge survey exclusion: PASS")
