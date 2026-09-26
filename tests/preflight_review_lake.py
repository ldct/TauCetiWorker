#!/usr/bin/env python3
"""preflight() gates a HOST authoring round on a local `lake` toolchain — but review is not an
authoring stage: it runs the fetched-on-demand review engine and never compiles. Since the sandbox
default flipped to host, `tauceti work --only review` runs on the host by default, so a stray `lake`
requirement would falsely block every review-only machine that has no Lean toolchain. This test pins
that review-host preflight passes without `lake`, while an authoring stage still requires it.

Exit 0 = both cases agree; 1 = a mismatch."""

import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0


def check(name, ok):
    global fails
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}")


def opts(only):
    return tc.RoundOpts(only=only, agent="claude", work_model="claude", sandbox_host=True, dry_run=False)


# Only `lake` is absent; gh/git/uvx present so preflight reaches the toolchain gate.
tc.cli._have = lambda tool: tool != "lake"
CFG = SimpleNamespace()  # review-host preflight never touches cfg (uses_fork excludes review)

raised = False
try:
    tc.cli.preflight(CFG, opts(["review", "progress"]))
except tc.Die:
    raised = True
check("host review+progress without lake is NOT blocked", not raised)

for tasks in (["review"], ["progress"]):
    tc.cli.preflight(CFG, opts(tasks))
    check(f"{tasks} needs no lake", True)

# Every other host task still needs the build toolchain, including mixed fleets.
for tasks in (
    ["fix"],
    ["rebase"],
    ["bump"],
    ["lint-repair"],
    ["fix-ci"],
    ["roadmap"],
    ["review", "progress", "fix"],
    [],
):
    raised = False
    try:
        tc.cli.preflight(CFG, opts(tasks))
    except tc.Die as e:
        raised = "lake" in str(e)
    check(f"host {tasks or 'all'} without lake IS blocked", raised)

light = set(tc.cli.resolve_tasks(["review,progress"], []))
heavy = set(tc.cli.resolve_tasks([], ["review,progress"]))
check("fleets are disjoint", not light & heavy)
check("fleets cover all tasks", light | heavy == set(tc.ALLOWED_TASKS))
check("light fleet is exactly review+progress", light == {"review", "progress"})

sys.exit(1 if fails else 0)
