import datetime as dt
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

P = Path(__file__).parents[1] / "scripts" / "komga-home-deploy.py"
spec = importlib.util.spec_from_file_location("komga_deploy", P)
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)

COMMIT = "b" * 40
OLD = b"verified existing jar"
NEW = b"verified new jar"
OLD_SHA = hashlib.sha256(OLD).hexdigest()
NEW_SHA = hashlib.sha256(NEW).hexdigest()
PUBLISHED = "2026-11-23T00:01:02Z"
INSIDE_KST = dt.datetime(2026, 11, 23, 18, 35, tzinfo=dt.timezone.utc)
OUTSIDE_KST = dt.datetime(2026, 11, 23, 20, 0, tzinfo=dt.timezone.utc)


class Feed:
    def __init__(self, blob=NEW, commit=COMMIT, metadata=None):
        self.blob = blob
        self.commit = commit
        self.calls = []
        self.metadata = metadata or {}

    def latest(self):
        self.calls.append("list")
        return {"published_at": PUBLISHED}, COMMIT, {"komga.jar": "jar", "deploy-manifest.json": "manifest"}

    def asset(self, asset, maximum, out):
        self.calls.append(asset)
        data = self.blob if asset == "jar" else json.dumps(
            {"schema": 1, "service": "komga", "commit": self.commit, "sha256": NEW_SHA, **self.metadata}
        ).encode()
        out.write(data)
        return hashlib.sha256(data).hexdigest()


class TestDeploy(unittest.TestCase):
    def setUp(self):
        self.holder = tempfile.TemporaryDirectory()
        self.addCleanup(self.holder.cleanup)
        self.base = Path(self.holder.name)
        self.cfg = {
            "deploy_directory": str(self.base),
            "health_url": "http://127.0.0.1:25600/ebook/actuator/health",
            "restart_command": ["/usr/bin/false"],
            "service_java_executable": "/usr/bin/java",
            "minimum_java_major": 25,
            "approved_transitions": [{
                "from_sha256": OLD_SHA, "to_commit": COMMIT, "to_sha256": NEW_SHA, "rollback_compatible": True,
            }],
        }
        self.local = d.Local(self.cfg)
        self.local.current.write_bytes(OLD)

    def run_apply(self, gh, checker, now=INSIDE_KST):
        with patch.object(d, "check_runtime"), patch.object(d, "health", return_value=True):
            d.reconcile(self.local, True, gh, lambda: now, checker)

    def test_adopt_dry_run_and_reconcile_dry_run_leave_old_bytes_identity(self):
        inode = self.local.current.stat().st_ino
        d.adopt(self.local, OLD_SHA, False)
        self.assertEqual(self.local.current.read_bytes(), OLD)
        self.assertEqual(self.local.current.stat().st_ino, inode)
        self.assertFalse(self.local.current.is_symlink())
        self.assertFalse(self.local.status.exists())
        d.adopt(self.local, OLD_SHA, True)
        link = self.local.link_value()
        state = self.local.status.read_bytes()
        inode = self.local.current.stat().st_ino
        d.reconcile(self.local, False, Feed(), lambda: INSIDE_KST,
                    lambda _: self.fail("dry run attempted restart"))
        self.assertEqual(state, self.local.status.read_bytes())
        self.assertEqual(inode, self.local.current.stat().st_ino)
        self.assertEqual(link, self.local.link_value())
        self.assertEqual(OLD, self.local.current.read_bytes())
        self.assertFalse((self.local.releases / COMMIT).exists())

    def test_incorrect_baseline_never_overwritten(self):
        with self.assertRaisesRegex(d.DeployError, "baseline SHA-256"):
            d.adopt(self.local, NEW_SHA, True)
        self.assertEqual(self.local.current.read_bytes(), OLD)
        self.assertFalse(self.local.status.exists())

    def test_verified_apply(self):
        d.adopt(self.local, OLD_SHA, True)
        checked = []
        self.run_apply(Feed(), lambda _: checked.append(self.local.current.read_bytes()) or True)
        self.assertEqual(checked, [NEW])
        state = self.local.read()
        self.assertEqual(state["active"]["key"], COMMIT)
        self.assertIsNone(state["pending"])

    def test_health_failure_uses_local_previous_bits_and_remembers_failed(self):
        d.adopt(self.local, OLD_SHA, True)
        gh = Feed()
        checks = []
        def checker(_):
            checks.append(self.local.current.read_bytes())
            if len(checks) == 1:
                self.assertIsNotNone(self.local.read()["pending"])
                return False
            self.assertEqual(gh.calls, ["list", "manifest", "jar"])
            return True
        with self.assertRaisesRegex(d.DeployError, "candidate health check failed"):
            self.run_apply(gh, checker)
        self.assertEqual(checks, [NEW, OLD])
        state = self.local.read()
        self.assertEqual(state["failed"], {COMMIT: NEW_SHA})
        self.assertIsNone(state["pending"])
        self.assertEqual(state["active"]["sha256"], OLD_SHA)
        gh.calls.clear()
        self.run_apply(gh, lambda _: self.fail("unhealthy candidate was retried"))
        self.assertEqual(gh.calls, ["list"])

    def test_interrupt_after_swap_recovers_without_feed_or_window(self):
        d.adopt(self.local, OLD_SHA, True)
        class Interruption(BaseException):
            pass
        def kill(_):
            self.assertEqual(self.local.current.read_bytes(), NEW)
            raise Interruption()
        with self.assertRaises(Interruption):
            self.run_apply(Feed(), kill)
        self.assertIsNotNone(self.local.read()["pending"])
        class Offline:
            def latest(self):
                self.fail("rollback contacted GitHub")
        d.reconcile(self.local, True, Offline(), lambda: OUTSIDE_KST,
                    lambda _: self.local.current.read_bytes() == OLD)
        self.assertEqual(self.local.read()["failed"][COMMIT], NEW_SHA)
        self.assertIsNone(self.local.read()["pending"])

    def test_invalid_hash_window_and_compatibility_do_not_swap(self):
        d.adopt(self.local, OLD_SHA, True)
        def restart(_):
            self.fail("unchecked candidate attempted restart")
        with self.assertRaisesRegex(d.DeployError, "JAR hash"):
            self.run_apply(Feed(b"tampered"), restart)
        with self.assertRaisesRegex(d.DeployError, "deployment manifest"):
            self.run_apply(Feed(commit="f" * 40), restart)
        gh = Feed()
        self.run_apply(gh, restart, OUTSIDE_KST)
        self.assertEqual(gh.calls, [])
        self.cfg["approved_transitions"] = []
        self.run_apply(gh, restart)
        self.assertEqual(gh.calls, ["list", "manifest"])
        self.assertEqual(self.local.current.read_bytes(), OLD)

    def test_apply_preflight_and_window_rechecked_after_download(self):
        d.adopt(self.local, OLD_SHA, True)
        with patch.object(d, "check_runtime", side_effect=d.DeployError("unavailable")):
            with self.assertRaisesRegex(d.DeployError, "unavailable"):
                d.reconcile(self.local, True, Feed(), lambda: INSIDE_KST)
        times = iter([INSIDE_KST, OUTSIDE_KST])
        with patch.object(d, "check_runtime"), patch.object(d, "health", return_value=True):
            d.reconcile(self.local, True, Feed(), lambda: next(times),
                        lambda _: self.fail("restart after maintenance window"))
        self.assertEqual(self.local.current.read_bytes(), OLD)
        self.assertIsNone(self.local.read()["pending"])

    def test_rollback_failure_journal_stays(self):
        d.adopt(self.local, OLD_SHA, True)
        with self.assertRaisesRegex(d.DeployError, "offline rollback did not"):
            self.run_apply(Feed(), lambda _: False)
        self.assertIsNotNone(self.local.read()["pending"])
        self.assertEqual(self.local.current.read_bytes(), OLD)

    def test_lock_rejects_concurrent_reconciliation(self):
        import os
        lock = self.local.open_lock(True)
        self.addCleanup(os.close, lock)
        with self.assertRaisesRegex(d.DeployError, "another reconciliation"):
            self.local.open_lock(True)

    def test_automatic_requires_exact_predecessor_and_local_backup(self):
        previous = "a" * 40
        d.adopt(self.local, OLD_SHA, True, previous)
        self.cfg["approved_transitions"] = []
        self.cfg["auto_rollback_safe"] = True
        self.cfg["backup_precheck_command"] = ["/usr/bin/true"]
        mismatch = Feed(metadata={"rollback_safe": True, "previous_commit": "e" * 40})
        def no_restart(_):
            self.fail("unapproved automatic release attempted restart")
        with patch.object(d, "run_local_hook", side_effect=AssertionError("ineligible backup started")):
            self.run_apply(mismatch, no_restart)
            self.run_apply(Feed(), no_restart)
        feed = Feed(metadata={"rollback_safe": True, "previous_commit": previous})
        with patch.object(d, "run_local_hook", return_value=False):
            with self.assertRaisesRegex(d.DeployError, "local backup/precheck"):
                self.run_apply(feed, no_restart)
        self.assertEqual(self.local.current.read_bytes(), OLD)
        with patch.object(d, "run_local_hook", return_value=True):
            checks = []
            with self.assertRaisesRegex(d.DeployError, "candidate health check failed"):
                self.run_apply(feed, lambda _: checks.append(self.local.current.read_bytes()) or len(checks) == 2)
        self.assertEqual(checks, [NEW, OLD])
        self.assertEqual(self.local.read()["failed"], {COMMIT: NEW_SHA})

    def test_cli_defaults_to_dry_run_without_persisting_lock_or_state(self):
        cfg = self.base / "deploy.json"
        cfg.write_text(json.dumps(self.cfg), encoding="utf-8")
        command = ["--config", str(cfg)]
        self.assertEqual(d.main(command + ["adopt", "--expect-sha256", OLD_SHA]), 0)
        self.assertFalse(self.local.lock.exists())
        self.assertFalse(self.local.status.exists())
        self.assertFalse(self.local.current.is_symlink())
        d.adopt(self.local, OLD_SHA, True)
        state = self.local.status.read_bytes()
        with patch.object(d, "GitHub", return_value=Feed()):
            self.assertEqual(d.main(command + ["reconcile"]), 0)
        self.assertFalse(self.local.lock.exists())
        self.assertEqual(self.local.status.read_bytes(), state)
        self.assertEqual(self.local.current.read_bytes(), OLD)

    def test_release_selection_validates_the_tag_and_ignores_draft(self):
        gh = d.GitHub()
        release = {
            "tag_name": "komga-home-" + COMMIT,
            "draft": False, "prerelease": False, "published_at": PUBLISHED, "id": 4,
            "assets": [{"name": "komga.jar"}, {"name": "deploy-manifest.json"}],
        }
        draft = {**release, "draft": True, "id": 5, "tag_name": "komga-home-" + "a" * 40}
        ref = {"ref": "refs/tags/" + release["tag_name"], "object": {"type": "commit", "sha": COMMIT}}
        with patch.object(gh, "json", side_effect=[[draft, release], ref]):
            selected = gh.latest()
        self.assertEqual(selected[1], COMMIT)
        ref["object"]["sha"] = "a" * 40
        with patch.object(gh, "json", side_effect=[[release], ref]):
            with self.assertRaisesRegex(d.DeployError, "target its claimed commit"):
                gh.latest()

    def test_redirect_strips_bearer_and_rejects_untrusted_destination(self):
        import urllib.request
        handler = d.GitHubRedirect()
        req = urllib.request.Request(d.API + "/releases/assets/42", headers={"Authorization": "Bearer example"})
        redirected = handler.redirect_request(req, None, 302, "", {}, "https://release-assets.githubusercontent.com/blob-token")
        self.assertIsNone(redirected.get_header("Authorization"))
        with self.assertRaisesRegex(d.DeployError, "unexpected GitHub redirect"):
            handler.redirect_request(req, None, 302, "", {}, "https://example.com/asset")


if __name__ == "__main__":
    unittest.main()
