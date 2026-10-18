#!/usr/bin/env python3
"""Post-review REST muting: bounded personal lookup, confirmed writes, no write retries.

All GitHub requests and review engine calls are mocked.
"""

import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0


def check(name, got, want=True):
    global fails
    ok = got == want
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}: got {got!r} want {want!r}")


def cp(rc=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["gh"], rc, stdout, stderr)


LOOKUP = ["api", "--method", "GET", "/repos/TauCetiProject/TauCeti/notifications?all=true&per_page=100&page=1"]
MUTATE = ["gh", "api", "--method", "PUT", "/notifications/threads/123/subscription", "-F", "ignored=true"]
THREAD = {
    "id": "123",
    "subject": {"type": "PullRequest", "url": "https://api.github.com/repos/TauCetiProject/TauCeti/pulls/42"},
}


def listing(threads=None):
    return cp(stdout=json.dumps([THREAD] if threads is None else threads))


def test_notification_api():
    gh = tc.GitHub("TauCetiProject/TauCeti")
    with (
        patch.object(gh, "_gh", return_value=listing()) as lookup,
        patch.object(tc.github, "run", return_value=cp(stdout='{"ignored":true}')) as mutate,
        patch.object(tc.github, "log") as log,
    ):
        check("confirmed REST mute", gh.ignore_pr_notifications(42))
        check("bounded repository personal lookup", lookup.call_args.args[0], LOOKUP)
        check("stop after first match", lookup.call_count, 1)
        check("exact REST mutation", mutate.call_args.args[0], MUTATE)
        check("one write", mutate.call_count, 1)
        check("success logged", "disabled notifications" in log.call_args.args[0])
    from tauceti_worker.github_app import public_read

    with patch.dict(os.environ, {"GH_HOST": "github.com"}):
        check("notification read stays personal", public_read(LOOKUP, [gh.repo]), False)
        check("notification write stays personal", public_read(MUTATE[1:], [gh.repo]), False)
    check("write is not retryable as read", tc.github._gh_read_only(MUTATE), False)


def test_lookup_failures():
    gh = tc.GitHub("TauCetiProject/TauCeti")
    cases = [cp(1, stderr="HTTP 403"), cp(stdout="bad"), cp(), cp(stdout="null"), cp(stdout="{}"), listing([])]
    for id in (None, True, "../bad", "@file", "", "١٢٣"):
        cases.append(listing([{**THREAD, "id": id}]))
    for response in cases:
        with (
            patch.object(gh, "_gh", return_value=response),
            patch.object(tc.github, "run") as mutate,
            patch.object(tc.github, "log"),
        ):
            check("lookup failure is nonfatal", gh.ignore_pr_notifications(42), False)
            check("no write on bad lookup", mutate.call_count, 0)
    with (
        patch.object(gh, "_gh", side_effect=OSError()),
        patch.object(tc.github, "run") as mutate,
        patch.object(tc.github, "log"),
    ):
        check("lookup exception is nonfatal", gh.ignore_pr_notifications(42), False)
        check("lookup exception never writes", mutate.call_count, 0)


def test_pagination():
    gh = tc.GitHub("TauCetiProject/TauCeti")
    unrelated = [
        None,
        {},
        {**THREAD, "subject": {**THREAD["subject"], "type": "Issue"}},
        {**THREAD, "subject": {**THREAD["subject"], "url": "https://api.github.com/repos/other/repo/pulls/42"}},
    ] * 25
    for pages, expected, writes in (
        ([listing(unrelated), listing()], True, 1),
        ([listing(unrelated)] * 3, False, 0),
        ([listing([])], False, 0),
    ):
        with (
            patch.object(gh, "_gh", side_effect=pages) as lookup,
            patch.object(tc.github, "run", return_value=cp(stdout='{"ignored":true}')) as mutate,
            patch.object(tc.github, "log"),
        ):
            check("pagination result", gh.ignore_pr_notifications(42), expected)
            check("bounded page count", lookup.call_count, len(pages))
            check("expected writes", mutate.call_count, writes)
            for page, call in enumerate(lookup.call_args_list, 1):
                check("explicit page", call.args[0][-1].endswith(f"page={page}"))


def test_mutation_responses():
    gh = tc.GitHub("TauCetiProject/TauCeti")
    for stdout in (
        "",
        "bad",
        "null",
        "[]",
        "{}",
        '{"ignored":false}',
        '{"ignored":"true"}',
        '{"ignored":1}',
        '{"subscribed":false}',
    ):
        with (
            patch.object(gh, "_gh", return_value=listing()),
            patch.object(tc.github, "run", return_value=cp(stdout=stdout)) as mutate,
            patch.object(tc.github, "log") as log,
        ):
            check("unconfirmed mute fails", gh.ignore_pr_notifications(42), False)
            check("never replay write", mutate.call_count, 1)
            check("review remains successful", "review remains successful" in log.call_args.args[0])
    with (
        patch.object(gh, "_gh", return_value=listing()),
        patch.object(tc.github, "run", side_effect=OSError()) as mutate,
        patch.object(tc.github, "log"),
    ):
        check("write exception is nonfatal", gh.ignore_pr_notifications(42), False)
        check("exception write not replayed", mutate.call_count, 1)


def test_mute_not_retried_on_transient():
    gh = tc.GitHub("TauCetiProject/TauCeti")
    for error in (
        "HTTP 504",
        "HTTP 403 secondary rate limit Retry-After: 1",
        "HTTP 403 API rate limit exceeded",
        "HTTP 429",
    ):
        with (
            patch.object(tc.github, "run", side_effect=[listing(), cp(1, stderr=error)]) as run,
            patch.object(tc.github.time, "sleep") as sleep,
            patch.object(tc.github, "log"),
        ):
            check("write failure is nonfatal", gh.ignore_pr_notifications(42), False)
            check("one read one write", run.call_count, 2)
            check("no retry sleep", sleep.call_count, 0)
            check("exact write", run.call_args.args[0], MUTATE)


def test_failure_diagnostics():
    gh = tc.GitHub("TauCetiProject/TauCeti")
    secret = "private-test-token"
    with (
        patch.dict(os.environ, {"GH_TOKEN": secret}),
        patch.object(gh, "_gh", return_value=listing()),
        patch.object(tc.github, "run", return_value=cp(1, stderr=f"HTTP 403 {secret} " + "x" * 5000)),
        patch.object(tc.github, "log") as log,
    ):
        check("diagnostic failure is nonfatal", gh.ignore_pr_notifications(42), False)
        message = log.call_args.args[0]
        check("diagnostic redacts credentials", secret in message, False)
        check("diagnostic is bounded", len(message) < 2200)


class Counters:
    def __init__(self):
        self.values = {}

    def read(self, name):
        return self.values.get(name, 0)

    def write(self, name, value):
        self.values[name] = value

    def incr(self, name):
        self.write(name, self.read(name) + 1)
        return self.read(name)


class ReviewState:
    def __init__(self):
        self.busted = []

    def review_rounds(self, pr, counters):
        return 0

    def bust(self, pr):
        self.busted.append(pr)


class ReviewGitHub:
    def __init__(self, raises=False):
        self.muted = []
        self.raises = raises

    def ignore_pr_notifications(self, pr):
        self.muted.append(pr)
        if self.raises:
            raise RuntimeError("local notification problem")


def worker(state, raises=False):
    return types.SimpleNamespace(
        cfg=types.SimpleNamespace(state=state, logdir=state, wid="test", store_dir=state / "store"),
        counters=Counters(),
        rs=ReviewState(),
        gh=ReviewGitHub(raises),
    )


def test_review_lifecycle():
    wu = tc.work_units
    saved = {name: getattr(wu, name) for name in ("run_to_logfile", "review_in_bubble", "me", "log")}
    logs = []
    wu.run_to_logfile = lambda *args: 0
    wu.review_in_bubble = lambda *args: 0
    wu.me = lambda: "authenticated-login"
    wu.log = logs.append
    candidate = types.SimpleNamespace(pr=42, head="a" * 40, contest=None, contest_reply_id=None)
    opts = types.SimpleNamespace(work_model="codex")
    try:
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            host = worker(state)
            check("host posted review stays successful", wu.do_review(host, None, candidate, opts, bubble=False), 0)
            check("host success mutes notifications", host.gh.muted, [42])

            bubble = worker(state)
            check("bubble posted review stays successful", wu.do_review(bubble, None, candidate, opts, bubble=True), 0)
            check("bubble success mutes notifications", bubble.gh.muted, [42])

            failed_mute = worker(state, raises=True)
            check(
                "mute exception does not fail posted review",
                wu.do_review(failed_mute, None, candidate, opts, bubble=False),
                0,
            )
            check("mute exception does not skip scoreboard invalidation", failed_mute.rs.busted, [42])
            check("mute exception is logged", any("notifications FAILED unexpectedly" in line for line in logs))

            wu.run_to_logfile = lambda *args: 7
            failed_review = worker(state)
            check(
                "failed review returns engine status",
                wu.do_review(failed_review, None, candidate, opts, bubble=False),
                7,
            )
            check("failed review never mutes notifications", failed_review.gh.muted, [])
    finally:
        for name, value in saved.items():
            setattr(wu, name, value)


test_notification_api()
test_lookup_failures()
test_pagination()
test_mutation_responses()
test_mute_not_retried_on_transient()
test_failure_diagnostics()
test_review_lifecycle()
print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
