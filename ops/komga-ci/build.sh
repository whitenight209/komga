#!/usr/bin/env bash
set -euo pipefail
# Run only on an ephemeral GitHub-hosted runner with no secrets and no cached credentials.
expected=${1:?expected full commit SHA}
mode=${2:-test}
[[ $expected =~ ^[a-f0-9]{40}$ && "$(git rev-parse HEAD)" == "$expected" ]] || {
  echo 'Refusing to build a different commit' >&2; exit 2;
}
[[ $mode == test || $mode == package ]]
# Pristine containers check out nothing beyond this repository; npm and Gradle can
# execute arbitrary upstream code, so this script must never be run on a home runner.
unset GH_TOKEN GITHUB_TOKEN OPENAI_API_KEY
export NODE_OPTIONS=--max-old-space-size=4096
if [[ $mode == test || $mode == package ]]; then
  (cd komga-webui && npm ci && npm run build)
  # The stock UI needs Node 22 and Next UI Node 24. The workflow sets this
  # override explicitly to keep the upstream projects on their own versions.
  if [[ -n ${NODE_NEXT_PATH:-} ]]; then
    (cd next-ui && PATH="$NODE_NEXT_PATH:$PATH" npm ci && PATH="$NODE_NEXT_PATH:$PATH" npm run build:with-i18n)
  else
    (cd next-ui && npm ci && npm run build:with-i18n)
  fi
fi
./gradlew --no-daemon :komga:webuiCopyIndex :komga:nextuiCopyIndex :komga:test :komga:bootJar
shopt -s nullglob
jars=()
for jar in komga/build/libs/komga-*.jar; do
  [[ $jar == *-plain.jar || $jar == *-sources.jar || $jar == *-javadoc.jar ]] || jars+=("$jar")
done
[[ "${#jars[@]}" == 1 && -f "${jars[0]}" && ! -L "${jars[0]}" ]] || {
  echo 'Expected one executable Komga JAR' >&2; exit 2;
}
python3 -c '
from pathlib import Path
import sys
sys.path.insert(0, "ops/komga-ci")
import pipeline
pipeline.build_jar_bytes(Path(sys.argv[1]))
' "${jars[0]}"
if [[ $mode == package ]]; then
  mkdir -p dist
  install -m 0644 "${jars[0]}" dist/komga.jar
fi
