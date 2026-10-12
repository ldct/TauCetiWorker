#!/usr/bin/env python3
"""Offline batching, pagination, failure isolation, auth routing and real survey integration."""

import importlib
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tauceti_worker as tc
from tauceti_worker import github_app
from tauceti_worker import github_comments as batch

survey_module = importlib.import_module("tauceti_worker.survey")
STAMP = "2026-09-26T00:00:00Z"


def comment(body="hello", updated=STAMP):
    return {"body": body, "updatedAt": updated}


def board(head="head1", updated=STAMP):
    meta = {"head_sha": head, "runs": [{"verdict": "approve"}]}
    return comment("<!--tauceti-scoreboard--> <!--tauceti-meta:v1 " + json.dumps(meta) + "-->", updated)


def held(head):
    marker = {"head": head, "providers": ["codex"], "expires_at": int(time.time()) + 3600}
    return comment("<!--tauceti-review-in-progress " + json.dumps(marker) + "-->")


def node(pr, comments=None, *, total=None, more=False, cursor="end"):
    comments = [comment()] if comments is None else comments
    return {
        "number": pr,
        "comments": {
            "totalCount": len(comments) if total is None else total,
            "nodes": comments,
            "pageInfo": {"hasNextPage": more, "endCursor": cursor if comments else None},
        },
    }


def response(repository, *, errors=None, rc=0):
    data = {"data": {"repository": repository}}
    if errors is not None:
        data["errors"] = errors
    return subprocess.CompletedProcess([], rc, json.dumps(data), "")


def fields(args):
    return dict(a.split("=", 1) for a in args if "=" in a and not a.startswith("-"))


class FakeGitHub(tc.GitHub):
    """Real batch method, with a transport that refuses anything except trusted reads."""

    def __init__(self, count=40):
        super().__init__()
        self.count = count
        self.requests = []
        self.clocks = {}
        self.comments = {}
        self.rest = []
        self.review = []
        self.elapsed = 0

    def _gh(self, args):
        self.requests.append(args)
        assert github_app.public_read(args, [self.repo]), args
        variables = fields(args)
        prs = [int(variables[f"pr{i}"]) for i in range(10) if f"pr{i}" in variables]
        assert 1 <= len(prs) <= 10
        assert variables["query"] == batch.COMMENTS_QUERIES[len(prs)]
        result = {}
        for i, pr in enumerate(prs):
            comments = self.comments.get(pr, [])
            offset = int(variables.get(f"cursor{i}", "0"))
            end = min(offset + 100, len(comments))
            result[f"pr{i}"] = node(
                pr, comments[offset:end], total=len(comments), more=end < len(comments), cursor=str(end)
            )
        self.elapsed += tc.COMMENTS_MEMO_S + 1  # a slow prefetch, without sleeping
        return response(result)

    def issue_comments(self, pr):
        self.rest.append(pr)
        return [{"body": c["body"], "updated_at": c["updatedAt"]} for c in self.comments.get(pr, [])]

    def review_comments(self, pr):
        self.review.append(pr)
        return []

    def open_prs(self):
        return [
            {
                "number": n,
                "headRefOid": f"head{n}",
                "updatedAt": self.clocks.get(n, STAMP),
                "author": {"login": "other"},
                "statusCheckRollup": [{"context": "build", "state": "SUCCESS" if n != self.count + 1 else "FAILURE"}],
                "isDraft": n == self.count + 2,
            }
            for n in range(1, self.count + 3)
        ]


class BatchTests(unittest.TestCase):
    def fetch(self, responses, numbers=(1, 2)):
        calls = []
        replies = iter(responses)

        def gh(args):
            calls.append(args)
            return next(replies)

        return batch.fetch_issue_comments(gh, "TauCetiProject/TauCeti", list(numbers)), calls

    def test_forty_cold_and_empty(self):
        gh = FakeGitHub()
        self.assertEqual(gh.issue_comments_batch([]), {})
        self.assertEqual(gh.requests, [])
        self.assertEqual(gh.issue_comments_batch(list(range(1, 41))), dict.fromkeys(range(1, 41), []))
        self.assertEqual(len(gh.requests), 4)
        self.assertEqual(gh.rest, [])

    def test_all_document_sizes_and_routing(self):
        for size in range(1, 11):
            gh = FakeGitHub()
            gh.issue_comments_batch(list(range(1, size + 1)))
            args = gh.requests[0]
            self.assertTrue(github_app.public_read(args, [gh.repo]))
            self.assertTrue(github_app.public_read([*args, "-f", "cursor0=opaque+/=cursor"], [gh.repo]))
            self.assertFalse(github_app.public_read(args, ["private/repo"]))
            for bad in [
                [*args, "-F", "extra=1"],
                [*args, "-f", "cursor0=@/tmp/secret"],
                [*args, "-X", "POST"],
                [a.replace("owner=TauCetiProject", "owner=private") for a in args],
                [a.replace("pr0=1", "pr0=-1") for a in args],
                [a.replace("pr0=1", "pr0=not-a-number") for a in args],
                [a.replace("query=query", "query=mutation") for a in args],
                [a + " " if a.startswith("query=") else a for a in args],
                ["api", "graphql", "-f", "query=query {viewer{login}}"],
            ]:
                self.assertFalse(github_app.public_read(bad, [gh.repo]), bad)

    def test_independent_pagination_and_rest_projection(self):
        gh = FakeGitHub()
        gh.comments = {1: [comment(str(n)) for n in range(201)], 2: [comment("peer")]}
        result = gh.issue_comments_batch([1, 2])
        self.assertEqual(result[1], [{"body": str(n), "updated_at": STAMP} for n in range(201)])
        self.assertEqual(result[2], [{"body": "peer", "updated_at": STAMP}])
        self.assertEqual(len(gh.requests), 3)
        self.assertEqual(fields(gh.requests[1])["cursor0"], "100")
        self.assertNotIn("pr1", fields(gh.requests[1]))
        self.assertEqual(fields(gh.requests[2])["cursor0"], "200")

    def test_alias_renumbering_and_later_failure_keeps_completed_peer(self):
        gh = FakeGitHub()
        gh.comments = {1: [comment("done")], 2: [comment(str(n)) for n in range(101)]}
        result = gh.issue_comments_batch([1, 2])
        self.assertEqual(len(result[2]), 101)
        self.assertEqual(fields(gh.requests[1])["pr0"], "2")
        self.assertEqual(fields(gh.requests[1])["cursor0"], "100")
        result, calls = self.fetch(
            [
                response({"pr0": node(1, []), "pr1": node(2, total=2, more=True, cursor="one")}),
                response(None),
            ]
        )
        self.assertEqual(result, {1: [], 2: None})
        self.assertEqual(fields(calls[1])["pr0"], "2")

    def test_partial_error_keeps_good_alias_but_discards_all_bad_pages(self):
        first = response(
            {"pr0": node(1, total=2, more=True, cursor="one"), "pr1": node(2, total=2, more=True, cursor="one")}
        )
        for rc in (0, 1):
            last = response(
                {"pr0": node(1, total=2, cursor="two"), "pr1": node(2, total=2, cursor="two")},
                errors=[{"message": "failed", "path": ["repository", "pr0", "comments", "nodes", 0, "body"]}],
                rc=rc,
            )
            result, _ = self.fetch([first, last])
            self.assertIsNone(result[1])
            self.assertEqual(len(result[2]), 2)

    def test_global_errors_and_nonzero_without_errors_fail_closed(self):
        for errors in (
            [{"message": "no path"}],
            [{"path": ["repository"]}],
            [{"path": ["repository", "unknown"]}],
            [None],
            {},
            "error",
        ):
            result, _ = self.fetch([response({"pr0": node(1), "pr1": node(2)}, errors=errors)])
            self.assertEqual(result, {1: None, 2: None})
        result, _ = self.fetch([response({"pr0": node(1), "pr1": node(2)}, rc=1)])
        self.assertEqual(result, {1: None, 2: None})

    def test_malformed_alias_never_masks_successful_peer(self):
        variants = [None, [], {}, {"number": True}, {**node(1), "number": 99}]
        for key, values in {
            "totalCount": [None, -1, True, "1", 2],
            "nodes": [
                None,
                {},
                [None],
                [comment(None)],
                [{"body": "missing date"}],
                [comment(updated="bad")],
                [comment(updated="2026-09-26")],
                [comment()] * 101,
            ],
            "pageInfo": [
                None,
                {},
                {"hasNextPage": "false", "endCursor": "end"},
                {"hasNextPage": False, "endCursor": 10},
                {"hasNextPage": True, "endCursor": None},
                {"hasNextPage": False, "endCursor": None},
            ],
        }.items():
            for value in values:
                bad = node(1)
                bad["comments"][key] = value
                variants.append(bad)
        variants += [node(1, [], total=1, more=True), {**node(1), "comments": None}]
        for bad in variants:
            with self.subTest(bad=bad):
                result, _ = self.fetch([response({"pr0": bad, "pr1": node(2, [])})])
                self.assertEqual(result, {1: None, 2: []})
        result, _ = self.fetch([response({"pr1": node(2, [])})])
        self.assertEqual(result, {1: None, 2: []})

    def test_bad_envelopes_and_transport_failure_leave_later_batches_usable(self):
        bads = [
            "",
            "not json",
            "null",
            "[]",
            "{}",
            '{"data":null}',
            '{"data":{"repository":null}}',
            '{"data":{"repository":[]}}',
        ]
        for text in bads:
            result, _ = self.fetch([subprocess.CompletedProcess([], 0, text, "")], [1])
            self.assertEqual(result, {1: None})
        gh = FakeGitHub()
        real = gh._gh
        with patch.object(
            gh,
            "_gh",
            side_effect=[
                OSError("offline"),
                real(
                    [
                        "api",
                        "graphql",
                        "-f",
                        f"query={batch.COMMENTS_QUERIES[1]}",
                        "-f",
                        "owner=TauCetiProject",
                        "-f",
                        "name=TauCeti",
                        "-F",
                        "pr0=11",
                    ]
                ),
            ],
        ):
            result = gh.issue_comments_batch(list(range(1, 12)))
        self.assertEqual(result, {**dict.fromkeys(range(1, 11)), 11: []})

    def test_repeated_cursor_final_cursor_and_count_changes(self):
        for last in (
            node(1, total=3, more=True, cursor="one"),
            node(1, total=2, cursor="one"),
            node(1, total=4, more=True, cursor="two"),
        ):
            total = 2 if not last["comments"]["pageInfo"]["hasNextPage"] else 3
            result, calls = self.fetch(
                [
                    response({"pr0": node(1, total=total, more=True, cursor="one")}),
                    response({"pr0": last}),
                ],
                [1],
            )
            self.assertEqual(result, {1: None})
            self.assertEqual(len(calls), 2)
        result, calls = self.fetch(
            [response({"pr0": node(1, total=5, more=True, cursor=cursor)}) for cursor in ("one", "two", "one")], [1]
        )
        self.assertEqual(result, {1: None})
        self.assertEqual(len(calls), 3)

    def test_page_cap_discards_partial_history(self):
        gh = FakeGitHub()
        gh.comments = {1: [comment()] * 201, 2: []}
        with patch.object(batch, "COMMENTS_MAX_PAGES", 2):
            result = gh.issue_comments_batch([1, 2])
        self.assertEqual(result, {1: None, 2: []})
        self.assertEqual(len(gh.requests), 2)


class SurveyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.cfg = SimpleNamespace(wid="test", sbcache=root / "sb", state=root, store_dir=root)
        self.gh = FakeGitHub()
        self.rs = tc.ReviewState(self.cfg, self.gh)
        self.counters = tc.Counters(self.cfg)

    def survey(self, **kwargs):
        with (
            patch.object(survey_module, "me", return_value="me"),
            patch.object(survey_module, "progress_due", return_value=(True, "due")),
        ):
            return tc.survey(self.cfg, self.gh, self.rs, self.counters, **kwargs)

    def test_real_survey_cold_mixed_hits_changed_and_shallow(self):
        self.gh.comments = {2: [held("head2")], 3: [board("head3")]}
        start = time.time()
        with patch.object(tc.review_state.time, "time", side_effect=lambda: start + self.gh.elapsed):
            sv = self.survey()
            self.assertEqual(len(self.gh.requests), 4)
            self.assertEqual(self.gh.rest, [], "long prefetch must not expire into REST re-reads")
            self.assertEqual([c.pr for c in sv.reviewable.actionable], [1, *range(4, 41)])
            self.assertEqual(sv.review_inflight, [(2, "codex")])
            self.assertEqual(self.gh.review, [3])
            self.assertEqual(self.rs._survey_comments, {})
            self.survey()
            self.assertEqual(len(self.gh.requests), 4, "unchanged PRs are free")
            # One changed PR and one cold PR, mixed with 38 entitled hits.
            self.gh.clocks[1] = "2026-09-26T00:01:00Z"
            self.gh.comments[1] = [held("head1")]
            self.rs.bust(4)
            sv = self.survey()
            self.assertEqual(len(self.gh.requests), 5)
            variables = fields(self.gh.requests[-1])
            self.assertEqual([variables["pr0"], variables["pr1"]], ["1", "4"])
            self.assertEqual([pr for pr, _ in sv.review_inflight], [1, 2])
            self.assertEqual(self.gh.rest, [])
            self.gh.clocks[5] = "2026-09-26T00:01:00Z"
            self.survey(deep=False)
            self.assertEqual(len(self.gh.requests), 5)

    def test_late_scoreboard_and_marker_are_not_lost(self):
        self.gh.count = 2
        self.gh.comments = {
            1: [board("old", "2026-09-25T00:00:00Z")] + [comment()] * 99 + [board("head1")],
            2: [comment()] * 100 + [held("head2")],
        }
        sv = self.survey()
        self.assertEqual(sv.reviewable.actionable, [])
        self.assertEqual(sv.review_inflight, [(2, "codex")])
        self.assertEqual(self.rs.gh_meta(1).data["head_sha"], "head1")
        self.assertEqual(len(self.gh.requests), 2)
        self.assertEqual(self.gh.rest, [])

    def test_forced_reads_bust_and_scope_cleanup(self):
        self.rs.observe([SimpleNamespace(number=1, updated_at=STAMP)])
        self.gh.comments[1] = [board("old"), held("old")]
        with self.rs.survey_comments([1]):
            self.gh.comments[1] = [board("new")]
            self.assertEqual(self.rs.gh_meta(1, force=True).data["head_sha"], "new")
            self.assertEqual(self.gh.rest, [1])
            self.assertEqual(self.rs._survey_comments, {})
            self.assertEqual(self.rs.inflight_review(1, "old", force=True), set())
            self.assertEqual(self.gh.rest, [1, 1])
        self.rs.bust(1)
        with self.rs.survey_comments([1]):
            self.rs.bust(1)
            self.assertEqual(self.rs._survey_comments, {})
        with self.assertRaisesRegex(RuntimeError, "task failed"), self.rs.survey_comments([1]):
            raise RuntimeError("task failed")
        self.assertEqual(self.rs._survey_comments, {})
        self.assertEqual(self.rs._comments, {})

    def test_failed_prefetch_preserves_stale_cache_and_does_not_retry_rest(self):
        self.gh.comments[1] = [board("old")]
        self.rs.observe([SimpleNamespace(number=1, updated_at=STAMP)])
        with self.rs.survey_comments([1]):
            self.assertEqual(self.rs.gh_meta(1).provenance, "fresh")
        previous = (self.cfg.sbcache / "1.key.json").read_text()
        self.rs.observe([SimpleNamespace(number=1, updated_at="2026-09-26T00:01:00Z")])
        with (
            patch.object(self.gh, "issue_comments_batch", return_value={1: None, 2: None}),
            self.rs.survey_comments([1, 2]),
        ):
            self.assertEqual(self.rs.gh_meta(1).provenance, "stale")
            self.assertEqual(self.rs.gh_meta(1).data["head_sha"], "old")
            self.assertEqual(self.rs.gh_meta(2).provenance, "fetch_failed")
            self.assertEqual(self.rs.inflight_review(2, "head2"), set())
            self.assertEqual(self.gh.rest, [])
        self.assertEqual((self.cfg.sbcache / "1.key.json").read_text(), previous)
        self.assertFalse((self.cfg.sbcache / "2.key.json").exists())

    def test_forced_marker_bypasses_prefetch_and_failed_prefetch_is_not_sticky(self):
        self.gh.comments[1] = [held("head1")]
        with self.rs.survey_comments([1]):
            self.gh.comments[1] = []
            self.assertEqual(self.rs.inflight_review(1, "head1", force=True), set())
            self.assertEqual(self.rs._survey_comments, {})
        self.assertEqual(self.gh.rest, [1])
        self.rs.bust(1)
        with patch.object(self.gh, "issue_comments_batch", return_value={1: None}):
            with self.rs.survey_comments([1]):
                self.gh.comments[1] = [board("new")]
                self.assertEqual(self.rs.gh_meta(1, force=True).data["head_sha"], "new")
        self.assertEqual(self.gh.rest, [1, 1])

    def test_task_exception_cleans_scoped_prefetch(self):
        with patch.object(survey_module, "_survey_review", side_effect=RuntimeError("task failed")):
            with self.assertRaisesRegex(RuntimeError, "task failed"):
                self.survey()
        self.assertEqual(self.rs._survey_comments, {})


if __name__ == "__main__":
    unittest.main()
