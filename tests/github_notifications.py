#!/usr/bin/env python3
"""Shared notification cache: strictly offline, including concurrent processes."""

import json
import multiprocessing
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import github
from tauceti_worker import github_identity as identity
from tauceti_worker import github_notifications as notifications

REPO = "TauCetiProject/TauCeti"


def cp(stdout="", rc=0, stderr=""):
    return subprocess.CompletedProcess(["fake-gh"], rc, stdout, stderr)


def thread(pr=42, id="123", repo=REPO, kind="PullRequest"):
    return {"id": id, "subject": {"type": kind, "url": f"https://api.github.com/repos/{repo}/pulls/{pr}"}}


def concurrent_review(start, results, calls, missing):
    def lookup(*args):
        with open(calls, "a") as stream:
            stream.write("lookup\n")
        time.sleep(0.1)
        return cp(json.dumps([] if missing else [thread(42), thread(43, "124")]))

    def write(*args):
        with open(calls, "a") as stream:
            stream.write("write\n")
        return cp('{"ignored":true}')

    start.wait(10)
    with (
        patch.object(github.GitHub, "_gh", side_effect=lookup),
        patch.object(github, "run", side_effect=write),
        patch.object(identity.subprocess, "run", side_effect=AssertionError("unexpected subprocess")),
        patch.object(github, "log"),
    ):
        results.put(github.GitHub(REPO).ignore_pr_notifications(42))


class NotificationCacheTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict(os.environ, {"HOME": str(self.root), "GH_TOKEN": "personal-secret-A"}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        identity.enable_identity_cache()
        self.anchor = Path(os.environ[identity.CACHE_ENV])
        self.key = identity._credential_key()
        self.path = self.anchor.with_name(self.anchor.name + ".notifications-" + self.key + ".json")
        for patcher in (
            patch.object(identity.subprocess, "run", side_effect=AssertionError("unexpected subprocess")),
            patch.object(github, "log"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.lookup = patch.object(github.GitHub, "_gh", return_value=cp(json.dumps([thread()]))).start()
        self.write = patch.object(github, "run", return_value=cp('{"ignored":true}')).start()
        self.addCleanup(patch.stopall)

    def mute(self, pr=42, repo=REPO):
        # Deliberately create new instances: persistence must not depend on one
        # worker instance, an in-memory LRU, or a single round.
        return github.GitHub(repo).ignore_pr_notifications(pr)

    def save(self, record):
        self.path.write_text(json.dumps(record))
        self.path.chmod(0o600)

    def test_shared_success_never_suppresses_later_write(self):
        self.assertTrue(self.mute())
        os.environ["HOME"] = str(self.root / "isolated-worker")
        identity.enable_identity_cache()
        self.assertTrue(self.mute())
        self.assertEqual(self.lookup.call_count, 1)
        self.assertEqual(self.write.call_count, 2)
        self.assertFalse(Path(os.environ["HOME"]).exists())
        self.assertFalse(self.anchor.exists())  # separate from the identity record
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        for path in self.path.parent.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertNotIn("personal-secret-A", path.read_text())
        record = json.loads(self.path.read_text())
        self.assertEqual(set(record), {"version", "key", "threads", "refreshed"})
        self.assertNotIn("ignored", self.path.read_text())

    def test_all_valid_prs_on_all_fetched_pages_are_cached(self):
        page1 = [thread(1, "101"), thread(2, "102")] + [None] * 98
        page2 = [thread(42), thread(43, "143"), thread(44, "144"), thread(45, "bad")]
        self.lookup.side_effect = [cp(json.dumps(page1)), cp(json.dumps(page2))]
        self.assertTrue(self.mute())
        for pr in (1, 2, 43, 44):
            self.assertTrue(self.mute(pr))
        self.assertFalse(self.mute(45))
        self.assertEqual(self.lookup.call_count, 2)
        self.assertEqual(self.write.call_count, 5)
        self.assertEqual(len(json.loads(self.path.read_text())["threads"]), 5)

    def test_repository_isolation_and_case_normalization(self):
        self.assertTrue(self.mute())
        self.assertTrue(self.mute(repo=REPO.lower()))
        self.assertFalse(self.mute(repo="other/repo"))
        self.assertEqual(self.lookup.call_count, 2)
        self.assertEqual(self.write.call_count, 2)

    def test_credentials_and_hosts_are_isolated(self):
        self.assertTrue(self.mute())
        os.environ["GH_TOKEN"] = "personal-secret-B"
        self.lookup.return_value = cp(json.dumps([thread(id="456")]))
        self.assertTrue(self.mute())
        self.assertIn("/456/", self.write.call_args.args[0][4])
        os.environ["GH_TOKEN"] = "personal-secret-A"
        self.assertTrue(self.mute())
        self.assertIn("/123/", self.write.call_args.args[0][4])
        self.assertEqual(self.lookup.call_count, 2)
        for _ in range(2):
            with patch.dict(os.environ, {"GH_HOST": "enterprise.example"}):
                self.assertTrue(self.mute())
                self.assertIn("/456/", self.write.call_args.args[0][4])
        self.assertEqual(self.lookup.call_count, 4)
        self.assertEqual(len(list(self.path.parent.glob("*.json"))), 2)

    def test_thread_ttl_is_not_extended_by_hits(self):
        with patch.object(notifications.time, "time", return_value=1000):
            self.assertTrue(self.mute())
        with patch.object(notifications.time, "time", return_value=1000 + notifications.THREAD_TTL - 1):
            self.assertTrue(self.mute())
            self.assertEqual(self.lookup.call_count, 1)
        with patch.object(notifications.time, "time", return_value=1000 + notifications.THREAD_TTL):
            self.assertTrue(self.mute())
            self.assertEqual(self.lookup.call_count, 2)
        self.assertEqual(self.write.call_count, 3)

    def test_shared_negative_refresh_ttl_across_different_prs(self):
        self.lookup.return_value = cp("[]")
        with patch.object(notifications.time, "time", return_value=1000):
            self.assertFalse(self.mute(42))
        self.lookup.return_value = cp(json.dumps([thread(43)]))
        with patch.object(notifications.time, "time", return_value=1059):
            self.assertFalse(self.mute(43))
            self.assertEqual(self.lookup.call_count, 1)
        with patch.object(notifications.time, "time", return_value=1060):
            self.assertTrue(self.mute(43))
            self.assertEqual(self.lookup.call_count, 2)
        self.assertEqual(self.write.call_count, 1)

    def test_successful_scan_also_throttles_missing_prs(self):
        self.assertTrue(self.mute())
        self.assertFalse(self.mute(99))
        self.assertEqual(self.lookup.call_count, 1)
        self.assertEqual(self.write.call_count, 1)

    def test_bounded_missing_lookup_shared_across_reviews(self):
        self.lookup.return_value = cp(json.dumps([thread(1)] * 100))
        for pr in (42, 43, 44):
            self.assertFalse(self.mute(pr))
        self.assertEqual(self.lookup.call_count, 3)
        self.write.assert_not_called()

    def test_failed_scan_keeps_prior_page_mappings_and_short_backoff(self):
        self.lookup.side_effect = [cp(json.dumps([thread(1)] * 100)), cp(rc=1, stderr="HTTP 403")]
        self.assertFalse(self.mute())
        self.assertFalse(self.mute(43))
        self.assertTrue(self.mute(1))
        self.assertEqual(self.lookup.call_count, 2)
        self.assertEqual(self.write.call_count, 1)

    def test_404_evicts_only_target_without_immediate_scan_or_retry(self):
        self.lookup.return_value = cp(json.dumps([thread(), thread(43, "124")]))
        self.assertTrue(self.mute())
        self.write.return_value = cp(rc=1, stderr="gh: Not Found (HTTP 404)")
        self.assertFalse(self.mute())
        self.assertEqual(self.lookup.call_count, 1)
        self.assertEqual(self.write.call_count, 2)
        record = json.loads(self.path.read_text())
        self.assertEqual(len(record["threads"]), 1)
        self.assertEqual(record["refreshed"], {})
        self.lookup.return_value = cp(json.dumps([thread(id="456")]))
        self.write.return_value = cp('{"ignored":true}')
        self.assertTrue(self.mute())
        self.assertIn("/456/", self.write.call_args.args[0][4])
        self.assertTrue(self.mute(43))
        self.assertIn("/124/", self.write.call_args.args[0][4])
        self.assertEqual(self.lookup.call_count, 2)
        self.assertEqual(self.write.call_count, 4)

    def test_cached_writes_require_strict_confirmation_and_never_retry(self):
        self.assertTrue(self.mute())
        responses = [
            cp(""),
            cp("invalid"),
            cp("null"),
            cp("[]"),
            cp("{}"),
            cp('{"ignored":false}'),
            cp('{"ignored":"true"}'),
            cp('{"ignored":1}'),
            cp(rc=1, stderr="HTTP 403 secondary rate limit"),
            cp(rc=1, stderr="HTTP 429"),
            cp(rc=1, stderr="HTTP 504"),
            OSError("write failed"),
        ]
        for response in responses:
            with self.subTest(response=response):
                self.write.side_effect = [response]
                self.assertFalse(self.mute())
        self.assertEqual(self.lookup.call_count, 1)
        self.assertEqual(self.write.call_count, 1 + len(responses))

    def test_cache_entries_bounded_and_expired_entries_pruned(self):
        with patch.object(notifications.time, "time", return_value=1000):
            with notifications.notification_threads() as cache:
                for pr in range(2010):
                    cache.remember(REPO, pr, str(pr + 1))
                    cache.mark_refreshed(f"owner/repo{pr}")
        record = json.loads(self.path.read_text())
        self.assertEqual(len(record["threads"]), notifications.MAX_ENTRIES)
        self.assertEqual(len(record["refreshed"]), notifications.MAX_ENTRIES)
        with patch.object(notifications.time, "time", return_value=1000 + notifications.THREAD_TTL):
            with notifications.notification_threads() as cache:
                self.assertEqual(cache.threads, {})
                self.assertEqual(cache.refreshed, {})
                cache.remember(REPO, 42, "123")
        self.assertEqual(len(json.loads(self.path.read_text())["threads"]), 1)

    def test_corrupt_cache_recovers_and_future_timestamps_are_rejected(self):
        self.assertTrue(self.mute())
        for data in ("invalid", "[]", "null", '{"version":1}', "[" * 2000, "x" * (notifications._MAX_BYTES + 1)):
            with self.subTest(data=data[:20]):
                self.path.write_text(data)
                before = self.lookup.call_count
                self.assertTrue(self.mute())
                self.assertEqual(self.lookup.call_count, before + 1)
        record = json.loads(self.path.read_text())
        for saved in (time.time() + 10000, float("nan"), float("inf"), True, "123", 10**1000):
            record["threads"][REPO.lower() + "/pulls/42"]["saved_at"] = saved
            record["refreshed"][REPO.lower()] = saved
            self.save(record)
            before = self.lookup.call_count
            self.assertTrue(self.mute())
            self.assertEqual(self.lookup.call_count, before + 1)

    def test_invalid_cached_ids_and_mismatched_fingerprint_never_used(self):
        self.assertTrue(self.mute())
        record = json.loads(self.path.read_text())
        for id in ("../bad", "", True, None, "١٢٣", "1" * 31):
            record["threads"][REPO.lower() + "/pulls/42"]["id"] = id
            record["refreshed"] = {}
            self.save(record)
            before = self.lookup.call_count
            self.assertTrue(self.mute())
            self.assertEqual(self.lookup.call_count, before + 1)
        record["key"] = "another-account"
        self.save(record)
        before = self.lookup.call_count
        self.assertTrue(self.mute())
        self.assertEqual(self.lookup.call_count, before + 1)

    def test_disabled_unanchored_and_unavailable_credential_fall_back(self):
        for configured in (None, "", "off", "relative/login.json"):
            if configured is None:
                os.environ.pop(identity.CACHE_ENV, None)
            else:
                os.environ[identity.CACHE_ENV] = configured
            with patch.object(identity, "_credential_key", side_effect=AssertionError("must not resolve credentials")):
                self.assertTrue(self.mute())
                self.assertTrue(self.mute())
        os.environ[identity.CACHE_ENV] = str(self.anchor)
        with patch.object(identity, "_credential_key", return_value=None):
            self.assertTrue(self.mute())
            self.assertTrue(self.mute())
        self.assertEqual(self.lookup.call_count, 10)
        self.assertEqual(self.write.call_count, 10)
        self.assertFalse(self.path.exists())

    def test_invalid_absolute_cache_paths_fall_back(self):
        regular_file = self.root / "not-a-directory"
        regular_file.write_text("keep")
        for configured in ("/", str(regular_file / "login.json"), str(self.root / ("x" * 250))):
            with patch.dict(os.environ, {identity.CACHE_ENV: configured}):
                self.assertTrue(self.mute())
        self.assertEqual(self.lookup.call_count, 3)
        self.assertEqual(self.write.call_count, 3)
        self.assertEqual(regular_file.read_text(), "keep")

    def test_personal_rest_routing_unchanged_on_cache_hit(self):
        from tauceti_worker.github_app import public_read

        self.assertTrue(self.mute())
        self.assertTrue(self.mute())
        read_args = self.lookup.call_args.args[0]
        self.assertFalse(public_read(read_args, [REPO]))
        self.assertEqual(
            read_args, ["api", "--method", "GET", f"/repos/{REPO}/notifications?all=true&per_page=100&page=1"]
        )
        for call in self.write.call_args_list:
            write_args = call.args[0]
            self.assertEqual(
                write_args,
                ["gh", "api", "--method", "PUT", "/notifications/threads/123/subscription", "-F", "ignored=true"],
            )
            self.assertFalse(public_read(write_args[1:], [REPO]))
            self.assertFalse(github._gh_read_only(write_args))
        self.assertEqual(self.lookup.call_count, 1)
        self.assertEqual(self.write.call_count, 2)

    def test_filesystem_failures_do_not_repeat_operations(self):
        for where in ("lock", "read", "write"):
            target = {
                "lock": (identity, "_locked_directory"),
                "read": (notifications, "_read"),
                "write": (notifications, "_write"),
            }[where]
            with patch.object(*target, side_effect=OSError("unavailable secret path")):
                self.assertTrue(self.mute())
                self.assertTrue(self.mute())
        self.assertEqual(self.lookup.call_count, 6)
        self.assertEqual(self.write.call_count, 6)
        self.assertFalse(self.path.exists())

    def test_insecure_files_directory_and_links_are_not_used(self):
        self.assertTrue(self.mute())
        for path, mode in (
            (self.path, 0o644),
            (self.path.with_name(self.path.name + ".lock"), 0o644),
            (self.path.parent, 0o755),
        ):
            path.chmod(mode)
            before = self.lookup.call_count
            self.assertTrue(self.mute())
            self.assertEqual(self.lookup.call_count, before + 1)
            path.chmod(0o700 if path.is_dir() else 0o600)
        outside = self.root / "outside.json"
        outside.write_bytes(self.path.read_bytes())
        outside.chmod(0o600)
        original = outside.read_bytes()
        for link in ("symlink", "hardlink"):
            self.path.unlink()
            if link == "symlink":
                self.path.symlink_to(outside)
            else:
                os.link(outside, self.path)
            before = self.lookup.call_count
            self.assertTrue(self.mute())
            self.assertEqual(self.lookup.call_count, before + 1)
            self.assertEqual(outside.read_bytes(), original)

    def test_credential_rotation_during_lookup_is_not_persisted(self):
        def rotate(*args):
            os.environ["GH_TOKEN"] = "personal-secret-B"
            return cp(json.dumps([thread()]))

        self.lookup.side_effect = rotate
        self.assertTrue(self.mute())
        self.assertFalse(self.path.exists())
        self.assertEqual(self.lookup.call_count, 1)
        self.assertEqual(self.write.call_count, 1)

    def test_multiprocess_cold_and_negative_cache_prevent_stampedes(self):
        context = multiprocessing.get_context("fork")
        for missing in (False, True):
            with self.subTest(missing=missing):
                os.environ["GH_TOKEN"] = f"concurrent-token-{missing}"
                calls = self.root / f"calls-{missing}"
                start, results = context.Event(), context.Queue()
                processes = [
                    context.Process(target=concurrent_review, args=(start, results, calls, missing)) for _ in range(4)
                ]
                for process in processes:
                    process.start()
                start.set()
                try:
                    self.assertEqual([results.get(timeout=10) for _ in processes], [not missing] * 4)
                    for process in processes:
                        process.join(10)
                        self.assertEqual(process.exitcode, 0)
                    operations = calls.read_text().splitlines()
                    self.assertEqual(operations.count("lookup"), 1)
                    self.assertEqual(operations.count("write"), 0 if missing else 4)
                finally:
                    for process in processes:
                        if process.is_alive():
                            process.terminate()
                        process.join(10)
                    results.close()


if __name__ == "__main__":
    unittest.main()
