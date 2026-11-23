import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import pipeline


class PipelineTest(unittest.TestCase):
    def test_detects_protected_paths_without_matching_substrings(self):
        with patch.object(pipeline, "git", return_value=b"normal/ops.txt\x00.GitHub/workflows/x.yml\x00a/.gitmodules\x00"):
            self.assertEqual(pipeline.protected_paths("base", "head"),
                             [b".GitHub/workflows/x.yml", b"a/.gitmodules"])

    def test_mandatory_unique_branch_pair(self):
        self.assertEqual(pipeline.candidate_branch("a"*40, "b"*40), "komga-sync/aaaaaaaaaaaa-bbbbbbbbbbbb")
        with self.assertRaises(SystemExit):
            pipeline.candidate_branch("master", "b"*40)

    def test_requires_strict_pr_rules_with_expected_status_sources(self):
        active = [{"type": "pull_request"}, {"type": "required_status_checks", "parameters": {
            "strict_required_status_checks_policy": True,
            "required_status_checks": [{"context": label, "integration_id": 12}
                                       for label in (pipeline.CI_CONTEXT, pipeline.REVIEW_CONTEXT)],
        }}]
        pipeline.require_promotion_rules(active)
        with self.assertRaises(SystemExit):
            pipeline.require_promotion_rules(active[1:])
        active[1]["parameters"]["required_status_checks"][1]["integration_id"] = None
        with self.assertRaises(SystemExit):
            pipeline.require_promotion_rules(active)

    def test_one_inherited_active_workflow_holds_sync(self):
        workflows = [{"state": "active", "path": pipeline.WORKFLOW},
                     {"state": "active", "path": ".github/workflows/release.yml"}]
        with patch.object(pipeline, "repo_api", return_value={"total_count": 2, "workflows": workflows}):
            with self.assertRaises(SystemExit):
                pipeline.require_inherited_workflows_off()
            workflows[1]["state"] = "disabled_manually"
            pipeline.require_inherited_workflows_off()

    def test_heads_up_idempotent_no_exec_eligible_dry_run(self):
        base, upstream = "a" * 40, "b" * 40
        outputs = [base, "", base, "", upstream, type("Result", (), {"returncode": 1})(), "c"*40, b""]
        with patch.dict(os.environ, {"PIPELINE_MODE": "dry-run", "GITHUB_SHA": base}):
            with patch.object(pipeline, "git", side_effect=outputs) as git, patch.object(pipeline, "out"), patch.object(
                pipeline, "repo_api", side_effect=AssertionError("dry run must not call write endpoint")
            ):
                pipeline.sync()
                self.assertFalse(any(call.args[0] in ("push", "merge") for call in git.call_args_list))

    def test_source_identity_pins_head_and_rejects_draft(self):
        base, upstream, head = "a"*40, "b"*40, "c"*40
        repo = {"id": pipeline.REPO_ID}
        pr = {"state": "open", "draft": False,
              "base": {"sha": base, "ref": "master", "repo": repo},
              "head": {"sha": head, "ref": pipeline.candidate_branch(base, upstream), "repo": repo},
              "user": {"login": "github-actions[bot]"}, "body": pipeline.marker(base, upstream)}
        pipeline.verify_pr(pr, base, upstream, head)
        for extra in ({"draft": True}, {"head": {**pr["head"], "sha": "d"*40}},
                      {"base": {**pr["base"], "sha": "d"*40}}):
            with self.assertRaises(SystemExit):
                pipeline.verify_pr({**pr, **extra}, base, upstream, head)

    def test_requires_both_ui_assets_and_hashes_archive_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "komga.jar"
            names = ["META-INF/MANIFEST.MF", "BOOT-INF/lib/dep.jar", "BOOT-INF/classes/Foo.class",
                     "BOOT-INF/classes/public/index.html", "BOOT-INF/classes/public/index-next.html"]
            for all_files in (False, True):
                with zipfile.ZipFile(path, "w") as archive:
                    for name in names if all_files else names[:-1]:
                        archive.writestr(name, b"data")
                if all_files:
                    self.assertRegex(pipeline.build_jar_bytes(path), r"^[a-f0-9]{64}$")
                else:
                    with self.assertRaises(SystemExit):
                        pipeline.build_jar_bytes(path)


if __name__ == "__main__":
    unittest.main()
