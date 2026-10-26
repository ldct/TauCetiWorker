#!/usr/bin/env python3
"""A provider rate limit backs every worker on the machine off, escalating, until a review succeeds.

The engine stops a round with REVIEW_PROVIDER_DOWN_EXIT when its provider answers 429 to consecutive
rubrics. A fleet manager launches a fresh one-shot round every minute, and each one used to walk
straight back into the same limit: the in-process loop back-off never sees it. Pinned here:

  1. record() escalates 5m -> 10m -> ... capped, never shortens a longer wait, and clear() resets it.
  2. The engine's abort line is parsed for the providers it names; other aborts record nothing.
  3. do_review records a back-off for a rate-limit abort, none for an auth abort, and clears it on
     success.
  4. resolve_work_model refuses to launch a backed-off provider even under --ignore-quota and
     $TAUCETI_SKIP_PACING, naming when it will retry.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import os
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
_tmp = tempfile.TemporaryDirectory()
os.environ["TAUCETI_PROVIDER_BACKOFF_DIR"] = _tmp.name
import tauceti_worker as tc  # noqa: E402
from tauceti_worker import provider_backoff as pb  # noqa: E402
from tauceti_worker.constants import REVIEW_PROVIDER_DOWN_EXIT  # noqa: E402

fails = 0
RATE_LINE = (
    "review aborted: the provider is rate limiting this account (every configured provider \u2014 codex \u2014 "
    "failed 2 consecutive attempts)\n"
)
AUTH_LINE = (
    "review aborted: reviewer authentication failed (every configured provider \u2014 codex \u2014 "
    "failed 2 consecutive attempts)\n"
)


def check(name, cond):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")


class Counters:
    def __init__(self):
        self.v = {}

    def read(self, k):
        return self.v.get(k, 0)

    def incr(self, k):
        self.v[k] = self.v.get(k, 0) + 1
        return self.v[k]

    def write(self, k, n):
        self.v[k] = n


def drive(rc, state, log_text=""):
    w = types.SimpleNamespace(
        cfg=types.SimpleNamespace(state=state, logdir=state, wid="w", store_dir=state / "store"),
        counters=Counters(),
        rs=types.SimpleNamespace(bust=lambda pr: None, review_rounds=lambda pr, c: 0),
        gh=types.SimpleNamespace(
            add_reaction=lambda i: True, remove_reaction=lambda i: True, ignore_pr_notifications=lambda pr: True
        ),
    )
    c = types.SimpleNamespace(pr=42, head="a" * 40, contest=None, contest_reply_id=None)
    opts = types.SimpleNamespace(work_model="codex")

    def fake_run(argv, logf, label):
        Path(logf).write_text("[generality] no verdict: whatever\n" + log_text)
        return rc

    orig_run, orig_me = tc.work_units.run_to_logfile, tc.work_units.me
    tc.work_units.run_to_logfile, tc.work_units.me = fake_run, lambda: "tester"
    try:
        tc.work_units.do_review(w, types.SimpleNamespace(), c, opts, bubble=False)
        return None
    except tc.config.NoProgress as e:
        return e
    finally:
        tc.work_units.run_to_logfile, tc.work_units.me = orig_run, orig_me


def main():
    now = 1_000_000.0
    # 1) escalation, cap, never-shorten, clear
    waits = [pb.record("codex", now=now) for _ in range(7)]
    check("first wait is the bottom step", waits[0] == pb.BACKOFF_STEPS_S[0])
    check("waits escalate", waits[1] == pb.BACKOFF_STEPS_S[1] and waits[2] == pb.BACKOFF_STEPS_S[2])
    check("waits are capped", waits[-1] == max(pb.BACKOFF_STEPS_S))
    left, strikes = pb.remaining("codex", now=now)
    check("remaining reports the wait and strikes", left == max(pb.BACKOFF_STEPS_S) and strikes == 7)
    check("expired back-off is not in force", pb.remaining("codex", now=now + 10_000) is None)
    pb.clear("codex")
    check("clear removes it", pb.remaining("codex", now=now) is None)
    pb.record("codex", now=now)
    pb.record("codex", now=now)  # strikes=2 -> 600s
    check("strikes survive expiry until a success", pb.record("codex", now=now + 5000) == pb.BACKOFF_STEPS_S[2])
    pb.clear("codex")
    check("another provider is independent", pb.remaining("claude") is None)

    # 2) parsing
    check("rate-limit line names codex", pb.rate_limited_providers(RATE_LINE, ["x"]) == ["codex"])
    check("auth line records nothing", pb.rate_limited_providers(AUTH_LINE, ["codex"]) == [])
    check(
        "an engine that names no provider falls back to the configured one",
        pb.rate_limited_providers("review aborted: the provider is rate limiting this account\n", ["codex"])
        == ["codex"],
    )

    with tempfile.TemporaryDirectory() as d:
        state = Path(d)
        (state / "store").mkdir()
        # 3) do_review
        raised = drive(REVIEW_PROVIDER_DOWN_EXIT, state, RATE_LINE)
        check(
            "rate-limit abort still raises NoProgress, uncharged", raised is not None and "not charged" in str(raised)
        )
        check("rate-limit abort records a back-off", pb.remaining("codex") is not None)
        pb.clear("codex")
        drive(REVIEW_PROVIDER_DOWN_EXIT, state, AUTH_LINE)
        check("auth abort records no back-off", pb.remaining("codex") is None)
        pb.record("codex")
        check("success clears the back-off", drive(0, state) is None and pb.remaining("codex") is None)

    # 4) resolve_work_model
    pb.record("codex")
    for label, env in (("--ignore-quota", {}), ("TAUCETI_SKIP_PACING", {"TAUCETI_SKIP_PACING": "1"})):
        os.environ.update(env)
        try:
            tc.loop.resolve_work_model(object(), "codex", dry=False, ignore_quota=True, fresh=True)
            got = None
        except tc.config.NoProgress as e:
            got = str(e)
        finally:
            os.environ.pop("TAUCETI_SKIP_PACING", None)
        check(f"backed-off codex is not launched under {label}", got is not None and "retry at" in got)
    check(
        "dry run is unaffected",
        tc.loop.resolve_work_model(object(), "codex", dry=True, ignore_quota=True)[0] == "codex",
    )
    pb.clear("codex")

    print("all ok" if not fails else f"{fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
