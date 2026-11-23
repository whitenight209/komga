#!/usr/bin/env python3
"""Root-configured, fail-closed Komga release reconciliation."""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

REPO = "whitenight209/komga"
API = "https://api.github.com/repos/" + REPO
TAG = re.compile(r"komga-home-([0-9a-f]{40})\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
ENTRY = re.compile(r"(baseline-[0-9a-f]{64}|[0-9a-f]{40})\Z")
KST = dt.timezone(dt.timedelta(hours=9))
WINDOW_START, WINDOW_END = dt.time(3, 30), dt.time(5)
MAX_JAR_BYTES = 512 * 1024 * 1024


class DeployError(Exception):
    pass


def need(condition, message):
    if not condition:
        raise DeployError(message)


def is_digest(value):
    return isinstance(value, str) and DIGEST.fullmatch(value) is not None


def is_commit(value):
    return isinstance(value, str) and COMMIT.fullmatch(value) is not None


def date(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise DeployError("invalid release publication time")
    return dt.datetime.fromisoformat(value[:-1] + "+00:00")


def sha(path):
    with path.open("rb") as f:
        h = hashlib.sha256()
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
        return h.hexdigest()


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path, value):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".journal.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        fsync_dir(path.parent)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)


def unique_pairs(pairs):
    out = {}
    for key, value in pairs:
        need(key not in out, "duplicate JSON key")
        out[key] = value
    return out


def parse(body):
    return json.loads(body, object_pairs_hook=unique_pairs)


class GitHubRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        need(
            target.scheme == "https" and target.netloc in {
                "release-assets.githubusercontent.com", "objects.githubusercontent.com",
            } and req.full_url.startswith(API + "/releases/assets/"),
            "unexpected GitHub redirect",
        )
        # Never forward API credentials to an asset host.
        return urllib.request.Request(newurl, headers={"User-Agent": "komga-home-deploy/1"})


class GitHub:
    def __init__(self):
        self.token = os.environ.get("GH_TOKEN")
        self.opener = urllib.request.build_opener(GitHubRedirect())

    def get(self, suffix, accept="application/vnd.github+json"):
        headers = {
            "Accept": accept, "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "komga-home-deploy/1",
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        return self.opener.open(urllib.request.Request(API + suffix, headers=headers), timeout=30)

    def json(self, suffix):
        with self.get(suffix) as response:
            body = response.read(4 * 1024 * 1024 + 1)
        need(len(body) <= 4 * 1024 * 1024, "GitHub API response exceeded size limit")
        return parse(body)

    def latest(self):
        releases = self.json("/releases?per_page=50")
        need(isinstance(releases, list), "invalid GitHub releases response")
        releases = [
            r for r in releases
            if isinstance(r, dict) and r.get("draft") is False and r.get("prerelease") is False
            and TAG.fullmatch(r.get("tag_name") or "")
        ]
        if not releases:
            return None
        release = max(releases, key=lambda r: (date(r["published_at"]), r["id"]))
        commit = TAG.fullmatch(release["tag_name"])[1]
        ref = self.json("/git/ref/tags/" + release["tag_name"])
        need(ref.get("ref") == "refs/tags/" + release["tag_name"], "wrong release tag ref")
        obj = ref["object"]
        for _ in range(4):
            if obj.get("type") != "tag":
                break
            need(is_commit(obj.get("sha")), "invalid annotated tag object")
            obj = self.json("/git/tags/" + obj["sha"])["object"]
        need(obj.get("type") == "commit" and obj.get("sha") == commit, "release tag does not target its claimed commit")
        assets = release.get("assets", [])
        need(len(assets) == 2 and {a.get("name") for a in assets} == {"komga.jar", "deploy-manifest.json"},
             "unexpected release assets")
        return release, commit, {a["name"]: a for a in assets}

    def asset(self, asset, maximum, out):
        size, asset_id = asset.get("size"), asset.get("id")
        need(type(size) is int and 0 < size <= maximum, "invalid release asset size")
        need(type(asset_id) is int and asset_id > 0 and asset.get("state") == "uploaded", "invalid release asset ID or state")
        count = 0
        digest = hashlib.sha256()
        with self.get("/releases/assets/" + str(asset_id), "application/octet-stream") as response:
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                count += len(chunk)
                need(count <= size, "GitHub asset exceeded claimed size")
                digest.update(chunk)
                out.write(chunk)
        need(count == size, "truncated GitHub asset")
        upstream = asset.get("digest")
        if upstream:
            need(upstream == "sha256:" + digest.hexdigest(), "GitHub release asset digest mismatch")
        return digest.hexdigest()


class Local:
    def __init__(self, cfg):
        self.base = Path(cfg["deploy_directory"])
        need(self.base.is_absolute() and self.base == self.base.resolve() and self.base.is_dir(),
             "deploy_directory must be an existing non-symlink absolute directory")
        self.releases = self.base / "releases"
        self.current = self.base / "current.jar"
        self.status = self.base / "state.json"
        self.lock = self.base / "deploy.lock"
        self.cfg = cfg

    def open_lock(self, apply):
        if not apply and not self.lock.exists():
            return None
        flags = os.O_RDWR | os.O_CREAT if apply else os.O_RDONLY
        fd = os.open(self.lock, flags | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if apply else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise DeployError("another reconciliation is running")
        return fd

    def read(self):
        need(self.status.is_file() and not self.status.is_symlink(), "no deployment state; adopt the trusted installed JAR first")
        state = parse(self.status.read_bytes())
        need(state.get("schema") == 1 and isinstance(state.get("failed"), dict), "invalid local state")
        for item in [state["active"], state.get("pending", {}).get("from") if state.get("pending") else None,
                     state.get("pending", {}).get("to") if state.get("pending") else None]:
            if item is not None:
                self.validate_entry(item)
        return state

    def validate_entry(self, item):
        need(ENTRY.fullmatch(item.get("key", "")) and is_digest(item.get("sha256")), "invalid local release state")
        if item["key"].startswith("baseline-"):
            need(item["key"] == "baseline-" + item["sha256"], "baseline state mismatch")
            need("source_commit" not in item or is_commit(item["source_commit"]), "invalid baseline source commit")
        else:
            date(item["published_at"])
            if "previous_commit" in item:
                need(is_commit(item["previous_commit"]) and item.get("rollback_safe") is True,
                     "invalid local release compatibility state")

    def save(self, state):
        atomic_json(self.status, state)

    def relpath(self, item):
        self.validate_entry(item)
        return f"releases/{item['key']}/komga.jar"

    def check(self, item):
        jar = self.base / self.relpath(item)
        need(jar == jar.resolve() and jar.is_file() and sha(jar) == item["sha256"], "managed JAR missing or changed")
        return jar

    def link_value(self):
        return os.readlink(self.current) if self.current.is_symlink() else None

    def set_link(self, item):
        self.check(item)
        with tempfile.TemporaryDirectory(dir=self.base, prefix=".switch.") as directory:
            tmp = Path(directory) / "link"
            tmp.symlink_to(self.relpath(item))
            os.replace(tmp, self.current)
            fsync_dir(self.base)

    def stage(self, src, item):
        self.releases.mkdir(mode=0o750, exist_ok=True)
        need(self.releases.resolve() == self.releases, "managed releases cannot be a symlink")
        dest = self.releases / item["key"]
        if dest.exists():
            self.check(item)
            return
        with tempfile.TemporaryDirectory(dir=self.releases, prefix=".stage.") as directory:
            tmp = Path(directory) / "release"
            tmp.mkdir(mode=0o750)
            jar = tmp / "komga.jar"
            with src.open("rb") as in_f, jar.open("xb") as out:
                shutil.copyfileobj(in_f, out)
                out.flush()
                os.fchmod(out.fileno(), 0o640)
                os.fsync(out.fileno())
            need(sha(jar) == item["sha256"], "staged JAR hash mismatch")
            fsync_dir(tmp)
            os.rename(tmp, dest)
            fsync_dir(self.releases)


def config(path):
    need(path.is_absolute() and path.is_file() and not path.is_symlink(), "config must be a local absolute regular file")
    st = path.stat()
    need(st.st_uid in {0, os.geteuid()} and not st.st_mode & 0o022, "config must not be group/world writable")
    cfg = parse(path.read_bytes())
    need(isinstance(cfg, dict), "invalid deployment configuration")
    url = urllib.parse.urlsplit(cfg["health_url"])
    need(url.scheme == "http" and url.hostname in {"127.0.0.1", "::1"} and not url.username
         and not url.password and not url.fragment, "health_url must be an unauthenticated loopback HTTP URL")
    def is_argv(command):
        return (isinstance(command, list) and len(command) > 0
                and all(isinstance(arg, str) and arg and "\0" not in arg for arg in command)
                and Path(command[0]).is_absolute())

    need(is_argv(cfg["restart_command"]), "restart_command must be an explicit absolute argv array")
    auto = cfg.get("auto_rollback_safe", False)
    need(type(auto) is bool, "auto_rollback_safe must be boolean")
    need(not auto or is_argv(cfg.get("backup_precheck_command")),
         "automatic transitions require an explicit absolute backup_precheck_command argv")
    java = cfg["service_java_executable"]
    minimum = cfg["minimum_java_major"]
    need(isinstance(java, str) and Path(java).is_absolute() and type(minimum) is int and minimum >= 21,
         "configure the absolute JVM used by the service and the required Java major version")
    approvals = cfg.get("approved_transitions", [])
    need(isinstance(approvals, list) and all(
        isinstance(a, dict) and is_digest(a.get("from_sha256")) and is_commit(a.get("to_commit"))
        and is_digest(a.get("to_sha256")) and a.get("rollback_compatible") is True for a in approvals
    ), "invalid rollback compatibility approvals")
    return cfg


def report(message):
    print(message, flush=True)


def adopt(local, expected, apply, deployed_commit=None):
    need(is_digest(expected), "provide the independently verified SHA-256 of the installed JAR")
    # Absence of a journal is the only first-time adoption path.
    need(not os.path.lexists(local.status), "already adopted, or local state exists")
    jar = local.current
    need(jar.is_file() and not jar.is_symlink(), "place the trusted installed JAR at current.jar before adopting")
    need(sha(jar) == expected, "installed JAR does not match the expected baseline SHA-256")
    item = {"key": "baseline-" + expected, "sha256": expected}
    if deployed_commit is not None:
        need(is_commit(deployed_commit), "invalid verified baseline source commit")
        item["source_commit"] = deployed_commit
    if not apply:
        report("dry-run: trusted baseline would be adopted; existing JAR was not changed")
        return
    local.stage(jar, item)
    local.save({"schema": 1, "active": item, "failed": {}, "pending": None, "adopting": True})
    local.set_link(item)
    local.save({"schema": 1, "active": item, "failed": {}, "pending": None})
    report("trusted baseline adopted; no restart was performed")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DeployError("health endpoint redirected")


def health(cfg):
    # No GitHub token, redirects, proxy, response logging, or Internet dependency.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(cfg["health_url"], timeout=3) as response:
        data = response.read(16_385)
        return response.status == 200 and len(data) <= 16_384 and parse(data).get("status") == "UP"


def check_runtime(cfg):
    result = subprocess.run([cfg["service_java_executable"], "-XshowSettings:properties", "-version"],
                            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"},
                            stdin=subprocess.DEVNULL, capture_output=True, check=False,
                            timeout=15, text=True, errors="replace")
    version = re.search(r"^\s*java\.specification\.version\s*=\s*(?:1\.)?(\d+)\s*$",
                        result.stdout + "\n" + result.stderr, re.MULTILINE)
    need(result.returncode == 0 and version and int(version[1]) >= cfg["minimum_java_major"],
         "the configured Komga service JVM cannot run this release")


def run_local_hook(command, timeout):
    return subprocess.run(command, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"},
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          check=False, timeout=timeout).returncode == 0


def restart_and_health(cfg):
    from time import monotonic, sleep
    if not run_local_hook(cfg["restart_command"], 45):
        return False
    deadline = monotonic() + 60
    while monotonic() < deadline:
        try:
            if health(cfg):
                return True
        except (OSError, ValueError, TypeError, AttributeError, DeployError):
            pass
        sleep(2)
    return False


def source_commit(item):
    return item["key"] if is_commit(item["key"]) else item.get("source_commit")


def approved(cfg, old, new):
    manual = {
        "from_sha256": old["sha256"], "to_commit": new["key"], "to_sha256": new["sha256"],
        "rollback_compatible": True,
    } in cfg.get("approved_transitions", [])
    if manual:
        return "manual"
    if (
        cfg.get("auto_rollback_safe") is True and new.get("rollback_safe") is True
        and is_commit(new.get("previous_commit")) and source_commit(old) == new["previous_commit"]
    ):
        return "automatic"
    return None


def recover(local, state, apply, checker=restart_and_health):
    if state.get("adopting") is True:
        old = state["active"]
        local.check(old)
        if local.link_value() != local.relpath(old):
            need(local.current.is_file() and not local.current.is_symlink() and sha(local.current) == old["sha256"],
                 "interrupted adoption but original artifact changed")
        if apply:
            local.set_link(old)
            state.pop("adopting")
            local.save(state)
        report("recovered trusted baseline" if apply else "dry-run: an interrupted adoption requires recovery")
        return True
    transaction = state.get("pending")
    if transaction is None:
        return False
    old, new = transaction["from"], transaction["to"]
    # A deploy which reached its durable journal already passed backup when automatic.
    need(state["active"] == old and transaction.get("rollback_authorized") is True
         and approved(local.cfg, old, new), "rollback is not locally approved; review is required")
    actual = local.link_value()
    need(actual in {local.relpath(old), local.relpath(new)}, "JAR link changed outside deployer; recovery stopped")
    local.check(old)
    if not apply:
        report("dry-run: an interrupted deploy requires offline recovery; no change made")
        return True
    state["failed"][new["key"]] = new["sha256"]
    local.save(state)
    # If interrupted again, the transaction remains until the old service is healthy.
    local.set_link(old)
    need(checker(local.cfg), "offline rollback did not become healthy; review the server locally")
    state["pending"] = None
    local.save(state)
    report("candidate stopped; previous JAR restored and healthy")
    return True


def reconcile(local, apply, gh, now, checker=restart_and_health):
    state = local.read()
    if recover(local, state, apply, checker):
        return
    old = state["active"]
    need(local.link_value() == local.relpath(old), "active JAR link changed outside deployer")
    local.check(old)
    window = now().astimezone(KST).time()
    if apply and not (WINDOW_START <= window < WINDOW_END):
        report("outside 03:30–05:00 KST; deployment skipped")
        return
    candidate = gh.latest()
    if candidate is None:
        report("no eligible Komga home release")
        return
    release, commit, assets = candidate
    if commit == source_commit(old):
        report("already up to date")
        return
    if commit in state["failed"]:
        report("latest candidate previously failed; not retrying")
        return
    if "published_at" in old and date(release["published_at"]) <= date(old["published_at"]):
        report("candidate is not newer than deployed release")
        return
    import io
    manifest_body = io.BytesIO()
    gh.asset(assets["deploy-manifest.json"], 16_384, manifest_body)
    manifest = parse(manifest_body.getvalue())
    need(
        isinstance(manifest, dict) and type(manifest.get("schema")) is int and manifest["schema"] == 1
        and manifest.get("service") == "komga" and manifest.get("commit") == commit
        and is_digest(manifest.get("sha256")), "invalid deployment manifest",
    )
    new = {"key": commit, "sha256": manifest["sha256"], "published_at": release["published_at"]}
    if "rollback_safe" in manifest or "previous_commit" in manifest:
        need(type(manifest.get("rollback_safe")) is bool and is_commit(manifest.get("previous_commit"))
             and manifest["previous_commit"] != commit, "invalid deployment manifest rollback metadata")
        if manifest["rollback_safe"]:
            new.update(rollback_safe=True, previous_commit=manifest["previous_commit"])
    approval = approved(local.cfg, old, new)
    if not approval:
        report(f"candidate {commit} SHA-256 {new['sha256']} needs a local compatibility approval; no change made")
        return
    # Dry run verifies downloaded bytes without touching the deploy directory.
    tmp_parent = local.base if apply else None
    with tempfile.TemporaryDirectory(dir=tmp_parent) as directory:
        jar = Path(directory) / "komga.jar"
        with jar.open("xb") as out:
            digest = gh.asset(assets["komga.jar"], MAX_JAR_BYTES, out)
        need(digest == new["sha256"], "candidate JAR hash mismatch")
        if not apply:
            report(f"dry-run: would deploy verified {commit}; no change made")
            return
        check_runtime(local.cfg)
        need(health(local.cfg), "current service must be healthy before deployment")
        local.stage(jar, new)
    if approval == "automatic":
        need(run_local_hook(local.cfg["backup_precheck_command"], 180),
             "local backup/precheck did not succeed; JAR link unchanged")
    if not WINDOW_START <= now().astimezone(KST).time() < WINDOW_END:
        report("the maintenance window ended while staging; JAR link unchanged")
        return
    state["pending"] = {"from": old, "to": new, "rollback_authorized": True}
    local.save(state)
    local.set_link(new)
    try:
        healthy = checker(local.cfg)
    except (OSError, subprocess.TimeoutExpired):
        healthy = False
    if not healthy:
        recover(local, state, True, checker)
        raise DeployError("candidate health check failed; deployed version was not advanced")
    state["active"] = new
    state["pending"] = None
    local.save(state)
    report(f"deployed verified {commit}; service healthy")


def main(argv=None):
    cli = argparse.ArgumentParser()
    cli.add_argument("--config", required=True, type=Path)
    cli.add_argument("--apply", action="store_true", help="make changes; otherwise dry run")
    commands = cli.add_subparsers(dest="command", required=True)
    select = commands.add_parser("adopt")
    select.add_argument("--expect-sha256", required=True)
    select.add_argument("--deployed-commit", help="independently verified commit of the installed baseline (optional)")
    commands.add_parser("reconcile")
    commands.add_parser("recover", help="finish interrupted changes without GitHub")
    args = cli.parse_args(argv)
    fd = None
    try:
        local = Local(config(args.config))
        fd = local.open_lock(args.apply)
        if args.command == "adopt":
            adopt(local, args.expect_sha256, args.apply, args.deployed_commit)
        elif args.command == "recover":
            if not recover(local, local.read(), args.apply):
                report("no interrupted deployment")
        else:
            reconcile(local, args.apply, GitHub(), lambda: dt.datetime.now(dt.timezone.utc))
    except urllib.error.HTTPError as e:
        # Do not print a possibly signed release asset URL, body, or headers.
        report(f"deployment unavailable: HTTP {e.code}")
        return 1
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.TimeoutExpired, DeployError) as e:
        # Only our own controlled error text is safe to log.
        report(str(e) if isinstance(e, DeployError) else "deployment unavailable; inspect local configuration and service")
        return 1
    finally:
        if fd is not None:
            os.close(fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
