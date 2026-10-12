#!/usr/bin/env python3
"""Posted PR reviews ignore the personal subscription without listing notification history.

No network is used: PR lookup, subscription mutation and review engine are mocked. Pin the
public-read/personal-write boundary, validate GraphQL confirmation, and never replay a mutation
or invalidate an already-posted review when notification muting fails.
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


NODE_ID = "PR_node42"
LOOKUP = ["pr", "view", "42", "--repo", "TauCetiProject/TauCeti", "--json", "id"]
MUTATION = (
    "mutation($id:ID!){updateSubscription(input:{subscribableId:$id,state:IGNORED})"
    "{subscribable{id viewerSubscription}}}"
)
MUTATE = ["gh", "api", "graphql", "-f", f"query={MUTATION}", "-f", f"id={NODE_ID}"]


def confirmed(node_id=NODE_ID, state="IGNORED"):
    return {"data": {"updateSubscription": {"subscribable": {"id": node_id, "viewerSubscription": state}}}}


def test_notification_api():
    gh = tc.GitHub("TauCetiProject/TauCeti")
    with (
        patch.object(gh, "_gh", return_value=cp(stdout=json.dumps({"id": NODE_ID}))) as lookup,
        patch.object(tc.github, "run", return_value=cp(stdout=json.dumps(confirmed()))) as mutate,
        patch.object(tc.github, "log") as log,
    ):
        check("matching PR subscription is muted", gh.ignore_pr_notifications(42))
        check("public lookup uses explicit repo and only PR node ID", lookup.call_args.args[0], LOOKUP)
        check("lookup happens once", lookup.call_count, 1)
        check("mutation sets personal subscription IGNORED", mutate.call_args.args[0], MUTATE)
        check("mutation is executed exactly once", mutate.call_count, 1)
        check("successful mute is logged", "disabled notifications" in log.call_args.args[0])

    from tauceti_worker.github_app import public_read

    with patch.dict(os.environ, {"GH_HOST": "github.com"}):
        check("PR ID lookup is App eligible", public_read(LOOKUP, [gh.repo]))
        check("subscription mutation must stay personal", public_read(MUTATE[1:], [gh.repo]), False)
    check("subscription mutation is not retryable as a read", tc.github._gh_read_only(MUTATE), False)
    check("notification history endpoint is absent", "notifications" in " ".join(LOOKUP + MUTATE), False)


def test_lookup_failures():
    gh = tc.GitHub("TauCetiProject/TauCeti")
    cases = [
        ("lookup HTTP failure", cp(1, stderr="HTTP 404")),
        ("invalid JSON", cp(stdout="not json")),
        ("empty output", cp()),
        ("null response", cp(stdout="null")),
        ("list response", cp(stdout="[]")),
        ("missing ID", cp(stdout="{}")),
        ("null ID", cp(stdout='{"id":null}')),
        ("numeric ID", cp(stdout='{"id":42}')),
        ("empty ID", cp(stdout='{"id":""}')),
        ("whitespace ID", cp(stdout='{"id":" PR_node42"}')),
        ("file expansion ID", cp(stdout='{"id":"@/private/file"}')),
    ]
    for name, response in cases:
        with (
            patch.object(gh, "_gh", return_value=response),
            patch.object(tc.github, "run") as mutate,
            patch.object(tc.github, "log") as log,
        ):
            check(f"{name} fails best-effort", gh.ignore_pr_notifications(42), False)
            check(f"{name} never mutates", mutate.call_count, 0)
            check(f"{name} preserves posted review", "review remains successful" in log.call_args.args[0])
    with (
        patch.object(gh, "_gh", side_effect=OSError("unexpected")),
        patch.object(tc.github, "run") as mutate,
        patch.object(tc.github, "log"),
    ):
        check("lookup exception fails best-effort", gh.ignore_pr_notifications(42), False)
        check("lookup exception never mutates", mutate.call_count, 0)


def test_mutation_responses():
    gh = tc.GitHub("TauCetiProject/TauCeti")
    partial = {**confirmed(), "errors": [{"message": "partial failure"}]}
    cases = [
        ("empty output", ""),
        ("malformed JSON", "not json"),
        ("null response", "null"),
        ("list response", "[]"),
        ("missing data", "{}"),
        ("null data", '{"data":null}'),
        ("invalid data", '{"data":[]}'),
        ("missing mutation", '{"data":{}}'),
        ("null mutation", '{"data":{"updateSubscription":null}}'),
        ("missing subscribable", '{"data":{"updateSubscription":{}}}'),
        ("null subscribable", '{"data":{"updateSubscription":{"subscribable":null}}}'),
        ("missing subscription fields", '{"data":{"updateSubscription":{"subscribable":{}}}}'),
        ("mismatched node ID", json.dumps(confirmed("PR_other"))),
        ("not ignored", json.dumps(confirmed(state="UNSUBSCRIBED"))),
        ("subscribed", json.dumps(confirmed(state="SUBSCRIBED"))),
        ("missing viewer subscription", json.dumps(confirmed(state=None))),
        ("GraphQL errors", json.dumps({"errors": [{"message": "denied"}]})),
        ("partial success with errors", json.dumps(partial)),
        ("malformed errors", json.dumps({**confirmed(), "errors": {}})),
    ]
    for name, stdout in cases:
        with (
            patch.object(gh, "_gh", return_value=cp(stdout=json.dumps({"id": NODE_ID}))),
            patch.object(tc.github, "run", return_value=cp(stdout=stdout)) as mutate,
            patch.object(tc.github, "log") as log,
        ):
            check(f"mutation {name} is not success", gh.ignore_pr_notifications(42), False)
            check(f"mutation {name} is never replayed", mutate.call_count, 1)
            check(f"mutation {name} logs best-effort failure", "review remains successful" in log.call_args.args[0])

    with (
        patch.object(gh, "_gh", return_value=cp(stdout=json.dumps({"id": NODE_ID}))),
        patch.object(tc.github, "run", side_effect=OSError("unexpected")) as mutate,
        patch.object(tc.github, "log"),
    ):
        check("mutation exception fails best-effort", gh.ignore_pr_notifications(42), False)
        check("mutation exception is never replayed", mutate.call_count, 1)


def test_mute_not_retried_on_transient():
    gh = tc.GitHub("TauCetiProject/TauCeti")
    # Exercise the actual _gh -> gh_run read path, then verify the mutation bypasses gh_run even
    # for secondary rate limits (gh_run otherwise retries writes on explicit rate rejection).
    for error in (
        "HTTP 504",
        "HTTP 403 secondary rate limit Retry-After: 1",
        "HTTP 403 API rate limit exceeded",
        "HTTP 429",
    ):
        with (
            patch.object(
                tc.github, "run", side_effect=[cp(stdout=json.dumps({"id": NODE_ID})), cp(1, stderr=error)]
            ) as run,
            patch.object(tc.github.time, "sleep") as sleep,
            patch.object(tc.github, "log"),
        ):
            check(f"{error} mutation stays best-effort", gh.ignore_pr_notifications(42), False)
            check(f"{error} makes one read and one mutation", run.call_count, 2)
            check(f"{error} never sleeps to retry", sleep.call_count, 0)
            check(f"{error} uses the exact mutation argv", run.call_args.args[0], MUTATE)


def test_failure_diagnostics():
    gh = tc.GitHub("TauCetiProject/TauCeti")
    secret = "private-test-token"
    with (
        patch.dict(os.environ, {"GH_TOKEN": secret}),
        patch.object(gh, "_gh", return_value=cp(stdout=json.dumps({"id": NODE_ID}))),
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
test_mutation_responses()
test_mute_not_retried_on_transient()
test_failure_diagnostics()
test_review_lifecycle()
print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
