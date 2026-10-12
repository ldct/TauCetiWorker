#!/usr/bin/env python3
"""Offline inline/contest batching, nested pagination, routing and real survey tests."""

import json
import subprocess
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Reuse only offline fixtures; do not inherit the discussion-only assertions.
import github_comments_batch as discussion_tests
from github_comments_batch import STAMP, FakeGitHub, board, fields, held, response

import tauceti_worker as tc
from tauceti_worker import github_app
from tauceti_worker import github_review_comments as batch


def comment(cid=1, body="<!--tauceti-rubric:style-->", parent=None):
    return {
        "fullDatabaseId": str(cid),
        "body": body,
        "createdAt": STAMP,
        "replyTo": None if parent is None else {"fullDatabaseId": str(parent)},
    }


def connection(values, *, offset=0, limit=20, edges=False):
    end = min(offset + limit, len(values))
    selected = values[offset:end]
    if edges:
        selected = [{"cursor": str(i + 1), "node": value} for i, value in enumerate(values) if offset <= i < end]
    return {
        "totalCount": len(values),
        "edges" if edges else "nodes": selected,
        "pageInfo": {"hasNextPage": end < len(values), "endCursor": str(end) if selected else None},
    }


class InlineGitHub(FakeGitHub):
    review_comments_batch = tc.GitHub.review_comments_batch

    def __init__(self, count=40):
        super().__init__(count)
        self.inline_requests = []
        self.threads = {}
        self.transform = lambda reply, variables: reply

    def _gh(self, args):
        variables = fields(args)
        query = variables["query"]
        if query not in (*batch.REVIEW_QUERIES.values(), batch.REVIEW_THREAD_QUERY):
            return super()._gh(args)
        assert github_app.public_read(args, [self.repo]), args
        self.inline_requests.append(args)
        continuation = query == batch.REVIEW_THREAD_QUERY
        prs = [int(variables[f"pr{i}"]) for i in range(10) if f"pr{i}" in variables]
        assert query == (batch.REVIEW_THREAD_QUERY if continuation else batch.REVIEW_QUERIES[len(prs)])
        repository = {}
        for i, pr in enumerate(prs):
            threads = []
            for n, comments in enumerate(self.threads.get(pr, [])):
                threads.append(
                    {
                        "id": f"thread-{pr}-{n}",
                        "comments": connection(
                            comments, offset=int(variables.get("commentCursor", "0")), limit=100 if continuation else 20
                        ),
                    }
                )
            repository[f"pr{i}"] = {
                "number": pr,
                "reviewThreads": connection(
                    threads,
                    offset=int(variables.get("threadCursor" if continuation else f"cursor{i}", "0")),
                    limit=1 if continuation else 20,
                    edges=True,
                ),
            }
        reply = self.transform(response(repository), variables)
        self.elapsed += tc.COMMENTS_MEMO_S + 1
        return reply

    def fresh_claim_age(self, cid):
        return None


class InlineBatchTests(unittest.TestCase):
    def setUp(self):
        self.gh = InlineGitHub()

    def test_empty_forty_and_all_batch_sizes(self):
        self.assertEqual(self.gh.review_comments_batch([]), {})
        self.assertEqual(self.gh.inline_requests, [])
        self.assertEqual(self.gh.review_comments_batch(list(range(1, 41))), dict.fromkeys(range(1, 41), []))
        self.assertEqual(len(self.gh.inline_requests), 4)
        for size in range(1, 11):
            self.gh.review_comments_batch(list(range(1, size + 1)))
            self.assertEqual(fields(self.gh.inline_requests[-1])["query"], batch.REVIEW_QUERIES[size])
        self.assertEqual(self.gh.review, [])

    def test_nested_pagination_bigints_and_reply_root_mapping(self):
        root = 2**53 + 111
        # The highest-ID contest is neither the newest timestamp nor last item.
        comments = [comment(root)] + [comment(root + i, f"contest {i}", root + i - 1) for i in range(1, 127)]
        comments[-1]["createdAt"] = "2026-01-01T00:00:00Z"
        comments += [comment(root + 127, "<!--tauceti-reply:style-->", root)]
        # Root is on page one, eligible reply on page three; also >100 threads.
        self.gh.threads[1] = [[comment(i + 1)] for i in range(123)]
        self.gh.threads[1][21] = comments
        result = self.gh.review_comments_batch([1, 2])
        self.assertEqual(result[2], [])
        self.assertEqual(len(result[1]), 250)
        best = tc.ReviewState._newest_contest(result[1])
        self.assertEqual(best, {"id": root + 126, "rubric": "style", "created_at": "2026-01-01T00:00:00Z"})
        normalized = next(c for c in result[1] if c["id"] == root + 125)
        self.assertEqual(normalized["in_reply_to_id"], root)
        self.assertIs(type(normalized["id"]), int)
        continuations = [fields(a) for a in self.gh.inline_requests if fields(a)["query"] == batch.REVIEW_THREAD_QUERY]
        self.assertEqual([c["commentCursor"] for c in continuations], ["20", "120"])
        self.assertTrue(all(c["threadCursor"] == "21" for c in continuations))
        self.assertEqual(len(self.gh.inline_requests), 9)
        self.assertEqual(self.gh.review, [])

    def test_highest_integer_id_not_last_reply_or_lexical_order(self):
        self.gh.threads[1] = [[comment(1), comment(10, "CONTEST", 1), comment(9, "CONTEST", 1)]]
        result = self.gh.review_comments_batch([1])[1]
        self.assertEqual(tc.ReviewState._newest_contest(result)["id"], 10)

    def test_failed_alias_keeps_healthy_paginated_peer_and_later_batch(self):
        self.gh.threads[2] = [[comment(i)] for i in range(1, 24)]

        def transform(reply, variables):
            if variables["pr0"] != "1":
                return reply
            data = json.loads(reply.stdout)
            data["errors"] = [{"path": ["repository", "pr0", "reviewThreads"]}]
            return subprocess.CompletedProcess([], 1, json.dumps(data), "")

        self.gh.transform = transform
        result = self.gh.review_comments_batch(list(range(1, 12)))
        self.assertIsNone(result[1])
        self.assertEqual(len(result[2]), 23)
        self.assertEqual(result[11], [])
        self.assertEqual(len(self.gh.inline_requests), 3)
        continuation = fields(self.gh.inline_requests[1])
        self.assertEqual((continuation["pr0"], continuation["cursor0"]), ("2", "20"))

    def test_first_thread_continuation_omits_predecessor_and_exact_rest_shape(self):
        self.gh.threads[1] = [[comment(2**31)] + [comment(2**31 + i, "contest", 2**31) for i in range(1, 23)]]
        result = self.gh.review_comments_batch([1])[1]
        variables = fields(self.gh.inline_requests[-1])
        self.assertNotIn("threadCursor", variables)
        self.assertEqual(
            result[-1], {"id": 2**31 + 22, "body": "contest", "in_reply_to_id": 2**31, "created_at": STAMP}
        )

    def test_invalid_ids_bodies_dates_and_ancestry_fail_unknown(self):
        bad_comments = []
        for cid in [None, True, False, 0, -1, 1.5, "", "1.0", "1e20", "-1", "01", "bad"]:
            c = comment()
            c["fullDatabaseId"] = cid
            bad_comments.append([c])
        for field, value in [
            ("body", None),
            ("createdAt", None),
            ("createdAt", "yesterday"),
            ("createdAt", "2026-01-01"),
            ("replyTo", {}),
            ("replyTo", {"fullDatabaseId": None}),
        ]:
            c = comment()
            c[field] = value
            bad_comments.append([c])
        bad_comments += [
            [comment(), None],
            [comment(), comment()],
            [comment(), comment(2)],
            [comment(1, parent=2), comment(2, parent=1)],
            [comment(), comment(2, parent=3)],
            [comment(), comment(2, parent=2)],
        ]
        for comments in bad_comments:
            with self.subTest(comments=comments):
                self.gh.threads = {1: [comments]}
                self.assertEqual(self.gh.review_comments_batch([1, 2]), {1: None, 2: []})

    def test_partial_alias_and_thread_errors_do_not_poison_healthy_pr(self):
        for path in [["repository", "pr0"], ["repository", "pr0", "reviewThreads", "edges", 0, "node", "comments"]]:

            def transform(reply, variables, path=path):
                data = json.loads(reply.stdout)
                data["errors"] = [{"path": path}]
                return subprocess.CompletedProcess([], 1, json.dumps(data), "")

            self.gh.transform = transform
            self.assertEqual(self.gh.review_comments_batch([1, 2]), {1: None, 2: []})

    def test_bad_envelopes_transport_and_global_errors(self):
        replies = [
            None,
            [],
            {},
            {"data": None},
            {"data": {"repository": None}},
            {"data": {"repository": {"pr0": None}}},
        ]
        valid = json.loads(response({"pr0": {"number": 1, "reviewThreads": connection([], edges=True)}}).stdout)
        for errors in [None, {}, [None], [{}], [{"path": ["wrong", "pr0"]}], [{"path": ["repository", "other"]}]]:
            replies.append({**valid, "errors": errors})
        for reply in replies:
            with self.subTest(reply=reply):
                with patch.object(
                    self.gh, "_gh", return_value=subprocess.CompletedProcess([], 0, json.dumps(reply), "")
                ):
                    self.assertEqual(self.gh.review_comments_batch([1]), {1: None})
        for error in [OSError(), subprocess.TimeoutExpired("gh", 1)]:
            with patch.object(self.gh, "_gh", side_effect=error):
                self.assertEqual(self.gh.review_comments_batch([1]), {1: None})
        for stdout in ["not json", json.dumps(valid)]:
            with patch.object(self.gh, "_gh", return_value=subprocess.CompletedProcess([], 1, stdout, "")):
                self.assertEqual(self.gh.review_comments_batch([1]), {1: None})

    def test_malformed_connections_and_identity_binding(self):
        self.gh.threads = {1: [[comment()]]}
        mutations = [
            lambda p: p.update(number=2),
            lambda p: p.update(number=True),
            lambda p: p.update(reviewThreads=None),
            lambda p: p["reviewThreads"].update(totalCount=2),
            lambda p: p["reviewThreads"].update(totalCount=True),
            lambda p: p["reviewThreads"]["pageInfo"].update(endCursor=None),
            lambda p: p["reviewThreads"]["pageInfo"].update(hasNextPage="false"),
            lambda p: p["reviewThreads"]["edges"][0].update(cursor="wrong"),
            lambda p: p["reviewThreads"]["edges"][0]["node"].update(id=None),
            lambda p: p["reviewThreads"]["edges"][0]["node"].update(comments=None),
            lambda p: p["reviewThreads"]["edges"][0]["node"]["comments"].update(totalCount=2),
            lambda p: p["reviewThreads"]["edges"][0]["node"]["comments"]["pageInfo"].update(hasNextPage=True),
        ]
        for mutate in mutations:

            def transform(reply, variables, mutate=mutate):
                data = json.loads(reply.stdout)
                mutate(data["data"]["repository"]["pr0"])
                return subprocess.CompletedProcess([], 0, json.dumps(data), "")

            self.gh.transform = transform
            self.assertEqual(self.gh.review_comments_batch([1, 2]), {1: None, 2: []})

    def test_continuation_failures_discard_entire_pr(self):
        self.gh.threads = {1: [[comment()] + [comment(i, "contest", 1) for i in range(2, 125)]]}
        for kind in ["error", "thread", "cursor", "count", "comment_count", "repeat"]:

            def transform(reply, variables, kind=kind):
                if variables["query"] != batch.REVIEW_THREAD_QUERY:
                    return reply
                data = json.loads(reply.stdout)
                threads = data["data"]["repository"]["pr0"]["reviewThreads"]
                edge = threads["edges"][0]
                if kind == "error":
                    data["errors"] = [{"path": ["repository", "pr0", "reviewThreads"]}]
                if kind == "thread":
                    edge["node"]["id"] = "foreign-thread"
                if kind == "cursor":
                    edge["cursor"] = threads["pageInfo"]["endCursor"] = "foreign-cursor"
                if kind == "count":
                    threads["totalCount"] += 1
                if kind == "comment_count":
                    edge["node"]["comments"]["totalCount"] += 1
                if kind == "repeat":
                    edge["node"]["comments"]["pageInfo"]["endCursor"] = "20"
                return subprocess.CompletedProcess([], 0, json.dumps(data), "")

            self.gh.transform = transform
            self.assertEqual(self.gh.review_comments_batch([1, 2]), {1: None, 2: []}, kind)

    def test_thread_loops_count_changes_duplicate_comments_and_page_caps(self):
        self.gh.threads = {1: [[comment(i)] for i in range(1, 42)]}
        for kind in ["repeat_thread", "repeat_cursor", "changed_count", "duplicate_comment"]:

            def transform(reply, variables, kind=kind):
                if "cursor0" not in variables:
                    return reply
                data = json.loads(reply.stdout)
                threads = data["data"]["repository"]["pr0"]["reviewThreads"]
                if kind == "repeat_thread":
                    threads["edges"][0]["node"]["id"] = "thread-1-0"
                if kind == "repeat_cursor":
                    threads["edges"][0]["cursor"] = "1"
                if kind == "changed_count":
                    threads["totalCount"] += 1
                if kind == "duplicate_comment":
                    threads["edges"][0]["node"]["comments"]["nodes"][0]["fullDatabaseId"] = "1"
                return subprocess.CompletedProcess([], 0, json.dumps(data), "")

            self.gh.transform = transform
            self.assertEqual(self.gh.review_comments_batch([1, 2]), {1: None, 2: []})
        self.gh.transform = lambda reply, variables: reply
        with patch.object(batch, "REVIEW_MAX_PAGES", 2):
            self.assertEqual(self.gh.review_comments_batch([1, 2]), {1: None, 2: []})
            self.gh.threads = {1: [[comment()] + [comment(i, "contest", 1) for i in range(2, 125)]]}
            self.assertEqual(self.gh.review_comments_batch([1, 2]), {1: None, 2: []})

    def test_routing_exact_documents_and_repository_only(self):
        self.gh.threads = {1: [[comment()] + [comment(i, "contest", 1) for i in range(2, 23)]]}
        self.gh.review_comments_batch(list(range(1, 11)))
        for args in self.gh.inline_requests:
            self.assertTrue(github_app.public_read(args, [self.gh.repo]))
            self.assertFalse(github_app.public_read(args, ["private/repo"]))
            for extra in [["-f", "id=private-node"], ["-f", "owner=private"], ["-f", "unknown=1"]]:
                self.assertFalse(github_app.public_read(args + extra, [self.gh.repo]))
            variables = fields(args)
            for key, replacement in [
                ("query", variables["query"] + " "),
                ("pr0", "0"),
                ("pr0", "-1"),
                ("owner", "private"),
            ]:
                changed = [f"{key}={replacement}" if a.startswith(f"{key}=") else a for a in args]
                self.assertFalse(github_app.public_read(changed, [self.gh.repo]))
            self.assertNotIn("node(", variables["query"])
            self.assertNotIn("nodes(", variables["query"])
            self.assertNotIn("databaseId", variables["query"])
        # Cursors remain strings scoped to repository.pullRequest.reviewThreads,
        # never IDs passed to a global node query, even with hostile input.
        args = self.gh.inline_requests[-1] + ["-f", "threadCursor=private-node { viewer { login } }"]
        self.assertTrue(github_app.public_read(args, [self.gh.repo]))


class InlineSurveyTests(unittest.TestCase):
    setUp = discussion_tests.SurveyTests.setUp
    survey = discussion_tests.SurveyTests.survey

    def prepare(self):
        self.gh = InlineGitHub()
        self.gh.comments = {n: [board(f"head{n}")] for n in range(1, 41)}
        self.rs = tc.ReviewState(self.cfg, self.gh)

    def test_forty_cold_reviewed_and_warm_changed_shallow(self):
        self.prepare()
        self.gh.threads = {
            n: [[comment(n * 10), comment(n * 10 + 1, "<!--tauceti-reply:style-->", n * 10)]] for n in range(1, 41)
        }
        start = time.time()
        with patch.object(tc.review_state.time, "time", side_effect=lambda: start + self.gh.elapsed):
            sv = self.survey()
            self.assertEqual(sv.reviewable.actionable, [])
            self.assertEqual(len(self.gh.requests), 4)
            self.assertEqual(len(self.gh.inline_requests), 4)
            self.assertEqual(self.gh.rest, [])
            self.assertEqual(self.gh.review, [])
            self.assertEqual(self.rs._survey_review_comments, {})
            self.survey()
            self.assertEqual(len(self.gh.inline_requests), 4)
            self.gh.clocks[2] = "2026-09-27T00:00:00Z"
            self.survey()
            self.assertEqual(len(self.gh.inline_requests), 5)
            self.assertEqual(fields(self.gh.inline_requests[-1])["pr0"], "2")
            self.rs.bust(3)
            self.survey(deep=False)
            self.assertEqual(len(self.gh.inline_requests), 5)

    def test_only_current_reviewed_stale_contests_and_parity(self):
        self.prepare()
        self.gh.comments[1] = []
        self.gh.comments[2] = [board("old")]
        self.gh.comments[3] = [held("head3")]
        self.gh.comments[4] = [board("head4")]
        self.gh.comments[4][0]["body"] = self.gh.comments[4][0]["body"].replace('"approve"', '"error"')
        self.rs.observe([SimpleNamespace(number=5, updated_at=STAMP)])
        self.rs._write_sidecar(self.rs._contest_path(5), 5, {"contest": None})
        cid = 2**53 + 1
        self.gh.threads[6] = [[comment(cid), comment(cid + 1, "CONTEST", cid)]]
        sv = self.survey()
        prefetched = [int(v) for a in self.gh.inline_requests for k, v in fields(a).items() if k.startswith("pr")]
        self.assertEqual(prefetched, list(range(6, 41)))
        # Upstream no longer reads in-flight comment markers for selection: a held head is actionable.
        self.assertEqual([c.pr for c in sv.reviewable.actionable], [1, 2, 3, 4, 6])
        self.assertEqual(sv.reviewable.actionable[-1].contest_reply_id, cid + 1)
        self.assertEqual(self.gh.review, [])

    def test_failed_snapshot_never_writes_false_absence_and_healthy_peer_caches(self):
        self.prepare()
        self.gh.count = 2
        self.rs.observe([SimpleNamespace(number=1, updated_at="old")])
        self.rs._write_sidecar(self.rs._contest_path(1), 1, {"contest": {"id": 20}})
        previous = self.rs._contest_path(1).read_text()
        with patch.object(self.gh, "review_comments_batch", return_value={1: None, 2: []}):
            self.survey()
        self.assertEqual(self.rs._contest_path(1).read_text(), previous)
        self.assertIsNone(json.loads(self.rs._contest_path(2).read_text())["contest"])
        self.assertEqual(self.gh.review, [])

    def test_force_bust_observe_and_exception_cleanup(self):
        self.prepare()
        self.rs.observe([SimpleNamespace(number=1, updated_at=STAMP)])
        self.gh.threads[1] = [[comment(), comment(2, "CONTEST", 1)]]
        with self.rs.survey_comments([1], heads={1: "head1"}):
            self.assertIn(1, self.rs._survey_review_comments)
            self.assertFalse(self.rs._cache_path(1).exists(), "selection must not write scoreboards")
            self.assertFalse(self.rs._contest_path(1).exists(), "prefetch must not write contests")
            self.assertIsNone(self.rs.newest_contest_reply(1, force=True))
            self.assertNotIn(1, self.rs._survey_review_comments)
        self.assertEqual(self.gh.review, [1])
        self.rs.bust(1)
        with self.rs.survey_comments([1], heads={1: "head1"}):
            self.rs.bust(1)
            self.assertEqual(self.rs._survey_review_comments, {})
            self.rs.newest_contest_reply(1)
        self.assertEqual(self.gh.review, [1, 1])
        self.rs.bust(1)
        with self.assertRaisesRegex(RuntimeError, "stop"), self.rs.survey_comments([1], heads={1: "head1"}):
            raise RuntimeError("stop")
        self.assertEqual(self.rs._survey_review_comments, {})
        self.rs._survey_review_comments[1] = []
        self.rs.observe([])
        self.assertEqual(self.rs._survey_review_comments, {})

    def test_author_phase_does_not_reuse_review_snapshot_or_extra_writes(self):
        self.prepare()
        self.gh.count = 1
        prs = self.gh.open_prs()
        prs[0]["author"]["login"] = "me"
        self.gh.comments[1][0]["body"] = self.gh.comments[1][0]["body"].replace('"approve"', '"request_changes"')
        # Failure in review phase cannot create a "no contest" sidecar; author
        # phase still performs its existing fresh REST read, outside the scope.
        with (
            patch.object(self.gh, "open_prs", return_value=prs),
            patch.object(self.gh, "review_comments_batch", return_value={1: None}),
        ):
            sv = self.survey()
        self.assertEqual(self.gh.review, [1])
        self.assertEqual([c.pr for c in sv.needs_fix.actionable], [1])


if __name__ == "__main__":
    unittest.main()
