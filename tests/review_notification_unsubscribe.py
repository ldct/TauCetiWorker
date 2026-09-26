#!/usr/bin/env python3
"""Posted PR reviews best-effort mute the authenticated account's notification thread.

No network is used: both the GitHub REST calls and review engine are mocked. The cases pin the
thread lookup/API payload and make sure notification failures cannot turn an already-posted review
into a failure or cause it to be replayed.
"""

import json
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


def test_notification_api():
    gh = tc.GitHub("TauCetiProject/TauCeti")
    calls, logs = [], []
    replies = iter(
        [
            cp(
                stdout=json.dumps(
                    [
                        {
                            "id": "other",
                            "subject": {
                                "type": "PullRequest",
                                "url": "https://api.github.com/repos/TauCetiProject/TauCeti/pulls/7",
                            },
                        },
                        {
                            "id": "42",
                            "subject": {
                                "type": "PullRequest",
                                "url": "https://ghe.example/api/v3/repos/TauCetiProject/TauCeti/pulls/42/",
                            },
                        },
                        {
                            "id": "wrong-kind",
                            "subject": {
                                "type": "Issue",
                                "url": "https://api.github.com/repos/TauCetiProject/TauCeti/pulls/42",
                            },
                        },
                    ]
                )
            ),
            cp(),
        ]
    )
    gh._gh = lambda args: calls.append(args) or next(replies)
    old_log = tc.github.log
    tc.github.log = logs.append
    try:
        check("matching PR notification is muted", gh.ignore_pr_notifications(42))
        check(
            "lists all repository notification threads",
            calls[0],
            ["api", "--paginate", "/repos/TauCetiProject/TauCeti/notifications?all=true&per_page=50"],
        )
        check(
            "sets GitHub ignored subscription field",
            calls[1],
            ["api", "-X", "PUT", "/notifications/threads/42/subscription", "-F", "ignored=true"],
        )
        check("successful mute is logged", "disabled notifications" in logs[-1])
    finally:
        tc.github.log = old_log

    gh = tc.GitHub("TauCetiProject/TauCeti")
    calls, logs = [], []
    gh._gh = lambda args: calls.append(args) or cp(stdout="[]")
    old_log = tc.github.log
    tc.github.log = logs.append
    try:
        check("no notification is a no-op", gh.ignore_pr_notifications(42), False)
        check("no notification makes no PUT", len(calls), 1)
        check("no notification is clear in logs", "no GitHub notification thread" in logs[-1])
    finally:
        tc.github.log = old_log

    gh = tc.GitHub("TauCetiProject/TauCeti")
    calls, logs = [], []
    replies = iter(
        [
            cp(
                stdout=json.dumps(
                    [
                        {
                            "id": "42",
                            "subject": {
                                "type": "PullRequest",
                                "url": "https://api.github.com/repos/TauCetiProject/TauCeti/pulls/42",
                            },
                        }
                    ]
                )
            ),
            cp(1, stderr="HTTP 403 forbidden"),
        ]
    )
    gh._gh = lambda args: calls.append(args) or next(replies)
    old_log = tc.github.log
    tc.github.log = logs.append
    try:
        check("subscription API failure is reported", gh.ignore_pr_notifications(42), False)
        check("subscription API failure is non-throwing", "FAILED" in logs[-1])
    finally:
        tc.github.log = old_log


def test_mute_not_retried_on_transient():
    # Upstream added retries to gh_run. Reads may retry, but never replay an ambiguous PUT.
    gh = tc.GitHub("TauCetiProject/TauCeti")
    thread = {
        "id": "42",
        "subject": {"type": "PullRequest", "url": "https://api.github.com/repos/TauCetiProject/TauCeti/pulls/42"},
    }
    with (
        patch.object(tc.github, "run", side_effect=[cp(stdout=json.dumps([thread])), cp(1, stderr="HTTP 504")]) as run,
        patch.object(tc.github.time, "sleep") as sleep,
        patch.object(tc.github, "log"),
    ):
        check("transient notification PUT failure stays best-effort", gh.ignore_pr_notifications(42), False)
        check("ambiguous notification PUT is not replayed", run.call_count, 2)
        check("ambiguous notification PUT never sleeps to retry", sleep.call_count, 0)
        check("PUT still sends ignored as a boolean", run.call_args.args[0][-2:], ["-F", "ignored=true"])


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
    saved = {
        name: getattr(wu, name) for name in ("run_to_logfile", "review_in_bubble", "_sync_review_outbox", "me", "log")
    }
    calls, logs = [], []
    wu.run_to_logfile = lambda *args: 0
    wu.review_in_bubble = lambda *args: 0
    wu._sync_review_outbox = lambda w, pr: calls.append(("sync", pr)) or 0
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
            check("host success continues archive sync", calls, [("sync", 42)])

            calls.clear()
            bubble = worker(state)
            check("bubble posted review stays successful", wu.do_review(bubble, None, candidate, opts, bubble=True), 0)
            check("bubble success mutes notifications", bubble.gh.muted, [42])

            calls.clear()
            failed_mute = worker(state, raises=True)
            check(
                "mute exception does not fail posted review",
                wu.do_review(failed_mute, None, candidate, opts, bubble=False),
                0,
            )
            check("mute exception does not skip archive sync", calls, [("sync", 42)])
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
test_mute_not_retried_on_transient()
test_review_lifecycle()
print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
