"""Treat a "blind approval" scoreboard as no review, as TauCeti's merge labeller does.

TauCetiReview#195 stopped counting an approving scoreboard as the merge verdict when one of its rubric
rows says the judge could not inspect the workspace (its tool sandbox failed, or the pinned Mathlib
source was missing), so it voted on the diff alone. CI then returned ~200 such PRs to awaiting-review,
but the worker kept reading their newest current-head board as a completed review and never picked
them. This mirrors the labeller (merge_from_scoreboard.py: BLIND_ROW_RE, blind_rubrics,
is_blind_approval, latest_completed_scoreboard): among the current head's completed boards, blind
approvals are skipped; a sighted one decides; with none, the head reads as unreviewed. The regexes are
copied verbatim from TauCetiReview at 98063f7 so the two agree on which rows are blind.

Reading model prose can only WITHHOLD an approval here, never grant one or remove a block, exactly as
upstream: a blind board that blocks still counts.
"""

from __future__ import annotations

import json
import re

SCOREBOARD_MARKER = "<!--tauceti-scoreboard-->"
META_RE = re.compile(r"<!--tauceti-meta:v1 (.*?)-->", re.S)
DEFAULT_RUBRICS = frozenset(
    ["correctness", "reuse", "scope", "attribution", "api-design",
     "generality", "placement", "naming", "documentation", "proof-quality"]
)  # fmt: skip
TABLE_ROW_RE = re.compile(r"^\|[^|]*\|\s*\[?([a-z0-9-]+)\]?[^|]*\|\s*([^|]+?)\s*\|", re.M)
WORD_STATE = {
    "approved": "green",
    "changes requested": "blocking_request",
    "blocked": "blocking_block",
    "stale (re-run pending)": "stale",
    "not yet run": "absent",
    "error": "error",
}
BLOCKING_STATES = {"blocking_request", "blocking_block"}
_BLIND_TOOL = (r"(?:sandbox|command runner|command tool|read-only (?:command|shell|tool|search)s?"
               r"|read (?:command|tool)s?|shell|filesystem tools?|search tools?)")  # fmt: skip
_BLIND_BAD = r"(?:fail\w*|error|unavailable|could not (?:start|run|initiali[sz]e)|did not start|prevented)"
_BLIND_PLACE = r"(?:the|its|this) (?:checkout|runner|workspace|sandbox)"
BLIND_ROW_RE = re.compile(
    rf"\b{_BLIND_TOOL}\b[^|.;]{{0,60}}?\b{_BLIND_BAD}"
    r"|\b(?:inspection|verification|searches|search|cross-checks?|comparison|grep)\b[^|.;]{0,40}?"
    r"\b(?:was|were) (?:unavailable|prevented|not possible)"
    r"|\b(?:searches|inspection)\b[^|.;]{0,20}?could not (?:run|execute|be (?:run|performed))"
    rf"|mathlib[^|.;]{{0,60}}?\b(?:not (?:in|present in|available in|checked out in) {_BLIND_PLACE}"
    rf"|absent from {_BLIND_PLACE}|missing from {_BLIND_PLACE}|could not be (?:searched|grepped))"
    r"|mathlib (?:checkout|source|sources) (?:was|were|is) (?:absent|missing|not present)",
    re.I,
)  # fmt: skip


def blind_rubrics(body: str | None) -> set[str]:
    out = set()
    for row in (body or "").splitlines():
        m = TABLE_ROW_RE.match(row)
        if m and m.group(1) in DEFAULT_RUBRICS and BLIND_ROW_RE.search(row):
            out.add(m.group(1))
    return out


def _states(body: str, meta: dict) -> dict:
    raw = meta.get("states")
    if isinstance(raw, dict) and raw:
        return raw
    out = {}
    for rubric, word in TABLE_ROW_RE.findall(body or ""):
        w = word.strip().lower()
        if rubric in DEFAULT_RUBRICS and w in WORD_STATE:
            out[rubric] = WORD_STATE[w]
    return out


def is_blind_approval(body: str | None, meta: dict) -> bool:
    return bool(blind_rubrics(body)) and not any(s in BLOCKING_STATES for s in _states(body or "", meta).values())


def _meta(body: str) -> dict | None:
    found = META_RE.findall(body)
    if not found:
        return None
    try:
        d = json.loads(found[-1].strip())
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def sighted_scoreboard(comments, newest: dict | None) -> dict | None:
    """`newest` (the meta survey_facts.scoreboard picked), adjusted for blind approvals.

    Not blind → unchanged. Blind → the newest SIGHTED completed board for the same head if one exists,
    else a copy of `newest` flagged `blind_approval`, which _clean_head reads as "not reviewed"."""
    if not isinstance(newest, dict):
        return newest
    head = newest.get("head_sha")
    boards = []
    for index, c in enumerate(comments or []):
        body = c.get("body") or ""
        if SCOREBOARD_MARKER not in body:
            continue
        meta = _meta(body)
        if not meta or meta.get("mode") == "init" or meta.get("head_sha") != head:
            continue
        boards.append(((c.get("updated_at") or "", index), body, meta))
    boards.sort(key=lambda b: b[0], reverse=True)
    if not boards or not is_blind_approval(boards[0][1], boards[0][2]):
        return newest
    for _, body, meta in boards[1:]:
        if not is_blind_approval(body, meta):
            return meta
    return {**newest, "blind_approval": True}
