#!/usr/bin/env python3
"""Personal identity cache: no real gh, GitHub, model, or other API calls."""

import contextlib
import io
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
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_worker import cli, github
from tauceti_worker import github_identity as identity
from tauceti_worker.config import Die


def concurrent_lookup(start, results, count):
    start.wait(10)

    def fetch():
        with open(count, "a") as stream:
            stream.write("one personal API call\n")
        time.sleep(0.1)
        return "personal-user"

    try:
        results.put(identity.cached_personal_login(fetch))
    except Exception as exc:
        results.put(type(exc).__name__)


class IdentityCacheTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {"HOME": str(self.root), "GH_TOKEN": "personal-secret-A"}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        # Every test fails closed if it accidentally reaches a subprocess. Tests
        # for local credential retrieval replace this with a strictly checked mock.
        self.no_subprocess = patch.object(
            identity.subprocess, "run", side_effect=AssertionError("unexpected subprocess")
        )
        self.no_subprocess.start()
        self.addCleanup(self.no_subprocess.stop)
        github.me.cache_clear()
        self.addCleanup(github.me.cache_clear)
        identity.enable_identity_cache()
        self.path = Path(os.environ[identity.CACHE_ENV])
        self.fetch = Mock(return_value="personal-user")

    def cached(self):
        return identity.cached_personal_login(self.fetch)

    def write_record(self, record):
        self.path.write_text(json.dumps(record))
        self.path.chmod(0o600)

    def test_enabled_path_is_absolute_shared_after_home_isolation(self):
        self.assertEqual(self.path, self.root / ".cache/tauceti/github-identity/login.json")
        self.cached()
        os.environ["HOME"] = str(self.root / "isolated-worker")
        identity.enable_identity_cache()
        self.assertEqual(os.environ[identity.CACHE_ENV], str(self.path))
        self.assertEqual(self.cached(), "personal-user")
        self.fetch.assert_called_once()
        self.assertFalse(Path(os.environ["HOME"]).exists())

    def test_custom_relative_path_anchored_at_startup(self):
        os.environ[identity.CACHE_ENV] = "relative/private/login.json"
        identity.enable_identity_cache()
        self.assertEqual(os.environ[identity.CACHE_ENV], os.path.abspath("relative/private/login.json"))

    def test_disabled_or_unanchored_never_resolves_credentials(self):
        for configured in (None, "", "off", "relative/login.json"):
            with self.subTest(configured=configured):
                if configured is None:
                    os.environ.pop(identity.CACHE_ENV, None)
                else:
                    os.environ[identity.CACHE_ENV] = configured
                os.environ.pop("GH_TOKEN", None)
                with patch.object(identity, "_credential_key", side_effect=AssertionError("must not look up auth")):
                    self.assertEqual(self.cached(), "personal-user")
        self.assertFalse(self.path.exists())
        os.environ[identity.CACHE_ENV] = "off"
        identity.enable_identity_cache()
        self.assertEqual(os.environ[identity.CACHE_ENV], "off")

    def test_private_cache_and_no_credentials_persisted(self):
        self.assertEqual(self.cached(), "personal-user")
        self.assertEqual(self.cached(), "personal-user")
        self.fetch.assert_called_once()
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        for path in self.path.parent.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            contents = path.read_text()
            self.assertNotIn("personal-secret-A", contents)
            self.assertNotIn("GH_TOKEN", contents)
        record = json.loads(self.path.read_text())
        self.assertEqual(set(record), {"version", "key", "login", "saved_at"})
        self.assertEqual(len(record["key"]), 64)

    def test_token_precedence_rotation_and_host_change(self):
        os.environ["GITHUB_TOKEN"] = "lower-priority-secret"
        self.cached()
        os.environ["GITHUB_TOKEN"] = "changed-but-not-effective"
        self.cached()
        self.fetch.assert_called_once()
        os.environ["GH_TOKEN"] = "personal-secret-B"
        self.fetch.return_value = "second-user"
        self.assertEqual(self.cached(), "second-user")
        os.environ.pop("GH_TOKEN")
        self.fetch.return_value = "third-user"
        self.assertEqual(self.cached(), "third-user")
        os.environ["GH_TOKEN"] = ""  # gh treats empty environment tokens as unset
        self.assertEqual(self.cached(), "third-user")
        self.assertEqual(self.fetch.call_count, 3)
        os.environ["GH_HOST"] = "enterprise.example"
        self.fetch.return_value = "enterprise-user"
        self.assertEqual(self.cached(), "enterprise-user")
        self.assertEqual(self.cached(), "enterprise-user")
        self.assertEqual(self.fetch.call_count, 5)
        self.assertEqual(json.loads(self.path.read_text())["login"], "third-user")
        os.environ["GH_HOST"] = "github.com"
        self.assertEqual(self.cached(), "third-user")

    def test_local_auth_uses_real_gh_fixed_host_and_suppresses_secrets(self):
        os.environ.pop("GH_TOKEN")
        os.environ["TAUCETI_REAL_GH"] = "/fake/real-gh"
        os.environ["GH_DEBUG"] = "api"
        result = subprocess.CompletedProcess([], 0, "local-secret-one\n", "stderr-secret")
        out, err = io.StringIO(), io.StringIO()
        with patch.object(identity.subprocess, "run", return_value=result) as run:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.cached()
                self.cached()
                result.stdout = "local-secret-two\n"
                self.fetch.return_value = "rotated-local-user"
                self.assertEqual(self.cached(), "rotated-local-user")
            self.assertEqual(run.call_count, 5)  # recheck credentials before each of the two saves
            args, kwargs = run.call_args
            self.assertEqual(args[0], ["/fake/real-gh", "auth", "token", "--hostname", "github.com"])
            self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
            self.assertEqual(kwargs["stdout"], subprocess.PIPE)
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertNotIn("GH_DEBUG", kwargs["env"])
        self.assertEqual(self.fetch.call_count, 2)
        self.assertEqual(out.getvalue() + err.getvalue(), "")
        self.assertNotIn("local-secret", self.path.read_text())

    def test_local_auth_path_fallback(self):
        os.environ.pop("GH_TOKEN")
        with patch.object(identity.shutil, "which", return_value="/fake/path-gh") as which:
            with patch.object(
                identity.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "secret")
            ) as run:
                self.cached()
        self.assertEqual(which.call_count, 2)
        which.assert_called_with("gh")
        self.assertEqual(run.call_args.args[0][0], "/fake/path-gh")

    def test_unresolvable_auth_never_reuses_or_saves_cache(self):
        self.cached()
        old_record = self.path.read_bytes()
        os.environ.pop("GH_TOKEN")
        os.environ["TAUCETI_REAL_GH"] = "/fake/real-gh"
        outcomes = [
            subprocess.CompletedProcess([], 1, "secret-on-failure"),
            subprocess.CompletedProcess([], 0, "\n"),
            OSError("a-secret-error"),
            subprocess.TimeoutExpired("secret-command", 5),
        ]
        self.fetch.return_value = "uncached-user"
        for result in outcomes:
            with self.subTest(result=type(result).__name__):
                kwargs = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
                with patch.object(identity.subprocess, "run", **kwargs):
                    self.assertEqual(self.cached(), "uncached-user")
                self.assertEqual(self.path.read_bytes(), old_record)
        self.assertEqual(self.fetch.call_count, 5)

    def test_ttl_and_failed_refresh_never_return_stale_identity(self):
        with patch.object(identity.time, "time", return_value=1000):
            self.cached()
        with patch.object(identity.time, "time", return_value=1000 + identity.CACHE_TTL - 1):
            self.cached()
        self.fetch.assert_called_once()
        self.fetch.side_effect = RuntimeError("personal query failed")
        with patch.object(identity.time, "time", return_value=1000 + identity.CACHE_TTL):
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "personal query failed"):
                    self.cached()
        self.assertEqual(self.fetch.call_count, 3)
        self.fetch.side_effect = None
        self.fetch.return_value = "fresh-user"
        with patch.object(identity.time, "time", return_value=1000 + identity.CACHE_TTL + 1):
            self.assertEqual(self.cached(), "fresh-user")
            self.assertEqual(self.cached(), "fresh-user")
        self.assertEqual(self.fetch.call_count, 4)

    def test_malformed_cache_and_invalid_timestamps_refresh(self):
        self.cached()
        good = json.loads(self.path.read_text())
        variants = [None, [], "a string", {}, {**good, "version": 99}, {**good, "key": "wrong"}]
        variants += [
            {**good, "saved_at": value}
            for value in (None, True, "1000", float("nan"), float("inf"), 10**400, time.time() + 100)
        ]
        variants += [
            {**good, "login": login} for login in ("", "bad\nlogin", "a--b", "a_", "-abc", "abc-", "x" * 40, {}, 7)
        ]
        for record in variants:
            with self.subTest(record=record):
                self.write_record(record)
                self.fetch.reset_mock()
                self.assertEqual(self.cached(), "personal-user")
                self.fetch.assert_called_once()
        for malformed in (b"{", b"\xff\xfe", b"x" * 10000, b"[" * 2000 + b"]" * 2000):
            self.path.write_bytes(malformed)
            self.fetch.reset_mock()
            self.assertEqual(self.cached(), "personal-user")
            self.fetch.assert_called_once()

    def test_auth_rotation_during_fetch_is_not_saved_under_old_credential(self):
        def rotate():
            os.environ["GH_TOKEN"] = "new-effective-token"
            return "new-user"

        self.assertEqual(identity.cached_personal_login(rotate), "new-user")
        self.assertFalse(self.path.exists())
        self.assertEqual(self.cached(), "personal-user")
        self.fetch.assert_called_once()

    def test_invalid_results_and_callback_failures_not_cached(self):
        for login in ("", "hello world", "a--b", "x" * 40, "a\nb", "[bot]"):
            self.fetch.return_value = login
            self.assertEqual(self.cached(), login)
            self.assertFalse(self.path.exists())
        self.fetch.reset_mock()
        error = OSError("query failed")
        self.fetch.side_effect = error
        with self.assertRaises(OSError) as raised:
            self.cached()
        self.assertIs(raised.exception, error)
        self.fetch.assert_called_once()  # filesystem fallback must not retry fetch errors
        self.assertFalse(self.path.exists())

    def test_filesystem_errors_fall_back_to_personal_query(self):
        self.cached()
        self.fetch.return_value = "uncached-user"
        with patch.object(identity, "_locked_directory", side_effect=PermissionError("secret-path")):
            self.assertEqual(self.cached(), "uncached-user")
        os.environ["GH_TOKEN"] = "new-secret"
        with patch.object(identity, "_write_login", side_effect=OSError("disk full")):
            self.assertEqual(self.cached(), "uncached-user")
            self.assertEqual(self.cached(), "uncached-user")
        self.assertEqual(self.fetch.call_count, 4)
        self.assertEqual(json.loads(self.path.read_text())["login"], "personal-user")

    def test_insecure_files_and_directory_are_not_trusted(self):
        self.cached()
        self.fetch.return_value = "uncached-user"
        for path, mode in (
            (self.path, 0o644),
            (self.path.with_name("login.json.lock"), 0o644),
            (self.path.parent, 0o755),
        ):
            path.chmod(mode)
            self.assertEqual(self.cached(), "uncached-user")
            path.chmod(0o700 if path.is_dir() else 0o600)
        self.assertEqual(self.fetch.call_count, 4)

    def test_symlink_and_hardlink_cache_files_never_followed(self):
        self.cached()
        outside = self.root / "outside.json"
        outside.write_bytes(self.path.read_bytes())
        outside.chmod(0o600)
        original = outside.read_bytes()
        self.fetch.return_value = "uncached-user"
        self.path.unlink()
        self.path.symlink_to(outside)
        self.assertEqual(self.cached(), "uncached-user")
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(outside.read_bytes(), original)
        self.path.unlink()
        os.link(outside, self.path)
        self.assertEqual(self.cached(), "uncached-user")
        self.assertEqual(outside.read_bytes(), original)
        self.assertEqual(self.fetch.call_count, 3)

    def test_symlink_directory_and_lock_never_followed(self):
        self.cached()
        self.fetch.return_value = "uncached-user"
        parent = self.path.parent
        moved = parent.with_name("moved")
        parent.rename(moved)
        parent.symlink_to(moved, target_is_directory=True)
        self.assertEqual(self.cached(), "uncached-user")
        parent.unlink()
        moved.rename(parent)
        lock = self.path.with_name("login.json.lock")
        lock.unlink()
        lock.symlink_to(self.path)
        original = self.path.read_bytes()
        self.assertEqual(self.cached(), "uncached-user")
        self.assertEqual(self.path.read_bytes(), original)

    def test_processes_coalesce_one_personal_query(self):
        ctx = multiprocessing.get_context("fork")
        start, results = ctx.Event(), ctx.Queue()
        count = self.root / "query-count"
        processes = [ctx.Process(target=concurrent_lookup, args=(start, results, str(count))) for _ in range(6)]
        try:
            for process in processes:
                process.start()
            start.set()
            self.assertEqual([results.get(timeout=10) for _ in processes], ["personal-user"] * len(processes))
            for process in processes:
                process.join(timeout=10)
                self.assertEqual(process.exitcode, 0)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
            results.close()
        self.assertEqual(count.read_text(), "one personal API call\n")
        self.assertEqual(self.cached(), "personal-user")
        self.fetch.assert_not_called()

    def test_me_preserves_personal_status_check_and_token_rotation(self):
        os.environ["TAUCETI_REAL_GH"] = "/fake/real-gh"
        os.environ["TAUCETI_GITHUB_APP_CONFIG"] = "off"
        response = subprocess.CompletedProcess([], 1, "valid-looking-login", "failed")
        with patch.object(github, "gh_run", return_value=response) as run:
            with self.assertRaises(Die):
                github.me()
            self.assertFalse(self.path.exists())
            response.returncode = 0
            response.stdout = "personal-user\n"
            self.assertEqual(github.me(), "personal-user")
            self.assertEqual(github.me(), "personal-user")
            os.environ["GH_TOKEN"] = "rotated-secret"
            response.stdout = "rotated-user\n"
            self.assertEqual(github.me(), "rotated-user")
            self.assertEqual(run.call_count, 3)
            self.assertEqual(run.call_args.args[0], ["/fake/real-gh", "api", "user", "--jq", ".login"])

    def test_me_library_legacy_cache_never_looks_up_credentials(self):
        os.environ.pop(identity.CACHE_ENV)
        os.environ.pop("GH_TOKEN")
        response = subprocess.CompletedProcess([], 0, "personal-user")
        with patch.object(github, "gh_run", return_value=response) as run:
            self.assertEqual(github.me(), "personal-user")
            self.assertEqual(github.me(), "personal-user")
            run.assert_called_once_with(["gh", "api", "user", "--jq", ".login"])
            github.me.cache_clear()
            self.assertEqual(github.me(), "personal-user")
            self.assertEqual(run.call_count, 2)

    def test_cli_enables_cache_before_app_or_home_isolation(self):
        calls = []
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(cli, "ensure_ssl_cert_file", return_value="/fake/certs"))
            stack.enter_context(patch.object(cli, "_ensure_scripts_executable"))
            stack.enter_context(
                patch.object(cli, "enable_identity_cache", side_effect=lambda: calls.append("identity"))
            )
            stack.enter_context(patch.object(cli, "enable_github_app", side_effect=lambda: calls.append("app")))
            stack.enter_context(patch.object(cli, "main", side_effect=lambda: calls.append("main")))
            self.assertEqual(cli.cli_main(), 0)
        self.assertEqual(calls, ["identity", "app", "main"])


if __name__ == "__main__":
    unittest.main()
