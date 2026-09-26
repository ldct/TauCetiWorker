#!/usr/bin/env python3
"""Progress refreshes git source without hydrating Mathlib or touching Lake caches."""

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import agents, work_recovery

with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ):
    checkout = Path(tmp) / "checkout"
    (checkout / ".git").mkdir(parents=True)
    cfg = SimpleNamespace(checkout=checkout, state=Path(tmp) / "state")
    with (
        patch.object(agents, "sync_mathlib_pool") as pool,
        patch.object(agents, "clean_lake_cache_after_toolchain_bump") as cache,
        patch.object(agents.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=b"")) as run,
        patch.object(agents, "repair_origin", return_value=True),
        patch.object(work_recovery, "guard", return_value=SimpleNamespace(ok=True, state="clean")),
        patch.object(work_recovery, "detach_handoff_owner"),
        patch.object(work_recovery, "note_owner"),
    ):
        assert agents.prepare_checkout(cfg, source_only=True)
        pool.assert_not_called()
        cache.assert_not_called()
        assert all(call.args[0][0] == "git" for call in run.call_args_list)
        assert any("fetch" in call.args[0] for call in run.call_args_list)
        assert agents.prepare_checkout(cfg)
        pool.assert_called_once_with(cfg)
        cache.assert_called_once_with(cfg)
print("[OK ] progress source-only checkout skips build caches; authoring retains them")
