"""$TAUCETI_SKIP_PACING launches the pinned agent with no usage read."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tauceti_worker.loop as loop

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r} want={want!r}")
    fails += not ok


def boom(*_a, **_k):
    raise AssertionError("the pacer was consulted")


loop.choose_model = boom
os.environ["TAUCETI_SKIP_PACING"] = "1"
check(
    "claude launches without a usage read",
    loop.resolve_work_model(None, "claude", dry=False, ignore_quota=False),
    ("claude", False),
)
check("codex too", loop.resolve_work_model(None, "codex", dry=False, ignore_quota=False), ("codex", False))
os.environ["TAUCETI_SKIP_PACING"] = "0"
try:
    loop.resolve_work_model(None, "claude", dry=False, ignore_quota=False)
    consulted = False
except AssertionError:
    consulted = True
check("unset/0 still consults the pacer", consulted, True)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
