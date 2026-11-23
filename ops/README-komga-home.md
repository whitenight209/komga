# Komga home CI/CD

- CI: [Komga Home Pipeline](../.github/workflows/komga-home.yml) and [configuration](komga-ci/README.md).
- CD: [home reconciliation, bootstrap, and recovery](komga-cd/README.md).

Start with a dry run. The workflow targets the fork's `master` branch, not upstream, and does not change the running system. The fork keeps its own code changes and build configuration. Check what binary and Java executable the running process actually uses before adopting it. This source compiles to **Java 25**; Java 21 cannot run the new JAR. If the running build cannot be mapped to a trusted source commit and JAR digest, leave automatic promotion disabled.

Pair the CI publication with the home-server setup by reviewing the recorded release SHA and running the dry-run reconciler before enabling any live deployment. Never expose a home-server SSH port just for CI. The public fork's candidate builds run without custom secrets on GitHub-hosted runners, not on a home-server runner. No library data or local service credentials are tracked in Git.

CI considers an exact, validated merge build publishable; that does **not** by itself prove an in-place database rollback is safe. The initial publisher does not claim compatibility. The CD consumer therefore requires approval of the exact transition before applying it, and otherwise only reports candidates. A trusted end-to-end compatibility check with a recoverable backup is required before enabling unattended deployment of those updates. Test on a separate instance before using live data.
