# GitHub setup

This workflow is scoped to `whitenight209/komga:master` and reads `gotson/komga:master`. It runs each day at 03:23 Asia/Seoul, or manually from the default branch after the workflow is merged. Start with no settings changes: the default mode only examines Git history and prints the decision, without creating or changing any remote resources. A changed upstream workflow, operational policy or Git metadata is held instead of importing it automatically. After enabling PR mode, protected-path holds create report-only draft PRs that must not be merged as updates.

## Enable incrementally

1. Verify the currently deployed fork commit and independently identify its JAR digest. Set `KOMGA_DEPLOYED_BASELINE` to that full fork commit SHA. Do not guess the commit from the version string.
2. Disable and audit pre-existing workflows in the **fork**, especially inherited workflows that use upstream dispatch, release or notification hooks. Do this before enabling PR/promote mode. The pipeline fails closed if another workflow remains active. Permit the workflow only to create same-repository PRs; no admin bypass. Changes in `.github`, `ops`, or Git configuration surfaces require manual review.
3. In repository Actions configuration set `KOMGA_PIPELINE_MODE=pr` for non-publishing PR and check validation. Set `KOMGA_CODEX_MODEL` to an available Responses API Codex model supporting strict Structured Outputs and the secret `KOMGA_REVIEW_OPENAI_API_KEY` to a project API key. No home secrets go into this repo. Review receives the exact diff as text, never checks out or executes the candidate, and has no tools. If the API key is unavailable or review fails, it does not approve.
4. Before using `KOMGA_PIPELINE_MODE=promote`, configure an enforced ruleset on `master` that requires pull requests and up-to-date branches with both `komga-home-ci` and `komga-codex-review`, bound to the actual GitHub Actions integration, with no bypass. Enable GitHub merge commits to retain upstream ancestry. Verify eligibility with a PR-only run first. Build jobs are hosted, isolated and receive no custom secrets; publishing uses a separate job and never runs the candidate's code.
5. With `KOMGA_PUBLISH_ENABLED=true`, the exact tested merge SHA can publish a draft-first, non-latest GitHub Release with `komga.jar` and `deploy-manifest.json`. For this **public** fork, release assets will be public; enable only if that is intended. Enable immutable releases where available. If packaging/publication fails after the PR merged, rerun failed jobs on that workflow run so the same verified merge is used. The next daily sync is not a substitute for this retry; the code may already be merged. Inspect partial drafts before retrying. No release is overwritten.

Local checks (from repository root):
```sh
python3 -B -m unittest discover -s ops/komga-ci -q
python3 -B -m unittest discover -s ops/komga-cd/tests -q
bash -n ops/komga-ci/build.sh
actionlint .github/workflows/komga-home.yml
```
