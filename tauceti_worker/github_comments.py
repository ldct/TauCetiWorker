"""Bounded, read-only survey issue-comment batches (not inline review comments).

Only these ten trusted documents may use App authentication. PR numbers and pagination
cursors are variables, never interpolated into a document or accepted as query text.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from datetime import datetime

COMMENTS_BATCH_SIZE = 10
COMMENTS_MAX_PAGES = 100  # per PR; never turn a truncated history into a successful absence
COMMENTS_QUERIES = {
    size: "query($owner:String!,$name:String!,"
    + ",".join(f"$pr{i}:Int!,$cursor{i}:String" for i in range(size))
    + "){repository(owner:$owner,name:$name){"
    + "".join(
        f"pr{i}:pullRequest(number:$pr{i}){{number comments(first:100,after:$cursor{i}){{"
        "totalCount nodes{body updatedAt} pageInfo{hasNextPage endCursor}}}"
        for i in range(size)
    )
    + "}}"
    for size in range(1, COMMENTS_BATCH_SIZE + 1)
}


def _failed_aliases(reply: dict, aliases: set[str], returncode: int) -> set[str]:
    """gh exits nonzero for partial GraphQL errors too. Preserve only unaffected aliases.

    An unpathed/global error (or malformed error envelope) cannot safely be attributed,
    so invalidates this entire response, even if apparently usable data accompanies it.
    """
    errors = reply.get("errors", [])
    if not isinstance(errors, list) or (returncode and not errors):
        return aliases
    failed = set()
    for error in errors:
        path = error.get("path") if isinstance(error, dict) else None
        if (
            not isinstance(path, list)
            or len(path) < 2
            or path[0] != "repository"
            or not isinstance(path[1], str)
            or path[1] not in aliases
        ):
            return aliases
        failed.add(path[1])
    return failed


def _comment_page(node: dict, pr: int) -> tuple[list[dict], int, bool, str | None]:
    """Validate a complete page and project only the REST fields ReviewState consumes."""
    if type(node["number"]) is not int or node["number"] != pr:
        raise ValueError("wrong PR")
    connection = node["comments"]
    total = connection["totalCount"]
    nodes = connection["nodes"]
    page = connection["pageInfo"]
    more, cursor = page["hasNextPage"], page["endCursor"]
    if type(total) is not int or total < 0 or not isinstance(nodes, list) or len(nodes) > 100:
        raise ValueError("invalid connection")
    if type(more) is not bool or (cursor is not None and (not isinstance(cursor, str) or not cursor)):
        raise ValueError("invalid page info")
    if bool(nodes) != bool(cursor) or (more and not nodes):
        raise ValueError("missing or inconsistent continuation")
    comments = []
    for comment in nodes:
        body, updated = comment["body"], comment["updatedAt"]
        if not isinstance(body, str) or not isinstance(updated, str):
            raise ValueError("invalid comment")
        # A missing/bad timestamp must not silently reorder scoreboards.
        if datetime.fromisoformat(updated.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("invalid timestamp")
        comments.append({"body": body, "updated_at": updated})
    return comments, total, more, cursor


def fetch_issue_comments(
    gh: Callable[[list[str]], subprocess.CompletedProcess], repo: str, numbers: list[int]
) -> dict[int, list[dict] | None]:
    """Fully paginate each PR, in batches of at most ten. None is failure, [] is absence.

    Each alias advances independently; a failure discards ALL its accumulated pages,
    but does not discard successful peers. No REST fallback disguises a failed batch.
    """
    numbers = list(dict.fromkeys(numbers))
    results: dict[int, list[dict] | None] = dict.fromkeys(numbers)
    owner, _, name = repo.partition("/")
    for start in range(0, len(numbers), COMMENTS_BATCH_SIZE):
        pending = numbers[start : start + COMMENTS_BATCH_SIZE]
        accumulated: dict[int, list[dict]] = {pr: [] for pr in pending}
        cursors: dict[int, str] = {}
        seen: dict[int, set[str]] = {pr: set() for pr in pending}
        totals: dict[int, int] = {}
        for _ in range(COMMENTS_MAX_PAGES):
            args = [
                "api",
                "graphql",
                "-f",
                f"query={COMMENTS_QUERIES[len(pending)]}",
                "-f",
                f"owner={owner}",
                "-f",
                f"name={name}",
            ]
            for i, pr in enumerate(pending):
                args.extend(["-F", f"pr{i}={pr}"])
                if pr in cursors:
                    args.extend(["-f", f"cursor{i}={cursors[pr]}"])
            try:
                response = gh(args)
                reply = json.loads(response.stdout)
                if not isinstance(reply, dict):
                    break
                failed = _failed_aliases(reply, {f"pr{i}" for i in range(len(pending))}, response.returncode)
                repository = reply["data"]["repository"]
                if not isinstance(repository, dict):
                    break
            except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
                break
            remaining = []
            for i, pr in enumerate(pending):
                alias = f"pr{i}"
                if alias in failed:
                    continue
                try:
                    comments, total, more, cursor = _comment_page(repository[alias], pr)
                    if cursor is not None and cursor in seen[pr]:
                        raise ValueError("repeated continuation")
                    if total != totals.setdefault(pr, total):
                        raise ValueError("comment count changed during pagination")
                    accumulated[pr].extend(comments)
                    count = len(accumulated[pr])
                    if more:
                        if count >= total:
                            raise ValueError("inconsistent or repeated continuation")
                        seen[pr].add(cursor)
                        cursors[pr] = cursor
                        remaining.append(pr)
                    elif count == total:
                        results[pr] = accumulated[pr]
                    # Otherwise a truncated connection remains None, never partial success.
                except (ValueError, KeyError, TypeError):
                    continue
            pending = remaining
            if not pending:
                break
    return results
