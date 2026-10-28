#!/usr/bin/env python3
"""A blind-approval scoreboard reads as unreviewed, exactly as TauCeti's merge labeller (TauCetiReview#195).

  1. The real blind board (fixture from TauCeti#21777) is detected, and its head is not a clean head.
  2. A sighted board, and a blind board that BLOCKS, are unchanged.
  3. An older sighted board for the same head decides over a newer blind one; one for another head
     does not.
  4. The regexes match TauCetiReview's when that checkout is available (skipped otherwise).

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tauceti_worker import blind_review as br  # noqa: E402
from tauceti_worker.review_state import _clean_head, scoreboard  # noqa: E402

fails = 0
BLIND = (REPO / "tests/fixtures/blind-review/blind_approval.md").read_text()
HEAD = json.loads(br.META_RE.findall(BLIND)[-1])["head_sha"]


def check(name, cond):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")


def board(body, head=HEAD, states=None, when="2026-10-27T00:00:00Z"):
    meta = {"head_sha": head, "states": states or {"correctness": "green"}, "round": 1}
    body = re.sub(r"<!--tauceti-meta:v1 .*?-->", "", body, flags=re.S) + f"\n<!--tauceti-meta:v1 {json.dumps(meta)}-->"
    return {"body": body, "updated_at": when, "user": {"login": "x"}}


SIGHTED = (
    "<!--tauceti-scoreboard-->\n| | rubric | state | judge | summary |\n|---|---|---|---|---|\n"
    "| ✅ | [correctness](x) | approved | `claude` | Checked the proofs against Mathlib; all sound. |\n"
)


def main():
    real = {"body": BLIND, "updated_at": "2026-10-27T15:01:07Z"}
    check("the real blind board names blind rubrics", len(br.blind_rubrics(BLIND)) >= 5)
    meta = scoreboard([real])
    check("a lone blind approval is flagged", bool(meta and meta.get("blind_approval")))
    check("a blind approval is not a clean head", _clean_head(meta) == "")

    sighted = board(SIGHTED)
    check("a sighted board is unchanged", scoreboard([sighted]).get("blind_approval") is None)
    check("a sighted board's head is clean", _clean_head(scoreboard([sighted])) == HEAD)

    blocking = board(BLIND, states={"correctness": "blocking_request"})
    check("a blind board that blocks still counts", _clean_head(scoreboard([blocking])) == HEAD)

    older_same = board(SIGHTED, when="2026-10-26T00:00:00Z")
    newer_blind = board(BLIND, when="2026-10-27T00:00:00Z")
    got = scoreboard([older_same, newer_blind])
    check(
        "an older sighted board for the same head decides", _clean_head(got) == HEAD and not got.get("blind_approval")
    )

    older_other = board(SIGHTED, head="b" * 40, when="2026-10-26T00:00:00Z")
    check("a sighted board for another head does not", _clean_head(scoreboard([older_other, newer_blind])) == "")
    check("no scoreboard is still None", scoreboard([]) is None)

    upstream = Path.home() / "TauCetiReview/runner/merge_from_scoreboard.py"
    if upstream.exists():
        src = upstream.read_text()
        for name in ("_BLIND_TOOL", "_BLIND_BAD", "_BLIND_PLACE"):
            m = re.search(rf"^{name} = (.*?)\n(?=\S)", src, re.S | re.M)
            ours = getattr(br, name)
            check(f"{name} matches TauCetiReview", m is not None and eval(m.group(1)) == ours)  # noqa: S307
        m = re.search(r"^TABLE_ROW_RE = re.compile\((r\".*?\")", src, re.M)
        check("TABLE_ROW_RE matches TauCetiReview", m is not None and eval(m.group(1)) == br.TABLE_ROW_RE.pattern)  # noqa: S307
    else:
        print("[skip] TauCetiReview checkout not found; regex parity not checked")

    print("all ok" if not fails else f"{fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
