#!/usr/bin/env python3
"""The hard remaining-quota reserve defaults to 10% and is configurable end to end."""

import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0


def check(name, cond):
    global fails
    print(f"[{'OK ' if cond else 'XX '}] {name}")
    fails += not cond


saved = os.environ.get("TAUCETI_QUOTA_RESERVE")
try:
    os.environ.pop("TAUCETI_QUOTA_RESERVE", None)
    check("default reserve is 10%", tc.quota_reserve() == 10.0)
    check("numbers parse", tc.parse_quota_reserve("12.5") == 12.5)
    for bad in (-1, 101, "nope", "nan", "inf", True):
        try:
            tc.parse_quota_reserve(bad)
            check(f"rejects {bad!r}", False)
        except ValueError:
            check(f"rejects {bad!r}", True)

    # The guard is strict as requested: exactly 10% remains launchable, below 10% is not.
    os.environ["TAUCETI_PACE"] = "0:100"
    at_floor = tc._classify_window("weekly", 90, 50, time.time() + 1000, False)
    below = tc._classify_window("weekly", 90.1, 50, time.time() + 1000, False)
    check("exactly 10% remaining is allowed", at_floor.status == tc.STATUS_UNDER_PACE)
    check("below 10% remaining is hard-blocked", below.status == tc.STATUS_BELOW_RESERVE)
    check("reserve block waits for reset", tc.Quota._next_eligible([below]) == below.resets_at)
    p = tc.Provider("codex", False, None, [below])
    check("reserve is hard even under --ignore-quota", tc._ignore_quota_verdict(None, p) == "wait")
    check(
        "reason names remaining quota and reserve",
        tc._unavail_reason(p)[1] == "weekly below quota reserve (9.9% left < 10%)",
    )

    # This deployment's environment must survive upstream's newer quota-reason handling.
    os.environ["TAUCETI_QUOTA_RESERVE"] = "2"
    tc.cli.resolve_quota_reserve("work", type("Args", (), {"quota_reserve": None})())
    check("deployment's 2% env reserve is retained", tc.quota_reserve() == 2.0)
    at_two = tc._classify_window("weekly", 98, 50, time.time() + 1000, False)
    below_two = tc._classify_window("weekly", 98.1, 50, time.time() + 1000, False)
    check("exactly 2% remaining is launchable", at_two.status == tc.STATUS_UNDER_PACE)
    p = tc.Provider("codex", False, None, [below_two])
    check("below 2% remains a hard guard", tc._ignore_quota_verdict(None, p) == "wait")
    check("2% reason survives upstream quota changes", tc._unavail_reason(p)[1].endswith("left < 2%)"))

    os.environ["TAUCETI_QUOTA_RESERVE"] = "0"
    disabled = tc._classify_window("weekly", 99, 50, None, False)
    check("zero disables the reserve", disabled.status == tc.STATUS_UNDER_PACE)

    # The work flag overrides and installs the environment inherited by loop children.
    tc.cli.resolve_quota_reserve("work", type("Args", (), {"quota_reserve": "7.5"})())
    check("CLI override is installed", os.environ["TAUCETI_QUOTA_RESERVE"] == "7.5")
    for invalid in ("101", ""):
        try:
            tc.cli.resolve_quota_reserve("work", type("Args", (), {"quota_reserve": invalid})())
            check(f"invalid CLI reserve {invalid!r} rejected", False)
        except tc.Die as e:
            check(f"invalid CLI reserve {invalid!r} rejected", "--quota-reserve" in str(e))

    spec = tc.WorkerSpec.from_dict({"id": "reserve", "quota_reserve": 5}, 0)
    check("workers.toml stores the reserve", spec.quota_reserve == 5.0 and spec.as_dict()["quota_reserve"] == 5.0)
    argv = spec.work_argv()
    check("managed worker forwards the reserve", argv[argv.index("--quota-reserve") + 1] == "5.0")
    for bad in (-1, 101, True, "bad"):
        try:
            tc.WorkerSpec.from_dict({"id": "reserve", "quota_reserve": bad}, 0)
            check(f"workers.toml rejects {bad!r}", False)
        except tc.WorkersError:
            check(f"workers.toml rejects {bad!r}", True)
finally:
    os.environ.pop("TAUCETI_PACE", None)
    os.environ.pop("TAUCETI_QUOTA_RESERVE", None)
    if saved is not None:
        os.environ["TAUCETI_QUOTA_RESERVE"] = saved

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
