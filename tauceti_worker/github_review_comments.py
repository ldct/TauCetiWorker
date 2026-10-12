"""Survey inline-comment reads: bounded trusted documents, repository-bound pagination.

No global node lookup: a thread continuation re-selects one edge of the SAME PR's
reviewThreads using its predecessor cursor, then verifies its identity. Even attacker
supplied cursors cannot route an allowlisted repository query into another repository.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from datetime import datetime

from .github_comments import _failed_aliases

REVIEW_BATCH_SIZE = 10
REVIEW_MAX_PAGES = 100  # independently for each PR's threads and each thread's comments
# At most 10 * (20 threads + 20 * 20 comments), even counting replyTo objects < 10k
# nodes, comfortably below GitHub's 500k node limit. Continuations are smaller still.
_COMMENT_FIELDS = (
    "totalCount nodes{fullDatabaseId body createdAt replyTo{fullDatabaseId}} pageInfo{hasNextPage endCursor}"
)
REVIEW_QUERIES = {
    size: "query($owner:String!,$name:String!,"
    + ",".join(f"$pr{i}:Int!,$cursor{i}:String" for i in range(size))
    + "){repository(owner:$owner,name:$name){"
    + "".join(
        f"pr{i}:pullRequest(number:$pr{i}){{number reviewThreads(first:20,after:$cursor{i}){{"
        "totalCount edges{cursor node{id comments(first:20){"
        + _COMMENT_FIELDS
        + "}}} pageInfo{hasNextPage endCursor}}}"
        for i in range(size)
    )
    + "}}"
    for size in range(1, REVIEW_BATCH_SIZE + 1)
}
REVIEW_THREAD_QUERY = (
    "query($owner:String!,$name:String!,$pr0:Int!,$threadCursor:String,$commentCursor:String!){"
    "repository(owner:$owner,name:$name){pr0:pullRequest(number:$pr0){number "
    "reviewThreads(first:1,after:$threadCursor){totalCount edges{cursor node{id "
    "comments(first:100,after:$commentCursor){" + _COMMENT_FIELDS + "}}} pageInfo{hasNextPage endCursor}}}}}"
)


def _id(value) -> int:
    # BigInt is serialized as a decimal string. Never round through float, use the
    # deprecated 32-bit databaseId, or treat a missing identifier as an absent contest.
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value):
        return int(value)
    raise ValueError("invalid fullDatabaseId")


def _page(connection: dict, field: str, limit: int):
    nodes, total, info = connection[field], connection["totalCount"], connection["pageInfo"]
    more, cursor = info["hasNextPage"], info["endCursor"]
    if type(total) is not int or total < 0 or not isinstance(nodes, list) or len(nodes) > limit:
        raise ValueError("invalid connection")
    if type(more) is not bool or (cursor is not None and (not isinstance(cursor, str) or not cursor)):
        raise ValueError("invalid page info")
    if bool(nodes) != bool(cursor) or (more and not nodes) or len(nodes) > total:
        raise ValueError("inconsistent connection")
    return nodes, total, more, cursor


def _threads(node: dict, pr: int, limit: int):
    if type(node["number"]) is not int or node["number"] != pr:
        raise ValueError("wrong PR")
    edges, total, more, cursor = _page(node["reviewThreads"], "edges", limit)
    cursors = [edge["cursor"] for edge in edges]
    if any(not isinstance(c, str) or not c for c in cursors) or len(set(cursors)) != len(cursors):
        raise ValueError("invalid thread cursors")
    if edges and cursors[-1] != cursor:
        raise ValueError("wrong end cursor")
    return edges, total, more, cursor


def _request(gh, repo: str, query: str, variables: dict):
    owner, _, name = repo.partition("/")
    args = ["api", "graphql", "-f", f"query={query}", "-f", f"owner={owner}", "-f", f"name={name}"]
    for key, value in variables.items():
        if value is not None:
            args.extend(["-F" if type(value) is int else "-f", f"{key}={value}"])
    response = gh(args)
    reply = json.loads(response.stdout)
    if not isinstance(reply, dict):
        raise ValueError("invalid response")
    aliases = {key for key in variables if key.startswith("pr")}
    failed = _failed_aliases(reply, aliases, response.returncode)
    repository = reply["data"]["repository"]
    if not isinstance(repository, dict):
        raise ValueError("missing repository")
    return repository, failed


def _rest_comments(comments: list[dict]) -> list[dict]:
    """Flatten GraphQL reply ancestry to REST's top-level in_reply_to_id.

    REST's create-reply endpoint accepts only top-level comments, not replies to
    replies. GraphQL replyTo describes a parent; don't assume every parent is the
    rubric root. Resolve only within the fully fetched thread, rejecting missing
    parents/cycles/multiple roots instead of silently losing contests.
    """
    by_id = {}
    for comment in comments:
        cid = _id(comment["fullDatabaseId"])
        body, created, parent = comment["body"], comment["createdAt"], comment["replyTo"]
        if not isinstance(body, str) or not isinstance(created, str):
            raise ValueError("invalid comment")
        if datetime.fromisoformat(created.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("invalid timestamp")
        if cid in by_id:
            raise ValueError("duplicate comment")
        by_id[cid] = {
            "id": cid,
            "body": body,
            "created_at": created,
            "in_reply_to_id": None if parent is None else _id(parent["fullDatabaseId"]),
        }
    roots = [cid for cid, c in by_id.items() if c["in_reply_to_id"] is None]
    if by_id and len(roots) != 1:
        raise ValueError("missing or ambiguous thread root")
    resolved = set(roots)
    for cid in by_id:
        chain = set()
        parent = cid
        while parent not in resolved:
            if parent in chain or parent not in by_id:
                raise ValueError("cyclic or missing parent")
            chain.add(parent)
            parent = by_id[parent]["in_reply_to_id"]
        resolved.update(chain)
    for c in by_id.values():
        if c["in_reply_to_id"] is not None:
            c["in_reply_to_id"] = roots[0]
    return list(by_id.values())


def _thread_comments(gh, repo, pr, edge, before, thread_total):
    thread = edge["node"]
    tid = thread["id"]
    if not isinstance(tid, str) or not tid:
        raise ValueError("missing thread ID")
    connection = thread["comments"]
    accumulated, seen = [], set()
    expected = None
    for page in range(REVIEW_MAX_PAGES):
        comments, total, more, cursor = _page(connection, "nodes", 20 if page == 0 else 100)
        if expected is None:
            expected = total
        if total != expected or (cursor is not None and cursor in seen):
            raise ValueError("changed count or repeated comments page")
        accumulated.extend(comments)
        if not more:
            if len(accumulated) != total:
                raise ValueError("truncated comments")
            return _rest_comments(accumulated)
        if len(accumulated) >= total or page + 1 == REVIEW_MAX_PAGES:
            raise ValueError("inconsistent continuation or page cap")
        seen.add(cursor)
        repository, failed = _request(
            gh, repo, REVIEW_THREAD_QUERY, {"pr0": pr, "threadCursor": before, "commentCursor": cursor}
        )
        if failed:
            raise ValueError("thread continuation failed")
        edges, count, _, _ = _threads(repository["pr0"], pr, 1)
        if count != thread_total or len(edges) != 1 or edges[0]["cursor"] != edge["cursor"]:
            raise ValueError("thread changed during continuation")
        if edges[0]["node"]["id"] != tid:
            raise ValueError("wrong continuation thread")
        connection = edges[0]["node"]["comments"]
    raise ValueError("comment page cap")


_FAILURES = (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError)


def fetch_review_comments(
    gh: Callable[[list[str]], subprocess.CompletedProcess], repo: str, numbers: list[int]
) -> dict[int, list[dict] | None]:
    """Complete REST-shaped histories; any failure invalidates that PR, never its peers."""
    numbers = list(dict.fromkeys(numbers))
    results = dict.fromkeys(numbers)
    for start in range(0, len(numbers), REVIEW_BATCH_SIZE):
        pending = numbers[start : start + REVIEW_BATCH_SIZE]
        cursors, totals = {}, {}
        accumulated = {pr: [] for pr in pending}
        seen_edges = {pr: set() for pr in pending}
        seen_threads = {pr: set() for pr in pending}
        seen_comments = {pr: set() for pr in pending}
        for _ in range(REVIEW_MAX_PAGES):
            variables = {}
            for i, pr in enumerate(pending):
                variables.update({f"pr{i}": pr, f"cursor{i}": cursors.get(pr)})
            try:
                repository, failed = _request(gh, repo, REVIEW_QUERIES[len(pending)], variables)
            except _FAILURES:
                break
            remaining = []
            for i, pr in enumerate(pending):
                if f"pr{i}" in failed:
                    continue
                try:
                    edges, total, more, cursor = _threads(repository[f"pr{i}"], pr, 20)
                    if total != totals.setdefault(pr, total):
                        raise ValueError("thread count changed")
                    before = cursors.get(pr)
                    for edge in edges:
                        tid = edge["node"]["id"]
                        if not isinstance(tid, str) or not tid or tid in seen_threads[pr]:
                            raise ValueError("missing or repeated thread")
                        if edge["cursor"] in seen_edges[pr]:
                            raise ValueError("repeated thread cursor")
                        comments = _thread_comments(gh, repo, pr, edge, before, total)
                        ids = {c["id"] for c in comments}
                        if ids & seen_comments[pr]:
                            raise ValueError("comment in multiple threads")
                        seen_comments[pr].update(ids)
                        accumulated[pr].extend(comments)
                        seen_threads[pr].add(tid)
                        seen_edges[pr].add(edge["cursor"])
                        before = edge["cursor"]
                    count = len(seen_threads[pr])
                    if more:
                        if count >= total:
                            raise ValueError("inconsistent thread continuation")
                        cursors[pr] = cursor
                        remaining.append(pr)
                    elif count == total:
                        results[pr] = accumulated[pr]
                except _FAILURES:
                    continue
            pending = remaining
            if not pending:
                break
    return results
