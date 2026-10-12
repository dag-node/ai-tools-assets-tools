# SPDX-License-Identifier: MIT
"""Drives the subcommands of packaging/release-steps.sh that a checkout runs: check-images, refresh-images and
repin-tools.

The registry is `command_stub.py` standing in for skopeo, exported to the script as a bash function, so a test decides
which manifest each reference returns; gh, sleep and, where a test injects a failure, awk and date are stubbed the same
way. Each test runs a copy of the release steps whose image pins name digests of the stub's manifests, and asserts
before its first run that the stub, not an installed command, answers the call. The two change commands run in scratch
checkouts whose origin is a bare repository under the test's directory, so a push or a pull request in an assertion is
a local push or a recorded `gh pr create` call. The release itself (the pull in preflight, the hardened verify runs)
runs on the GitHub runner alone and is proven by a release run.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

TESTS = pathlib.Path(__file__).resolve().parent
REPOSITORY = TESTS.parent
RELEASE_STEPS = REPOSITORY / "packaging" / "release-steps.sh"
STUB = TESTS / "command_stub.py"
BASH = shutil.which("bash")

IMAGES = {
    "el9": "quay.io/rockylinux/rockylinux:9",
    "el10": "quay.io/rockylinux/rockylinux:10",
    "fc44": "quay.io/fedora/fedora:44",
}


def manifest(name: str) -> str:
    return json.dumps({"schemaVersion": 2, "name": name})


def digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


PINNED = {dist: digest(manifest(f"{dist} pinned")) for dist in IMAGES}
CURRENT = {dist: digest(manifest(f"{dist} current")) for dist in IMAGES}


class ReleaseStepsCase(unittest.TestCase):
    """A scratch directory, the stub's answers and log, and a run of the release steps under the stubs."""

    stubbed = ("skopeo",)

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.answers = []
        self.answers_written = False
        self.log = self.root / "stub.log"
        self.log.write_text("", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.root)

    def answer(self, command: str, arguments: list, stdout="", stderr="", status=0, times=None, first=False):
        """Add a rule the stub answers with; <first> puts it ahead of the rules already given. The stub counts
        a rule's <times> down across runs until a test adds a rule, which writes the rules afresh."""
        rule = {"command": command, "arguments": arguments, "stdout": stdout, "stderr": stderr, "status": status,
                "times": times}
        self.answers.insert(0, rule) if first else self.answers.append(rule)
        self.answers_written = False

    def answer_registry(self, references: dict):
        """`skopeo inspect --raw` answers each reference with its manifest, or with an error when the value is one."""
        for reference, response in references.items():
            if isinstance(response, Exception):
                self.answer("skopeo", ["inspect", "--raw", f"docker://{reference}"], stderr=f"{response}\n", status=1)
            else:
                self.answer("skopeo", ["inspect", "--raw", f"docker://{reference}"], stdout=response)

    def calls(self, command: str) -> list:
        lines = self.log.read_text(encoding="utf-8").splitlines()
        return [entry["arguments"] for entry in map(json.loads, lines) if entry["command"] == command]

    def environment(self, path=None, stubbed=None) -> dict:
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("GITHUB_", "BASH_FUNC_", "GIT_"))}
        environment.update({
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "STUB_LOG": str(self.log),
            "STUB_ANSWERS": str(self.root / "answers.json"),
        })
        if path is not None:
            environment["PATH"] = path
        for command in self.stubbed if stubbed is None else stubbed:
            environment[f"BASH_FUNC_{command}%%"] = f'() {{ python3 {STUB} {command} "$@"; }}'
        return environment

    def run_steps(self, script: pathlib.Path, *arguments: str, cwd: pathlib.Path, path=None, stubbed=None):
        if not self.answers_written:
            (self.root / "answers.json").write_text(json.dumps(self.answers), encoding="utf-8")
            self.answers_written = True
        environment = self.environment(path, stubbed)
        for command in self.stubbed if stubbed is None else stubbed:
            kind = subprocess.run([BASH, "-c", f"type -t {command}"], env=environment, capture_output=True, text=True)
            self.assertEqual(kind.stdout.strip(), "function", f"{command} does not resolve to the stub")
        completed = subprocess.run([BASH, str(script), *arguments], cwd=cwd, env=environment, capture_output=True,
                                   text=True)
        return completed.returncode, completed.stdout, completed.stderr

    def git(self, directory: pathlib.Path, *arguments: str) -> str:
        completed = subprocess.run(["git", "-C", str(directory), *arguments], env=self.environment(stubbed=()),
                                   capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, f"git {' '.join(arguments)}: {completed.stderr}")
        return completed.stdout.strip()

    def make_repository(self, name: str, url: str, files: dict) -> pathlib.Path:
        """A checkout on develop, pushed to a bare origin git reaches through <url>, which the checkout's
        remote.origin.url names as written: url.<bare>.insteadOf redirects git, and a reader of the configured URL
        sees the GitHub repository."""
        bare = self.root / "remotes" / f"{name}.git"
        bare.parent.mkdir(exist_ok=True)
        subprocess.run(["git", "init", "--quiet", "--bare", "-b", "develop", str(bare)], check=True,
                       env=self.environment(stubbed=()))
        checkout = self.root / name
        checkout.mkdir(exist_ok=True)
        self.git(checkout, "init", "--quiet", "-b", "develop")
        for key, value in (("user.name", "Test Maintainer"), ("user.email", "maintainer@example.org"),
                           (f"url.{bare}.insteadOf", url), ("remote.origin.url", url),
                           ("remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")):
            self.git(checkout, "config", key, value)
        self.commit_files(checkout, files, "initial")
        self.git(checkout, "push", "--quiet", "-u", "origin", "develop")
        return checkout

    def commit_files(self, checkout: pathlib.Path, files: dict, message: str) -> str:
        for name, text in files.items():
            path = checkout / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.git(checkout, "add", "-A")
        self.git(checkout, "commit", "--quiet", "-s", "-m", message)
        return self.git(checkout, "rev-parse", "HEAD")

    def remote_branch(self, checkout: pathlib.Path, branch: str) -> str:
        return self.git(checkout, "ls-remote", "origin", f"refs/heads/{branch}").split("\t")[0]

    def set_origin_writable(self, name: str, writable: bool):
        """Take the write bits off the bare origin <name>, so a push to it is refused, or put them back."""
        bare = self.root / "remotes" / f"{name}.git"
        for path in [bare, *bare.rglob("*")]:
            mode = path.stat().st_mode
            path.chmod(mode | 0o200 if writable else mode & ~0o222)

    def write_release_steps(self, directory: pathlib.Path, references: dict) -> pathlib.Path:
        """A copy of the release steps under <directory>/packaging whose <DIST>_IMAGE lines pin <references>."""
        text = RELEASE_STEPS.read_text(encoding="utf-8")
        for dist, reference in references.items():
            text, count = re.subn(rf"^{dist.upper()}_IMAGE=\S+", f"{dist.upper()}_IMAGE={reference}", text, flags=re.M)
            self.assertEqual(count, 1, dist)
        script = directory / "packaging" / "release-steps.sh"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(text, encoding="utf-8")
        return script


class CheckImages(ReleaseStepsCase):
    def setUp(self):
        super().setUp()
        self.tools = self.root / "tools"
        self.script = self.write_release_steps(self.tools, {dist: f"{IMAGES[dist]}@{PINNED[dist]}" for dist in IMAGES})

    def pinned_reference(self, dist: str) -> str:
        repository = IMAGES[dist].rsplit(":", 1)[0]
        return f"{repository}@{PINNED[dist]}"

    def answer_pinned(self, *dists: str):
        self.answer_registry({self.pinned_reference(dist): manifest(f"{dist} pinned") for dist in dists})

    def check(self, **options):
        return self.run_steps(self.script, "check-images", cwd=self.tools, **options)

    def test_every_reference_resolves(self):
        self.answer_pinned(*IMAGES)
        status, stdout, stderr = self.check()
        self.assertEqual(status, 0, stderr)
        for dist in IMAGES:
            self.assertIn(f"release-steps: ok: {dist} {IMAGES[dist]} serves the pinned digest {PINNED[dist][:19]}", stdout)
        self.assertIn("release-steps: ok: every image resolves", stdout)
        self.assertEqual(len(self.calls("skopeo")), len(IMAGES))

    def test_an_unknown_manifest_names_the_digest_the_tag_names_now(self):
        self.answer_pinned("el9", "el10")
        self.answer_registry({
            self.pinned_reference("fc44"): Exception("Error: reading manifest in quay.io/fedora/fedora: manifest unknown"),
            IMAGES["fc44"]: manifest("fc44 current"),
        })
        status, stdout, stderr = self.check()
        self.assertEqual(status, 1)
        self.assertIn(f"fc44: {IMAGES['fc44']} no longer serves the pinned digest {PINNED['fc44']} (manifest unknown);"
                      f" the tag names {CURRENT['fc44']} now, so the pin to paste is"
                      f" FC44_IMAGE={IMAGES['fc44']}@{CURRENT['fc44']}", stderr)
        self.assertIn("check-images: 1 of 3 images failed the check", stderr)
        # Every image is checked before the exit.
        self.assertIn("release-steps: ok: el9", stdout)
        self.assertIn("release-steps: ok: el10", stdout)

    def test_an_unreadable_registry_names_no_replacement(self):
        self.answer_pinned("el9", "el10")
        self.answer_registry({self.pinned_reference("fc44"): Exception("dial tcp: lookup quay.io: i/o timeout")})
        status, _, stderr = self.check()
        self.assertEqual(status, 1)
        self.assertIn(f"fc44: the registry could not be read for {IMAGES['fc44']} (dial tcp: lookup quay.io: i/o timeout);"
                      " this is an outage, not an expiry", stderr)
        self.assertNotIn("the tag names", stderr)
        self.assertEqual(len(self.calls("skopeo")), len(IMAGES))

    def test_a_manifest_unknown_whose_tag_cannot_be_read_names_no_replacement(self):
        self.answer_pinned("el9", "el10")
        self.answer_registry({
            self.pinned_reference("fc44"): Exception("manifest unknown"),
            IMAGES["fc44"]: Exception("unauthorized: access to the requested resource is not authorized"),
        })
        status, _, stderr = self.check()
        self.assertEqual(status, 1)
        self.assertIn(f"fc44: the registry could not be read for {IMAGES['fc44']} (unauthorized:", stderr)
        self.assertNotIn("the tag names", stderr)

    def test_a_manifest_whose_digest_is_not_the_pin_fails(self):
        self.answer_pinned("el9", "el10")
        self.answer_registry({self.pinned_reference("fc44"): manifest("fc44 current")})
        status, _, stderr = self.check()
        self.assertEqual(status, 1)
        self.assertIn(f"the registry returned a manifest whose digest is {CURRENT['fc44']}", stderr)

    def test_an_absent_skopeo_is_named(self):
        farm = self.root / "bin"
        farm.mkdir()
        for command in ("dirname", "grep"):
            (farm / command).symlink_to(shutil.which(command))
        status, _, stderr = self.check(path=str(farm), stubbed=())
        self.assertEqual(status, 1)
        self.assertIn("`skopeo` is not installed and did not install; install it, then run again", stderr)

    def test_a_reference_without_a_digest_or_out_of_shape_is_refused(self):
        self.script = self.write_release_steps(self.tools, {
            "el9": IMAGES["el9"],
            "el10": "quay.io/rockylinux/rockylinux@sha256:827d",
            "fc44": f"{IMAGES['fc44']}@{PINNED['fc44']}",
        })
        self.answer_pinned("fc44")
        status, stdout, stderr = self.check()
        self.assertEqual(status, 1)
        self.assertIn(f"el9: EL9_IMAGE={IMAGES['el9']} carries no digest; every image is pinned as"
                      " <repository>:<tag>@sha256:<digest>", stderr)
        self.assertIn("el10: EL10_IMAGE=quay.io/rockylinux/rockylinux@sha256:827d is not"
                      " <repository>:<tag>@sha256:<digest>", stderr)
        self.assertIn("check-images: 2 of 3 images failed the check", stderr)
        self.assertIn("release-steps: ok: fc44", stdout)
        self.assertEqual(len(self.calls("skopeo")), 1)


OLD_PIN = "49eddc1f67e23cf6a7b02e82936e7bb03eb10609"


def caller_workflows(validate_ref=OLD_PIN, release_ref=OLD_PIN, comment="v1.2.0") -> dict:
    """A publisher's two workflow files, each calling a workflow of the tools at a ref, as dag-node/ai-tools-assets does."""
    suffix = f" # {comment}" if comment else ""
    return {
        "validate.yml": ("name: validate\non:\n  pull_request:\njobs:\n  ai-tools-assets:\n"
                         f"    uses: dag-node/ai-tools-assets-tools/.github/workflows/validate.yml@{validate_ref}{suffix}\n"
                         "    with:\n      enforce-signoff: true\n"),
        "release.yml": ("name: release\non:\n  push:\n    tags: ['*/v*']\njobs:\n  ai-tools-assets:\n"
                        f"    uses: dag-node/ai-tools-assets-tools/.github/workflows/set-release.yml@{release_ref}{suffix}\n"
                        "    secrets: inherit\n"),
    }


class CheckImagesInACaller(CheckImages):
    """check-images run from a caller's checkout, as validate.yml runs it there."""

    def setUp(self):
        super().setUp()
        self.caller = self.root / "caller"
        self.write_caller(caller_workflows())

    def write_caller(self, files: dict):
        workflows = self.caller / ".github" / "workflows"
        shutil.rmtree(workflows, ignore_errors=True)
        workflows.mkdir(parents=True)
        for name, text in files.items():
            (workflows / name).write_text(text, encoding="utf-8")

    def check(self, **options):
        return self.run_steps(self.script, "check-images", cwd=self.caller, **options)

    def test_pins_on_one_ref_pass(self):
        self.answer_pinned(*IMAGES)
        status, stdout, stderr = self.check()
        self.assertEqual(status, 0, stderr)
        self.assertIn(f"release-steps: ok: the 2 `uses:` lines naming dag-node/ai-tools-assets-tools pin @{OLD_PIN}",
                      stdout)

    def test_pins_on_two_refs_are_refused_before_the_registry_is_asked(self):
        self.write_caller(caller_workflows(validate_ref="v1"))
        status, _, stderr = self.check()
        self.assertEqual(status, 1)
        self.assertIn(f"the `uses:` lines pin different refs (.github/workflows/release.yml: @{OLD_PIN},"
                      " .github/workflows/validate.yml: @v1); make them one, then run again", stderr)
        self.assertEqual(self.calls("skopeo"), [])

    def test_a_pin_in_another_form_is_refused(self):
        files = caller_workflows()
        files["release.yml"] = files["release.yml"].replace(
            "uses: dag-node/ai-tools-assets-tools/", "uses: 'Dag-Node/ai-tools-assets-tools/").replace(" # v1.2.0", "'")
        self.write_caller(files)
        status, _, stderr = self.check()
        self.assertEqual(status, 1)
        self.assertIn(".github/workflows/release.yml:7 names a workflow of dag-node/ai-tools-assets-tools in a form other"
                      " than `uses: dag-node/ai-tools-assets-tools/.github/workflows/<file>@<ref> # <comment>`", stderr)

    def test_a_caller_without_set_release_says_so_and_passes(self):
        self.write_caller({"validate.yml": caller_workflows()["validate.yml"]})
        self.answer_pinned(*IMAGES)
        status, stdout, stderr = self.check()
        self.assertEqual(status, 0, stderr)
        self.assertIn("no `uses:` line names set-release.yml, so a release does not run through these pins", stdout)

    def test_an_expiry_in_a_caller_says_where_the_refresh_may_be(self):
        self.answer_pinned("el9", "el10")
        self.answer_registry({
            self.pinned_reference("fc44"): Exception("manifest unknown"),
            IMAGES["fc44"]: manifest("fc44 current"),
        })
        status, _, stderr = self.check()
        self.assertEqual(status, 1)
        self.assertIn("dag-node/ai-tools-assets-tools's develop may already carry the refresh: in its checkout,"
                      " `git log origin/develop -- packaging/release-steps.sh`; then"
                      " `bash <ai-tools-assets-tools>/packaging/release-steps.sh repin-tools --id <id> <sha>` here",
                      stderr)

    def test_the_tools_own_tree_has_no_pins_to_compare(self):
        shutil.rmtree(self.caller / ".github")
        self.answer_pinned(*IMAGES)
        status, stdout, stderr = self.check()
        self.assertEqual(status, 0, stderr)
        self.assertIn("no pins to compare", stdout)


TOOLS_URL = "https://github.com/dag-node/ai-tools-assets-tools.git"
CALLER_URL = "https://github.com/dag-node/ai-tools-assets.git"
TOOLS_SLUG = "dag-node/ai-tools-assets-tools"
CALLER_SLUG = "dag-node/ai-tools-assets"
CHANGELOG = """# Changelog

## [Unreleased]

## [1.2.0] - 2026-10-12

### Fixed

- **Hanging paragraphs.** An entry of the previous release.
"""
REFRESH_ID = "ATR-261103a"
REFRESH_BRANCH = f"build/{REFRESH_ID}-fc44-digest"
REFRESH_TITLE = "build(packaging): pin the Fedora 44 image quay.io serves"
PULL_REQUEST_URL = "https://github.com/dag-node/ai-tools-assets-tools/pull/31"


def today() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def refresh_entry(date: str) -> str:
    """The changelog entry refresh-images writes for the Fedora 44 image."""
    return ("- **Fedora 44 image.** A release verifies its Fedora 44 RPM in the image\n"
            f"  quay.io serves for `quay.io/fedora/fedora:44` on {date}. quay.io retired\n"
            "  the digest 1.2.0 pinned, so `check-images` reported `manifest unknown` on\n"
            "  every pull request.\n")


def unwrap(text: str) -> str:
    """The paragraphs of <text>, each joined onto one line, so a test compares the words and not the line breaks."""
    return "\n\n".join(" ".join(paragraph.split()) for paragraph in text.split("\n\n"))


def written_line(pins: int, changelog_lines: int) -> str:
    """The `ok:` line refresh-images prints once the scratch worktree holds the change it verified."""
    return (f"release-steps: ok: packaging/release-steps.sh +{pins} -{pins}, CHANGELOG.md +{changelog_lines} -0 in the"
            " scratch worktree")


def refresh_body(date: str) -> str:
    """The commit and pull-request body refresh-images writes for the Fedora 44 image."""
    old, new = PINNED["fc44"][7:19], CURRENT["fc44"][7:19]
    return ("quay.io retired the Fedora 44 digest the release steps pinned\n"
            f"(`{old}`), so `check-images` fails every pull request and push\n"
            "here and in each caller with `manifest unknown`. The steps pin the\n"
            f"digest `quay.io/fedora/fedora:44` resolves to on {date}\n"
            f"(`{new}`).\n"
            "\n"
            "A caller re-pins its `uses:` lines to this commit once it is on\n"
            f"`develop`: `release-steps.sh repin-tools --id {REFRESH_ID} <sha>`. No\n"
            "release is cut for a refresh; the next release carries it.")


class ChangeCase(CheckImages):
    """A checkout of the tools and of a caller, each with a bare origin, and gh logged in with no open pull request."""

    stubbed = ("skopeo", "gh", "sleep")

    def setUp(self):
        super().setUp()
        self.tools = self.make_repository("tools", TOOLS_URL, {
            "packaging/release-steps.sh": self.script.read_text(encoding="utf-8"),
            "CHANGELOG.md": CHANGELOG,
            ".github/workflows/release.yml": (REPOSITORY / ".github" / "workflows" / "release.yml").read_text(
                encoding="utf-8"),
        })
        self.script = self.tools / "packaging" / "release-steps.sh"
        self.caller = self.make_repository("caller", CALLER_URL, {
            f".github/workflows/{name}": text for name, text in caller_workflows().items()})
        self.answer("gh", ["auth", "status"])
        self.answer("sleep", [])

    def answer_open_pull_requests(self, slug: str, lines=""):
        self.answer("gh", ["pr", "list", "--repo", slug, "--base", "develop"], stdout=lines)

    def answer_expired_fedora(self):
        self.answer_pinned("el9", "el10")
        self.answer_registry({
            self.pinned_reference("fc44"): Exception("Error: reading manifest: manifest unknown"),
            IMAGES["fc44"]: manifest("fc44 current"),
        })

    def pull_request_creations(self) -> list:
        return [call for call in self.calls("gh") if call[:2] == ["pr", "create"]]


class RefreshImages(ChangeCase):
    def refresh(self, *options: str, cwd=None, **keywords):
        return self.run_steps(self.script, "refresh-images", "--id", REFRESH_ID, *options, cwd=cwd or self.tools,
                              **keywords)

    def answer_pull_request(self, existing=""):
        self.answer_open_pull_requests(TOOLS_SLUG)
        self.answer("gh", ["pr", "list", "--repo", TOOLS_SLUG, "--head", REFRESH_BRANCH], stdout=existing)
        self.answer("gh", ["pr", "create"], stdout=f"{PULL_REQUEST_URL}\n")

    def assert_tools_unchanged(self, develop: str):
        self.assertEqual(self.git(self.tools, "rev-parse", "develop"), develop)
        self.assertEqual(self.git(self.tools, "status", "--porcelain"), "")
        self.assertEqual(self.git(self.tools, "branch", "--list", "build/*"), "")
        self.assertEqual(self.remote_branch(self.tools, REFRESH_BRANCH), "")
        self.assertEqual(self.pull_request_creations(), [])

    def test_every_image_resolving_changes_nothing(self):
        develop = self.git(self.tools, "rev-parse", "develop")
        self.answer_pinned(*IMAGES)
        status, stdout, stderr = self.refresh()
        self.assertEqual(status, 0, stderr)
        self.assertIn("release-steps: ok: every image resolves; nothing to refresh", stdout)
        self.assert_tools_unchanged(develop)

    def test_an_expired_image_becomes_a_commit_a_push_and_a_pull_request(self):
        develop = self.git(self.tools, "rev-parse", "develop")
        self.answer_expired_fedora()
        self.answer_pull_request()
        status, stdout, stderr = self.refresh()
        self.assertEqual(status, 0, stderr)
        date = today()
        tip = self.git(self.tools, "rev-parse", REFRESH_BRANCH)
        self.assertEqual(self.git(self.tools, "rev-parse", f"{REFRESH_BRANCH}^"), develop)
        self.assertEqual(self.remote_branch(self.tools, REFRESH_BRANCH), tip)
        self.assertEqual(self.git(self.tools, "symbolic-ref", "--short", "HEAD"), "develop")
        self.assertEqual(self.git(self.tools, "status", "--porcelain"), "")
        script = self.git(self.tools, "show", f"{REFRESH_BRANCH}:packaging/release-steps.sh")
        self.assertIn(f"\nFC44_IMAGE={IMAGES['fc44']}@{CURRENT['fc44']} # resolved {date}\n", script)
        self.assertIn(f"\nEL9_IMAGE={IMAGES['el9']}@{PINNED['el9']} # resolved 2026-10-08\n", script)
        changelog = self.git(self.tools, "show", f"{REFRESH_BRANCH}:CHANGELOG.md")
        self.assertIn(f"## [Unreleased]\n\n### Fixed\n\n{refresh_entry(date)}\n## [1.2.0]", changelog)
        self.assertEqual(self.git(self.tools, "diff", "--name-only", f"develop..{REFRESH_BRANCH}").splitlines(),
                         ["CHANGELOG.md", "packaging/release-steps.sh"])
        message = self.git(self.tools, "log", "-1", "--format=%B", REFRESH_BRANCH)
        self.assertEqual(message, f"{REFRESH_TITLE}\n\n{refresh_body(date)}\n\n"
                                  "Signed-off-by: Test Maintainer <maintainer@example.org>")
        creation = self.pull_request_creations()
        self.assertEqual(len(creation), 1)
        self.assertEqual(creation[0][:9], ["pr", "create", "--repo", TOOLS_SLUG, "--base", "develop", "--head",
                                           REFRESH_BRANCH, "--title"])
        self.assertEqual(creation[0][9], REFRESH_TITLE)
        body_file = pathlib.Path(creation[0][11])
        self.assertEqual(body_file.read_text(encoding="utf-8"), refresh_body(date) + "\n")
        for line in (f"release-steps: refresh-images: fetching origin/develop in {self.tools}",
                     f"release-steps: ok: develop = origin/develop ({develop[:7]})",
                     "release-steps: refresh-images: checking 3 images",
                     f"release-steps: ok: fc44 expired ({PINNED['fc44'][7:19]} -> {CURRENT['fc44'][7:19]}); el9, el10 resolve",
                     f"release-steps: refresh-images: checking out develop of {self.tools} in a scratch worktree",
                     "release-steps: refresh-images: writing packaging/release-steps.sh, CHANGELOG.md in the scratch"
                     " worktree",
                     written_line(1, len(refresh_entry(date).splitlines()) + 3),
                     f"release-steps: refresh-images: committing on {REFRESH_BRANCH}",
                     f"release-steps: ok: commit {tip[:7]} (signed off by Test Maintainer)",
                     f"release-steps: refresh-images: pushing {REFRESH_BRANCH}",
                     f"release-steps: ok: origin/{REFRESH_BRANCH} = {tip[:7]}",
                     "release-steps: refresh-images: opening the pull request",
                     f"release-steps: ok: {PULL_REQUEST_URL}",
                     f"release-steps: next, after the merge: git ls-remote {TOOLS_URL} refs/heads/develop",
                     f"release-steps: next, in each caller: bash {self.tools}/packaging/release-steps.sh repin-tools"
                     f" --id {REFRESH_ID} <sha>"):
            self.assertIn(line + "\n", stdout)
        self.assertTrue(stdout.endswith(f"repin-tools --id {REFRESH_ID} <sha>\n"), stdout)
        self.assertEqual(self.git(self.tools, "worktree", "list", "--porcelain").count("worktree "), 1)

    def test_an_unreadable_registry_is_refused_with_nothing_written(self):
        develop = self.git(self.tools, "rev-parse", "develop")
        self.answer_pinned("el9", "el10")
        self.answer_registry({self.pinned_reference("fc44"): Exception("dial tcp: i/o timeout")})
        status, _, stderr = self.refresh()
        self.assertEqual(status, 1)
        self.assertIn("the registry could not be read for quay.io/fedora/fedora:44 (dial tcp: i/o timeout); this is"
                      " an outage, not an expiry: retry later, nothing was changed", stderr)
        self.assertIn("so an expiry cannot be told from an outage: retry later, nothing was changed", stderr)
        self.assert_tools_unchanged(develop)

    def test_a_dirty_tree_is_refused(self):
        develop = self.git(self.tools, "rev-parse", "develop")
        (self.tools / "notes.txt").write_text("draft\n", encoding="utf-8")
        status, _, stderr = self.refresh()
        self.assertEqual(status, 1)
        self.assertIn("?? notes.txt", stderr)
        self.assertIn("the tree has uncommitted changes (`git status --short` printed above); commit or stash them,"
                      " then run again", stderr)
        (self.tools / "notes.txt").unlink()
        self.assert_tools_unchanged(develop)
        self.assertEqual(self.calls("skopeo"), [])

    def test_a_checkout_off_develop_is_refused(self):
        self.git(self.tools, "switch", "--quiet", "-c", "topic")
        status, _, stderr = self.refresh()
        self.assertEqual(status, 1)
        self.assertIn("the checkout is on topic, not `develop`; run `git switch develop`", stderr)

    def test_a_develop_behind_origin_is_refused(self):
        develop = self.git(self.tools, "rev-parse", "develop")
        self.commit_files(self.tools, {"README.md": "newer\n"}, "a newer commit")
        self.git(self.tools, "push", "--quiet", "origin", "develop")
        self.git(self.tools, "update-ref", "refs/heads/develop", develop)
        self.git(self.tools, "reset", "--quiet", "--hard", develop)
        status, _, stderr = self.refresh()
        self.assertEqual(status, 1)
        self.assertIn("`develop` is 1 behind `origin/develop` after a fetch; run `git pull --ff-only` first", stderr)

    def test_a_develop_ahead_of_origin_is_refused(self):
        self.commit_files(self.tools, {"README.md": "local\n"}, "a local commit")
        status, _, stderr = self.refresh()
        self.assertEqual(status, 1)
        self.assertIn("`develop` is 1 ahead of `origin/develop` after a fetch; push or rebase the local commits first",
                      stderr)

    def test_gh_not_logged_in_is_refused(self):
        self.answers = [answer for answer in self.answers if answer["arguments"] != ["auth", "status"]]
        self.answer("gh", ["auth", "status"], stderr="You are not logged into any GitHub hosts.\n", status=1)
        status, _, stderr = self.refresh()
        self.assertEqual(status, 1)
        self.assertIn("`gh` is not logged in to github.com; run `gh auth login`", stderr)

    def test_an_id_out_of_shape_is_refused(self):
        for options in ((), ("--id", "261103a")):
            status, _, stderr = self.run_steps(self.script, "refresh-images", *options, cwd=self.tools)
            self.assertEqual(status, 1)
            self.assertIn("`--id` is required, as `<PREFIX>-<yyMMdd><letter>` (for example `ATR-261103a`)", stderr)

    def test_a_directory_other_than_the_checkout_root_is_refused(self):
        status, _, stderr = self.refresh(cwd=self.tools / "packaging")
        self.assertEqual(status, 1)
        self.assertIn(f"{self.tools / 'packaging'} is not a checkout of ai-tools-assets-tools; run from the"
                      " repository root", stderr)

    def test_a_changelog_without_unreleased_is_refused(self):
        self.commit_files(self.tools, {"CHANGELOG.md": CHANGELOG.replace("## [Unreleased]\n\n", "")}, "drop it")
        self.git(self.tools, "push", "--quiet", "origin", "develop")
        self.answer_expired_fedora()
        self.answer_pull_request()
        status, _, stderr = self.refresh()
        self.assertEqual(status, 1)
        self.assertIn("`CHANGELOG.md` has no `## [Unreleased]` section; add it above the newest release heading, then"
                      " run again", stderr)
        self.assertEqual(self.pull_request_creations(), [])

    def test_a_changelog_without_a_release_heading_is_refused(self):
        self.commit_files(self.tools, {"CHANGELOG.md": "# Changelog\n\n## [Unreleased]\n"}, "no release yet")
        self.git(self.tools, "push", "--quiet", "origin", "develop")
        self.answer_expired_fedora()
        status, _, stderr = self.refresh()
        self.assertEqual(status, 1)
        self.assertIn("`CHANGELOG.md` has no `## [X.Y.Z]` heading; the entry needs the previous version", stderr)

    def refresh_until_the_push_is_refused(self, *options: str) -> str:
        """A run that commits on the branch and stops at a push the origin refuses; returns the branch's tip."""
        self.set_origin_writable("tools", False)
        try:
            status, _, stderr = self.refresh(*options)
        finally:
            self.set_origin_writable("tools", True)
        self.assertEqual(status, 1, stderr)
        self.assertIn(f"the push of {REFRESH_BRANCH} was refused (", stderr)
        self.assertIn("); the branch is local and nothing was opened; fix the cause and run again", stderr)
        self.assertEqual(self.remote_branch(self.tools, REFRESH_BRANCH), "")
        self.assertEqual(self.pull_request_creations(), [])
        return self.git(self.tools, "rev-parse", REFRESH_BRANCH)

    def test_a_refused_push_leaves_the_branch_local_and_opens_nothing(self):
        self.answer_expired_fedora()
        self.answer_pull_request()
        self.refresh_until_the_push_is_refused()

    def test_a_failed_rewrite_leaves_the_file_and_creates_no_branch(self):
        develop = self.git(self.tools, "rev-parse", "develop")
        self.answer_expired_fedora()
        self.answer_pull_request()
        for stdout, stderr, status, phrase in (
                ("", "awk: simulated read failure\n", 2, "awk did not rewrite"),
                ("FC44_IMAGE=partial\n", "", 0, "holds 1 lines, not")):
            self.answer("awk", [], stdout=stdout, stderr=stderr, status=status, first=True)
            status, _, stderr = self.refresh(stubbed=(*self.stubbed, "awk"))
            self.assertEqual(status, 1, stderr)
            self.assertIn(phrase, stderr)
            self.assertIn("; the file is unchanged", stderr)
            self.assert_tools_unchanged(develop)

    def test_a_commit_that_fails_creates_no_branch_and_a_rerun_finishes(self):
        develop = self.git(self.tools, "rev-parse", "develop")
        self.git(self.tools, "config", "user.useConfigOnly", "true")
        self.git(self.tools, "config", "--unset", "user.email")
        self.answer_expired_fedora()
        self.answer_pull_request()
        status, _, stderr = self.refresh()
        self.assertEqual(status, 1)
        self.assertIn(f"the commit on {REFRESH_BRANCH} failed (", stderr)
        self.assertIn("); no branch was created and nothing was pushed", stderr)
        self.assert_tools_unchanged(develop)
        self.git(self.tools, "config", "user.email", "maintainer@example.org")
        status, stdout, stderr = self.refresh()
        self.assertEqual(status, 0, stderr)
        self.assertEqual(len(self.pull_request_creations()), 1)
        self.assertEqual(self.remote_branch(self.tools, REFRESH_BRANCH), self.git(self.tools, "rev-parse", REFRESH_BRANCH))

    def test_a_rerun_on_a_later_day_keeps_the_date_the_branch_committed(self):
        self.answer_expired_fedora()
        self.answer_pull_request()
        self.answer("date", ["-u", "+%Y-%m-%d"], stdout="2026-11-03\n")
        stubbed = (*self.stubbed, "date")
        self.set_origin_writable("tools", False)
        try:
            status, _, stderr = self.refresh(stubbed=stubbed)
        finally:
            self.set_origin_writable("tools", True)
        self.assertEqual(status, 1, stderr)
        tip = self.git(self.tools, "rev-parse", REFRESH_BRANCH)
        self.answer("date", ["-u", "+%Y-%m-%d"], stdout="2026-11-04\n", first=True)
        status, stdout, stderr = self.refresh(stubbed=stubbed)
        self.assertEqual(status, 0, stderr)
        self.assertIn(f"release-steps: ok: branch {REFRESH_BRANCH} already carries this change; continuing from it",
                      stdout)
        self.assertEqual(self.git(self.tools, "rev-parse", REFRESH_BRANCH), tip)
        self.assertEqual(self.remote_branch(self.tools, REFRESH_BRANCH), tip)
        self.assertIn(f"# resolved 2026-11-03\n", self.git(self.tools, "show", f"{REFRESH_BRANCH}:packaging/release-steps.sh"))
        self.assertEqual(len(self.pull_request_creations()), 1)

    def test_two_expired_images_become_one_commit(self):
        self.answer_pinned("el10")
        self.answer_registry({
            self.pinned_reference("el9"): Exception("manifest unknown"),
            IMAGES["el9"]: manifest("el9 current"),
            self.pinned_reference("fc44"): Exception("manifest unknown"),
            IMAGES["fc44"]: manifest("fc44 current"),
        })
        branch = f"build/{REFRESH_ID}-el9-fc44-digest"
        self.answer_open_pull_requests(TOOLS_SLUG)
        self.answer("gh", ["pr", "list", "--repo", TOOLS_SLUG, "--head", branch])
        self.answer("gh", ["pr", "create"], stdout=f"{PULL_REQUEST_URL}\n")
        status, stdout, stderr = self.refresh()
        self.assertEqual(status, 0, stderr)
        date = today()
        script = self.git(self.tools, "show", f"{branch}:packaging/release-steps.sh")
        self.assertIn(f"\nEL9_IMAGE={IMAGES['el9']}@{CURRENT['el9']} # resolved {date}\n", script)
        self.assertIn(f"\nFC44_IMAGE={IMAGES['fc44']}@{CURRENT['fc44']} # resolved {date}\n", script)
        self.assertIn(f"\nEL10_IMAGE={IMAGES['el10']}@{PINNED['el10']} # resolved 2026-10-08\n", script)
        message = unwrap(self.git(self.tools, "log", "-1", "--format=%B", branch))
        self.assertTrue(message.startswith("build(packaging): pin the Rocky 9 and Fedora 44 images quay.io serves\n\n"
                                           "quay.io retired the Rocky 9 and Fedora 44 digests the release steps pinned"
                                           f" (`{PINNED['el9'][7:19]}`, `{PINNED['fc44'][7:19]}`), so"), message)
        self.assertIn(f"The steps pin the digests `{IMAGES['el9']}` and `{IMAGES['fc44']}` resolve to on {date}"
                      f" (`{CURRENT['el9'][7:19]}`, `{CURRENT['fc44'][7:19]}`).", message)
        changelog = self.git(self.tools, "show", f"{branch}:CHANGELOG.md")
        self.assertIn("- **Rocky 9 and Fedora 44 images.** A release verifies its Rocky 9 and Fedora 44 RPMs in the images"
                      f" quay.io serves for `{IMAGES['el9']}` and `{IMAGES['fc44']}` on {date}. quay.io retired the"
                      " digests 1.2.0 pinned,", unwrap(changelog))
        self.assertIn(f"release-steps: ok: el9, fc44 expired ({PINNED['el9'][7:19]} -> {CURRENT['el9'][7:19]},"
                      f" {PINNED['fc44'][7:19]} -> {CURRENT['fc44'][7:19]}); el10 resolve", stdout)
        self.assertIn(written_line(2, len(changelog.splitlines()) - len(CHANGELOG.splitlines())), stdout)

    def test_the_entry_joins_an_existing_fixed_list(self):
        changelog = CHANGELOG.replace("## [Unreleased]\n", "## [Unreleased]\n\n### Fixed\n\n- **Earlier.** An entry.\n")
        self.commit_files(self.tools, {"CHANGELOG.md": changelog}, "an earlier fix")
        self.git(self.tools, "push", "--quiet", "origin", "develop")
        self.answer_expired_fedora()
        self.answer_pull_request()
        status, _, stderr = self.refresh()
        self.assertEqual(status, 0, stderr)
        self.assertIn(f"## [Unreleased]\n\n### Fixed\n\n- **Earlier.** An entry.\n{refresh_entry(today())}\n## [1.2.0]",
                      self.git(self.tools, "show", f"{REFRESH_BRANCH}:CHANGELOG.md"))

    def test_help_prints_the_usage_of_both_commands(self):
        status, stdout, stderr = self.run_steps(self.script, "refresh-images", "--help", cwd=self.tools)
        self.assertEqual(status, 0, stderr)
        self.assertIn("usage: release-steps.sh refresh-images --id <ID> [--caller <path>]... [--dry-run]", stdout)
        self.assertIn("release-steps.sh repin-tools --id <ID> <sha>|<vX.Y.Z> [--pr <number>] [--dry-run]", stdout)

    def test_a_dry_run_prints_the_change_and_writes_nothing(self):
        develop = self.git(self.tools, "rev-parse", "develop")
        self.answer_expired_fedora()
        self.answer_pull_request()
        status, stdout, stderr = self.refresh("--dry-run")
        self.assertEqual(status, 0, stderr)
        self.assertIn(f"+FC44_IMAGE={IMAGES['fc44']}@{CURRENT['fc44']} # resolved {today()}", stdout)
        self.assertIn("+- **Fedora 44 image.**", stdout)
        self.assertIn(f"\n{REFRESH_TITLE}\n\n{refresh_body(today())}\n", stdout)
        self.assertIn("release-steps: refresh-images: writing packaging/release-steps.sh, CHANGELOG.md in the scratch"
                      " worktree", stdout)
        self.assertIn(f"release-steps: refresh-images: would commit on {REFRESH_BRANCH}:", stdout)
        self.assertIn(f"release-steps: refresh-images: would push {REFRESH_BRANCH} to origin", stdout)
        self.assertIn(f"release-steps: next, to make the change: bash packaging/release-steps.sh refresh-images --id"
                      f" {REFRESH_ID}", stdout)
        self.assert_tools_unchanged(develop)
        self.assertEqual(self.git(self.tools, "worktree", "list", "--porcelain").count("worktree "), 1)

    def test_a_dry_run_refuses_a_branch_on_origin(self):
        self.git(self.tools, "push", "--quiet", "origin", f"develop:refs/heads/{REFRESH_BRANCH}")
        self.answer_expired_fedora()
        self.answer_pull_request()
        status, _, stderr = self.refresh("--dry-run")
        self.assertEqual(status, 1)
        self.assertIn(f"branch {REFRESH_BRANCH} already exists on origin; pick the next letter for `--id`", stderr)

    def test_a_dry_run_without_gh_still_prints_the_change(self):
        self.answers = []
        self.answer("gh", ["auth", "status"], status=1)
        self.answer_expired_fedora()
        status, stdout, stderr = self.refresh("--dry-run")
        self.assertEqual(status, 0, stderr)
        self.assertIn("`gh` is not logged in to github.com, so the dry run does not read GitHub", stderr)
        self.assertIn(f"\n{REFRESH_TITLE}\n", stdout)

    def test_a_rerun_after_the_branch_was_made_pushes_and_opens_once(self):
        self.answer_expired_fedora()
        self.answer_pull_request()
        tip = self.refresh_until_the_push_is_refused()
        status, stdout, stderr = self.refresh()
        self.assertEqual(status, 0, stderr)
        self.assertIn(f"release-steps: ok: branch {REFRESH_BRANCH} already carries this change; continuing from it",
                      stdout)
        self.assertIn(f"release-steps: ok: origin/{REFRESH_BRANCH} = {tip[:7]}\n", stdout)
        self.assertEqual(self.git(self.tools, "rev-parse", REFRESH_BRANCH), tip)
        self.assertEqual(self.remote_branch(self.tools, REFRESH_BRANCH), tip)
        self.assertEqual(len(self.pull_request_creations()), 1)

    def test_a_rerun_after_the_push_opens_the_pull_request_once(self):
        self.answer_expired_fedora()
        self.answer_open_pull_requests(TOOLS_SLUG)
        self.answer("gh", ["pr", "list", "--repo", TOOLS_SLUG, "--head", REFRESH_BRANCH])
        self.answer("gh", ["pr", "create"], stderr="HTTP 422: Validation Failed\n", status=1, times=1)
        self.answer("gh", ["pr", "create"], stdout=f"{PULL_REQUEST_URL}\n")
        status, _, stderr = self.refresh()
        self.assertEqual(status, 1)
        tip = self.git(self.tools, "rev-parse", REFRESH_BRANCH)
        self.assertIn(f"branch {REFRESH_BRANCH} is pushed; the pull request was not created (HTTP 422: Validation"
                      f" Failed); retry with: gh pr create --repo {TOOLS_SLUG} --base develop --head {REFRESH_BRANCH}"
                      f' --title "{REFRESH_TITLE}" --body-file ', stderr)
        status, stdout, stderr = self.refresh()
        self.assertEqual(status, 0, stderr)
        self.assertIn(f"release-steps: ok: origin/{REFRESH_BRANCH} = {tip[:7]}, pushed before", stdout)
        self.assertIn(f"release-steps: ok: {PULL_REQUEST_URL}", stdout)
        self.assertEqual(len(self.pull_request_creations()), 2)
        self.assertEqual(self.remote_branch(self.tools, REFRESH_BRANCH), tip)

    def test_a_rerun_after_the_pull_request_creates_nothing(self):
        self.answer_expired_fedora()
        self.answer_pull_request()
        self.assertEqual(self.refresh()[0], 0)
        self.answer("gh", ["pr", "list", "--repo", TOOLS_SLUG, "--head", REFRESH_BRANCH],
                    stdout=f"{PULL_REQUEST_URL}\n", first=True)
        tip = self.git(self.tools, "rev-parse", REFRESH_BRANCH)
        status, stdout, stderr = self.refresh()
        self.assertEqual(status, 0, stderr)
        self.assertIn(f"release-steps: ok: pull request {PULL_REQUEST_URL} is open for {REFRESH_BRANCH}; nothing to"
                      " create", stdout)
        self.assertEqual(len(self.pull_request_creations()), 1)
        self.assertEqual(self.git(self.tools, "rev-parse", REFRESH_BRANCH), tip)

    def test_a_branch_of_the_same_name_with_another_change_is_refused(self):
        self.git(self.tools, "branch", REFRESH_BRANCH)
        self.answer_expired_fedora()
        self.answer_pull_request()
        status, _, stderr = self.refresh()
        self.assertEqual(status, 1)
        develop = self.git(self.tools, "rev-parse", "--short=7", "develop")
        self.assertIn(f"branch {REFRESH_BRANCH} exists with a different change ({develop}); delete it or pick the next"
                      " letter for `--id`", stderr)
        self.assertEqual(self.remote_branch(self.tools, REFRESH_BRANCH), "")

    def test_a_branch_on_origin_alone_is_refused(self):
        self.git(self.tools, "push", "--quiet", "origin", f"develop:refs/heads/{REFRESH_BRANCH}")
        self.answer_expired_fedora()
        self.answer_pull_request()
        status, _, stderr = self.refresh()
        self.assertEqual(status, 1)
        self.assertIn(f"branch {REFRESH_BRANCH} already exists on origin; pick the next letter for `--id`", stderr)
        self.assertEqual(self.git(self.tools, "branch", "--list", "build/*"), "")

    def test_a_branch_github_does_not_yet_serve_is_retried(self):
        self.answer_expired_fedora()
        self.answer_open_pull_requests(TOOLS_SLUG)
        self.answer("gh", ["pr", "list", "--repo", TOOLS_SLUG, "--head", REFRESH_BRANCH])
        self.answer("gh", ["pr", "create"], stderr="could not find any commits for the head branch\n", status=1,
                    times=2)
        self.answer("gh", ["pr", "create"], stdout=f"{PULL_REQUEST_URL}\n")
        status, stdout, stderr = self.refresh()
        self.assertEqual(status, 0, stderr)
        self.assertIn(f"release-steps: retry 1/5: the pull request for {REFRESH_BRANCH} (could not find any commits"
                      " for the head branch); waiting 10 s", stdout)
        self.assertIn("release-steps: retry 2/5:", stdout)
        self.assertEqual(self.calls("sleep"), [["10"], ["20"]])
        self.assertEqual(len(self.pull_request_creations()), 3)

    def test_a_lookup_that_keeps_failing_is_refused_after_the_retries(self):
        self.answer_expired_fedora()
        self.answer("gh", ["pr", "list"], stderr="HTTP 502: Bad Gateway\n", status=1)
        status, _, stderr = self.refresh()
        self.assertEqual(status, 1)
        self.assertIn(f"the open pull requests for {TOOLS_SLUG} failed 5 times over 100 s (last answer: HTTP 502: Bad"
                      " Gateway); GitHub may still be catching up: run the same command again in a minute", stderr)
        self.assertEqual(self.calls("sleep"), [["10"], ["20"], ["30"], ["40"]])

    def test_a_caller_that_is_not_ready_refuses_before_the_tools_change(self):
        develop = self.git(self.tools, "rev-parse", "develop")
        (self.caller / "notes.txt").write_text("draft\n", encoding="utf-8")
        self.answer_expired_fedora()
        self.answer_pull_request()
        status, _, stderr = self.refresh("--caller", str(self.caller))
        self.assertEqual(status, 1)
        self.assertIn(f"caller {self.caller}: the tree has uncommitted changes (`git status --short` printed above);"
                      " commit or stash them, then run again; fix it before the refresh, nothing in"
                      " ai-tools-assets-tools was changed", stderr)
        self.assert_tools_unchanged(develop)
        self.assertEqual(self.calls("skopeo"), [])

    def test_a_caller_branch_already_on_origin_refuses_before_the_tools_change(self):
        self.git(self.caller, "push", "--quiet", "origin", f"develop:refs/heads/ci/{REFRESH_ID}-tools-fc44-digest")
        status, _, stderr = self.refresh("--caller", str(self.caller))
        self.assertEqual(status, 1)
        self.assertIn(f"caller {self.caller}: branch ci/{REFRESH_ID}-tools-fc44-digest already exists on origin; pick"
                      " the next letter for `--id`", stderr)

    def test_a_ready_caller_is_named_in_the_next_step(self):
        self.answer_expired_fedora()
        self.answer_pull_request()
        status, stdout, stderr = self.refresh("--caller", str(self.caller))
        self.assertEqual(status, 0, stderr)
        self.assertIn(f"release-steps: ok: caller {self.caller} is ready: its `uses:` lines pin @{OLD_PIN}", stdout)
        self.assertTrue(stdout.endswith(f"release-steps: next, in {self.caller}: bash ../tools/packaging/"
                                        f"release-steps.sh repin-tools --id {REFRESH_ID} <sha>\n"), stdout)


if __name__ == "__main__":
    unittest.main()
