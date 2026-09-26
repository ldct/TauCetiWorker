#!/usr/bin/env python3
"""The upstream survey path keeps local fail-closed checks, without nested retries."""

import copy
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker.constants import GH_TRANSIENT_TRIES
from tauceti_worker.github import GitHub, GitHubError
from tauceti_worker.survey import PRInfo

row = {
    "number": 1,
    "title": "PR 1",
    "body": "",
    "headRefOid": "abc",
    "updatedAt": "2026-09-26T00:00:00Z",
    "author": {"login": "bot", "__typename": "Bot"},
    "labels": {"nodes": [{"name": "awaiting-review"}], "totalCount": 1},
    "commits": {
        "nodes": [
            {
                "commit": {
                    "oid": "abc",
                    "status": {
                        "contexts": [{"context": "build", "state": "SUCCESS", "createdAt": "2026-09-06T00:00:00Z"}]
                    },
                }
            }
        ]
    },
}


def page(rows, more=False, cursor=None):
    return subprocess.CompletedProcess(
        [],
        0,
        json.dumps(
            {
                "data": {
                    "repository": {
                        "pullRequests": {"nodes": rows, "pageInfo": {"hasNextPage": more, "endCursor": cursor}}
                    }
                }
            }
        ),
        "",
    )


gh = GitHub("owner/repo")
with patch.object(gh, "_gh", side_effect=[page([row], True, "next"), page([row])]) as call:
    result = gh.open_prs()
    assert len(result) == 1 and call.call_count == 2, "duplicate PRs must not race in concurrent survey"
    query = " ".join(call.call_args_list[0].args[0])
    assert "first:$n" in query and "status{contexts" in query and "updatedAt" in query
    assert "statusCheckRollup" not in query and "checkRuns" not in query
    assert "cursor=next" in call.call_args_list[1].args[0]
info = PRInfo.from_json(result[0])
assert info.build_success and info.author_is_bot and info.build_status_at
assert info.updated_at == row["updatedAt"]

# Exercise the REAL gh_run layer: the old local page retry wrapper multiplied its budget.
for status in (502, 503, 504):
    bad = subprocess.CompletedProcess([], 1, "", f"HTTP {status}: unavailable")
    with (
        patch("tauceti_worker.github.run", side_effect=[page([row], True, "next"), bad, page([])]) as call,
        patch("tauceti_worker.github.time.sleep"),
    ):
        assert len(gh.open_prs()) == 1 and call.call_count == 3
        assert all("cursor=next" in c.args[0] for c in call.call_args_list[1:]), "retry this page only"
    with patch("tauceti_worker.github.run", return_value=bad) as call, patch("tauceti_worker.github.time.sleep"):
        try:
            gh.open_prs()
        except GitHubError as exc:
            assert str(status) in str(exc) and call.call_count == GH_TRANSIENT_TRIES + 1
        else:
            raise AssertionError("retry budget ignored")

bad = subprocess.CompletedProcess([], 1, "", "HTTP 401: unauthorized")
with patch("tauceti_worker.github.run", side_effect=[page([row], True, "next"), bad]) as call:
    try:
        gh.open_prs()
    except GitHubError as exc:
        assert "page=2" in str(exc) and call.call_count == 2
    else:
        raise AssertionError("partial result accepted")

invalid_label = copy.deepcopy(row)
invalid_label["labels"]["totalCount"] = 51
missing_pageinfo = json.loads(page([row]).stdout)
missing_pageinfo["data"]["repository"]["pullRequests"].pop("pageInfo")
partial_errors = json.loads(page([row]).stdout)
partial_errors["errors"] = [{"message": "resource limit"}]
for invalid in (
    subprocess.CompletedProcess([], 0, json.dumps(partial_errors), ""),
    subprocess.CompletedProcess([], 0, json.dumps(missing_pageinfo), ""),
    subprocess.CompletedProcess([], 0, '{"data":null,"errors":[{"message":"resource limit"}]}', ""),
    subprocess.CompletedProcess([], 0, "null", ""),
    page([None]),
    page([invalid_label]),
    page([{**row, "headRefOid": "different"}]),
    page([row], True, None),
    page([row], True, "repeated"),
):
    with patch.object(gh, "_gh", return_value=invalid) as call:
        try:
            gh.open_prs()
        except GitHubError:
            assert call.call_count <= 2, "invalid cursors must fail promptly, not run to the page cap"
        else:
            raise AssertionError("incomplete response accepted")

# Redact before both the transient retry log and the final actionable error.
logs = []
with (
    patch.dict("os.environ", {"GH_TOKEN": "secret-token"}),
    patch("tauceti_worker.github.run", return_value=subprocess.CompletedProcess([], 1, "", "HTTP 504 secret-token")),
    patch("tauceti_worker.github.time.sleep"),
    patch("tauceti_worker.github.log", side_effect=logs.append),
):
    try:
        gh.open_prs()
    except GitHubError as exc:
        logs.append(str(exc))
assert logs and all("secret-token" not in entry for entry in logs)
assert "repo=owner/repo, state=open, page=1, exit=1" in logs[-1]
print("PASS: upstream pagination/freshness, one retry layer, diagnostics and fail-closed response guards")
