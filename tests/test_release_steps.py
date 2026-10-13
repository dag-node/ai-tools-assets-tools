# SPDX-License-Identifier: MIT
"""Drives the subcommands of packaging/release-steps.sh that a checkout runs: check-images, refresh-images and
repin-tools.

The registry is `command_stub.py` standing in for skopeo, exported to the script as a bash function, so a test decides
which manifest each reference returns; gh, sleep and, where a test injects a failure, awk and date are stubbed the same
way. A test that fails a real command part way (git, sha256sum) wraps it with `fault`, which runs the real command on
every call but the one it fails. Each test runs a copy of the release steps whose image pins name digests of the stub's manifests, and asserts
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
import shlex
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

TESTS = pathlib.Path(__file__).resolve().parent
REPOSITORY = TESTS.parent
RELEASE_STEPS = REPOSITORY / "packaging" / "release-steps.sh"
SIGN_RPMS = REPOSITORY / "packaging" / "sign-rpms.sh"
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
        self.faults = {}
        self.fault_log = self.root / "faults.log"
        self.fault_log.write_text("", encoding="utf-8")

    def fault(self, command: str, when: str, stdout="", lines=None, status=2, call=None, warning=None, after=None):
        """Make <command> fail with <status> in the next runs of the release steps, on each call whose arguments,
        joined as " $* ", match the bash pattern <when>, or on the <call>-th such call alone; every other call runs
        the real command. A failing call prints <stdout>, or the first <lines> lines of the real command's output.
        With <warning>, the call instead prints it on stderr and runs the real command, keeping its status; with
        <after>, it runs the real command, then the bash command <after> in the script's shell, keeping the real
        command's status. Each such call is recorded in the fault log, so a test asserts that the failure it names
        happened. The test's own git calls run the real command."""
        counter = self.root / f"fault-{len(self.faults)}.count"
        counter.write_text("0", encoding="utf-8")
        if after is not None:
            action = f'command {command} "$@"; local fault_status=$?; {after}; return $fault_status;'
        elif warning is not None:
            action = f'printf %s {shlex.quote(warning)} >&2; command {command} "$@"; return $?;'
        elif lines is None:
            action = f"printf %s {shlex.quote(stdout)}; return {status};"
        else:
            action = f'command {command} "$@" | head -n {lines}; return {status};'
        due = "true" if call is None else f'[[ "$fault_count" -eq {call} ]]'
        self.faults[command] = (
            f'() {{ if [[ " $* " == {when} ]]; then local fault_count; fault_count=$(( $(<{shlex.quote(str(counter))}) + 1 ));'
            f' printf %s "$fault_count" >{shlex.quote(str(counter))};'
            f' if {due}; then printf "%s %s\\n" {command} "$*" >>{shlex.quote(str(self.fault_log))}; {action} fi; fi;'
            f' command {command} "$@"; }}')

    def faulted(self) -> list:
        """The calls a fault failed, as "<command> <arguments>"."""
        return self.fault_log.read_text(encoding="utf-8").splitlines()

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
        environment.update({f"BASH_FUNC_{command}%%": body for command, body in self.faults.items()})
        for command in (*(self.stubbed if stubbed is None else stubbed), *self.faults):
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


class RegistryCase(ReleaseStepsCase):
    """A copy of the release steps whose pins name the stub's manifests, and the answers for them."""

    def setUp(self):
        super().setUp()
        self.tools = self.root / "tools"
        self.script = self.write_release_steps(self.tools, {dist: f"{IMAGES[dist]}@{PINNED[dist]}" for dist in IMAGES})

    def pinned_reference(self, dist: str) -> str:
        repository = IMAGES[dist].rsplit(":", 1)[0]
        return f"{repository}@{PINNED[dist]}"

    def answer_pinned(self, *dists: str):
        self.answer_registry({self.pinned_reference(dist): manifest(f"{dist} pinned") for dist in dists})



class CheckImages(RegistryCase):
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

    def test_a_hash_that_fails_or_is_cut_short_fails_the_check(self):
        """The third sha256sum is the Fedora pin's; a failed one that prints the pinned digest does not pass it."""
        self.answer_pinned(*IMAGES)
        for stdout, status in (("", 2), ("e01fa6", 2), (f"{PINNED['fc44'][7:]}  -\n", 2), ("e01fa6  -\n", 0)):
            with self.subTest(stdout=stdout, status=status):
                self.fault("sha256sum", "*", stdout=stdout, status=status, call=3)
                exit_status, _, stderr = self.check()
                self.assertEqual(exit_status, 1, stderr)
                self.assertIn(f"fc44: the registry could not be read for {IMAGES['fc44']} (sha256sum ", stderr)
                self.assertNotIn("the tag names", stderr)
                self.assertEqual(len(self.faulted()), 1)
                self.fault_log.write_text("", encoding="utf-8")

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


class ChangeCase(RegistryCase):
    """A checkout of the tools and of a caller, each with a bare origin, and gh logged in with no open pull request.
    The caller pins the tools' initial commit, as a caller pins the release it last re-pinned to."""

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
        self.initial = self.git(self.tools, "rev-parse", "develop")
        self.caller = self.make_repository("caller", CALLER_URL, self.caller_pins())
        self.answer("gh", ["auth", "status"])
        self.answer("sleep", [])

    def caller_pins(self, ref=None, comment="v1.2.0") -> dict:
        """The caller's workflow files pinning the tools at <ref>, the initial commit where not given."""
        ref = ref or self.initial
        return {f".github/workflows/{name}": text for name, text in caller_workflows(ref, ref, comment).items()}

    def commit_tools(self, files: dict, message: str) -> str:
        """A commit on the tools' develop, pushed to origin, as a merged pull request leaves it."""
        sha = self.commit_files(self.tools, files, message)
        self.git(self.tools, "push", "--quiet", "origin", "develop")
        return sha

    def commit_caller(self, files: dict, message: str) -> str:
        sha = self.commit_files(self.caller, files, message)
        self.git(self.caller, "push", "--quiet", "origin", "develop")
        return sha

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

    def test_a_hash_that_fails_or_is_cut_short_is_not_pinned(self):
        """The third sha256sum is the hash of the manifest the Fedora tag names now, which the refresh pins."""
        develop = self.git(self.tools, "rev-parse", "develop")
        self.answer_expired_fedora()
        self.answer_pull_request()
        for stdout, status in (("", 2), ("e01fa6", 2), (f"{CURRENT['fc44'][7:]}  -\n", 2), ("e01fa6  -\n", 0)):
            with self.subTest(stdout=stdout, status=status):
                self.fault("sha256sum", "*", stdout=stdout, status=status, call=3)
                exit_status, _, stderr = self.refresh()
                self.assertEqual(exit_status, 1, stderr)
                self.assertIn(f"fc44: the registry could not be read for {IMAGES['fc44']} (sha256sum ", stderr)
                self.assertNotIn("the tag names", stderr)
                self.assertIn("check-images could not read every image (the lines above), so an expiry cannot be told"
                              " from an outage", stderr)
                self.assert_tools_unchanged(develop)
                self.assertEqual(len(self.faulted()), 1)
                self.fault_log.write_text("", encoding="utf-8")

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

    def test_a_branch_whose_script_cannot_be_read_is_refused_not_redated(self):
        """The branch exists, so its date is the one to keep; a failed read of it fell back to today's."""
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
        self.fault("git", f'*" show refs/heads/{REFRESH_BRANCH}:packaging/release-steps.sh "*')
        status, _, stderr = self.refresh(stubbed=stubbed)
        self.assertEqual(status, 1, stderr)
        self.assertIn(f"::error::release-steps: branch {REFRESH_BRANCH} exists and its packaging/release-steps.sh could"
                      f" not be read in {self.tools} (exit status 2, no output); nothing was written", stderr)
        self.assertEqual(self.git(self.tools, "rev-parse", REFRESH_BRANCH), tip)
        self.assertEqual(self.remote_branch(self.tools, REFRESH_BRANCH), "")
        self.assertEqual(len(self.faulted()), 1)

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

    def test_a_caller_branch_on_origin_is_refused_whatever_grep_answers(self):
        """The branch names of the caller's checked `ls-remote` answer are matched in bash; a grep that failed read as
        no match."""
        self.git(self.caller, "push", "--quiet", "origin", f"develop:refs/heads/ci/{REFRESH_ID}-tools-fc44-digest")
        self.answer_expired_fedora()
        self.answer_pull_request()
        self.fault("grep", '*" -oE "*', call=1)
        status, _, stderr = self.refresh("--caller", str(self.caller))
        self.assertEqual(status, 1)
        self.assertIn(f"caller {self.caller}: branch ci/{REFRESH_ID}-tools-fc44-digest already exists on origin; pick"
                      " the next letter for `--id`", stderr)

    def test_a_ready_caller_is_named_in_the_next_step(self):
        self.answer_expired_fedora()
        self.answer_pull_request()
        status, stdout, stderr = self.refresh("--caller", str(self.caller))
        self.assertEqual(status, 0, stderr)
        self.assertIn(f"release-steps: ok: caller {self.caller} is ready: its `uses:` lines pin @{self.initial}", stdout)
        self.assertTrue(stdout.endswith(f"release-steps: next, in {self.caller}: bash ../tools/packaging/"
                                        f"release-steps.sh repin-tools --id {REFRESH_ID} <sha>\n"), stdout)


REFRESH_DATE = "2026-11-03"
CALLER_BRANCH = f"ci/{REFRESH_ID}-tools-fc44-digest"
CALLER_PULL_REQUEST_URL = "https://github.com/dag-node/ai-tools-assets/pull/40"
RELEASE_CHANGELOG = CHANGELOG.replace("## [Unreleased]\n", """## [Unreleased]

## [1.3.0] - 2026-11-20

### Added

- **Image check.** `check-images` asks each registry for the pinned digest.
- **Digest refresh.** `refresh-images` and `repin-tools` open the pull
  requests of a refresh.

### Fixed

- A release pulls every image before the first signature.
""")


def gpg_runs() -> bool:
    """A test that signs a tag needs gpg and a tmpfs /dev/shm, where the release steps hold a keyring."""
    try:
        subprocess.run(["gpg", "--version"], capture_output=True, check=True)
        statfs = subprocess.run(["stat", "-f", "-c", "%T", "/dev/shm"], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return False
    return statfs.stdout.strip() == "tmpfs"


class RepinCase(ChangeCase):
    """A caller pinning the tools at 1.2.0, and the registry answering for the images of a refresh."""

    def setUp(self):
        super().setUp()
        self.answer_registry({self.pinned_reference(dist): manifest(f"{dist} pinned") for dist in ("el9", "el10")})
        repository = IMAGES["fc44"].rsplit(":", 1)[0]
        self.answer_registry({f"{repository}@{CURRENT['fc44']}": manifest("fc44 current")})

    def refreshed_script(self, dist="fc44", date=REFRESH_DATE, comment=True) -> str:
        """The tools' release steps with the <dist> pin at the stub's current digest, dated <date>."""
        script = (self.tools / "packaging" / "release-steps.sh").read_text(encoding="utf-8")
        resolved = f" # resolved {date}" if comment else ""
        return re.sub(rf"^{dist.upper()}_IMAGE=.*$", f"{dist.upper()}_IMAGE={IMAGES[dist]}@{CURRENT[dist]}{resolved}",
                      script, flags=re.M)

    def push_refresh(self, extra=None, **options) -> str:
        """A refresh of the Fedora 44 pin merged into the tools' develop, as refresh-images makes it."""
        files = {"packaging/release-steps.sh": self.refreshed_script(**options),
                 "CHANGELOG.md": (self.tools / "CHANGELOG.md").read_text(encoding="utf-8").replace(
                     "## [Unreleased]\n", "## [Unreleased]\n\n### Fixed\n\n- **Fedora 44 image.** A refresh.\n")}
        files.update(extra or {})
        return self.commit_tools(files, "build(packaging): pin the Fedora 44 image quay.io serves")

    def answer_caller_pull_request(self, existing=""):
        self.answer_open_pull_requests(CALLER_SLUG)
        self.answer("gh", ["pr", "list", "--repo", CALLER_SLUG, "--head"], stdout=existing)
        self.answer("gh", ["pr", "create"], stdout=f"{CALLER_PULL_REQUEST_URL}\n")

    def repin(self, *arguments: str, script=None):
        return self.run_steps(script or self.script, "repin-tools", "--id", REFRESH_ID, *arguments, cwd=self.caller)

    def caller_file(self, branch: str, name: str) -> str:
        return self.git(self.caller, "show", f"{branch}:.github/workflows/{name}")


class RepinTools(RepinCase):
    def test_a_refresh_commit_is_pinned_with_its_pull_request(self):
        sha = self.push_refresh()
        self.answer("gh", ["api", f"repos/{TOOLS_SLUG}/commits/{sha}/pulls"], stdout="31\n")
        self.answer_caller_pull_request()
        status, stdout, stderr = self.repin(sha)
        self.assertEqual(status, 0, stderr)
        comment = f"# develop {REFRESH_DATE}, fc44 digest, ai-tools-assets-tools#31"
        self.assertIn(f"    uses: dag-node/ai-tools-assets-tools/.github/workflows/validate.yml@{sha} {comment}\n",
                      self.caller_file(CALLER_BRANCH, "validate.yml"))
        self.assertIn(f"    uses: dag-node/ai-tools-assets-tools/.github/workflows/set-release.yml@{sha} {comment}\n",
                      self.caller_file(CALLER_BRANCH, "release.yml"))
        message = self.git(self.caller, "log", "-1", "--format=%B", CALLER_BRANCH)
        title = "ci: call the ai-tools-assets-tools commit that pins the Fedora 44 image"
        body = (f"The release and validate workflows called the v1.2.0 commit, whose release steps pin a Fedora 44"
                f" image digest quay.io no longer serves, so `check-images` failed every pull request here with"
                f" `manifest unknown`. They now call the ai-tools-assets-tools `develop` commit `{sha[:7]}`"
                f" (dag-node/ai-tools-assets-tools#31), which pins the digest `quay.io/fedora/fedora:44` resolves to"
                f" on {REFRESH_DATE}.\n\nNot a release of the tools: the next tools release carries the digest, and"
                " the next re-pin here names that release.")
        self.assertTrue(message.startswith(f"{title}\n\n"), message)
        generated = message[len(title) + 2:].split("\n\nSigned-off-by:")[0]
        self.assertEqual(unwrap(generated), body)
        creation = self.pull_request_creations()
        self.assertEqual(len(creation), 1)
        self.assertEqual(creation[0][3], CALLER_SLUG)
        self.assertEqual(creation[0][9], title)
        self.assertEqual(pathlib.Path(creation[0][11]).read_text(encoding="utf-8").rstrip("\n"), generated)
        self.assertEqual(self.remote_branch(self.caller, CALLER_BRANCH),
                         self.git(self.caller, "rev-parse", CALLER_BRANCH))
        self.assertIn(f"release-steps: repin-tools: checking 3 images at {sha[:7]}", stdout)
        self.assertIn(f"release-steps: ok: {sha[:7]} is on origin/develop and is ai-tools-assets-tools#31; from the"
                      f" pinned {self.initial[:7]} it changes image pins (fc44) and CHANGELOG.md alone", stdout)
        self.assertIn("release-steps: repin-tools: writing .github/workflows/release.yml, .github/workflows/validate.yml"
                      " in the scratch worktree", stdout)
        self.assertIn("release-steps: ok: .github/workflows/release.yml +1 -1, .github/workflows/validate.yml +1 -1 in"
                      " the scratch worktree", stdout)
        self.assertTrue(stdout.endswith(f"release-steps: next, after the merge: git ls-remote {CALLER_URL}"
                                        " refs/heads/develop\n"), stdout)

    def test_a_commit_that_changes_another_line_is_refused(self):
        script = (self.tools / "packaging" / "release-steps.sh").read_text(encoding="utf-8")
        sha = self.push_refresh({"packaging/release-steps.sh": self.refreshed_script().replace(
            "NFPM_VERSION=2.47.0", "NFPM_VERSION=2.48.0")})
        line = script.splitlines().index("NFPM_VERSION=2.47.0") + 1
        status, _, stderr = self.repin(sha, "--pr", "31")
        self.assertEqual(status, 1)
        self.assertIn(f"{sha[:7]} changes packaging/release-steps.sh:{line}, which is not an image pin or"
                      " `CHANGELOG.md`; a develop pin is for a digest refresh alone, and any other change reaches"
                      " a caller through a release", stderr)
        self.assertEqual(self.pull_request_creations(), [])

    def assert_caller_unchanged(self):
        self.assertEqual(self.git(self.caller, "branch", "--list", "ci/*"), "")
        self.assertEqual(self.remote_branch(self.caller, CALLER_BRANCH), "")
        self.assertEqual(self.pull_request_creations(), [])

    def test_a_pin_that_gains_an_unreleased_script_change_is_refused(self):
        script = (self.tools / "packaging" / "release-steps.sh").read_text(encoding="utf-8")
        line = script.splitlines().index("NFPM_VERSION=2.47.0") + 1
        self.commit_tools({"packaging/release-steps.sh": script.replace("NFPM_VERSION=2.47.0", "NFPM_VERSION=2.48.0")},
                          "build(packaging): nFPM 2.48.0")
        sha = self.push_refresh()
        status, _, stderr = self.repin(sha, "--pr", "31")
        self.assertEqual(status, 1)
        self.assertIn(f"from the pinned {self.initial[:7]}, {sha[:7]} changes packaging/release-steps.sh:{line}, which"
                      " is not an image pin or `CHANGELOG.md`; a develop pin is for a digest refresh alone, so this"
                      " caller re-pins through a release of the tools: cut the release, then run this command with its"
                      " tag (`vX.Y.Z`)", stderr)
        self.assert_caller_unchanged()

    def test_a_pin_that_gains_an_unreleased_workflow_change_is_refused(self):
        release_yml = (self.tools / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.commit_tools({".github/workflows/release.yml": release_yml + "# an unreleased change\n"}, "ci: a change")
        sha = self.push_refresh()
        status, _, stderr = self.repin(sha, "--pr", "31")
        self.assertEqual(status, 1)
        self.assertIn(f"from the pinned {self.initial[:7]}, {sha[:7]} changes .github/workflows/release.yml, which is"
                      " not an image pin or `CHANGELOG.md`; a develop pin is for a digest refresh alone, so this caller"
                      " re-pins through a release of the tools", stderr)
        self.assert_caller_unchanged()

    def test_two_refreshes_since_the_pin_are_pinned_together(self):
        repository = IMAGES["el9"].rsplit(":", 1)[0]
        self.answer_registry({f"{repository}@{CURRENT['el9']}": manifest("el9 current")})
        self.commit_tools({"packaging/release-steps.sh": self.refreshed_script("el9", "2026-11-01")},
                          "build(packaging): pin the Rocky 9 image quay.io serves")
        sha = self.push_refresh()
        self.answer("gh", ["pr", "list", "--repo", CALLER_SLUG, "--base", "develop"])
        self.answer("gh", ["pr", "list", "--repo", CALLER_SLUG, "--head"])
        self.answer("gh", ["pr", "create"], stdout=f"{CALLER_PULL_REQUEST_URL}\n")
        status, stdout, stderr = self.repin(sha, "--pr", "31")
        self.assertEqual(status, 0, stderr)
        branch = f"ci/{REFRESH_ID}-tools-el9-fc44-digest"
        self.assertIn(f"set-release.yml@{sha} # develop {REFRESH_DATE}, el9-fc44 digest, ai-tools-assets-tools#31\n",
                      self.caller_file(branch, "release.yml"))
        message = unwrap(self.git(self.caller, "log", "-1", "--format=%B", branch))
        self.assertTrue(message.startswith("ci: call the ai-tools-assets-tools commit that pins the Rocky 9 and Fedora 44"
                                           " images\n\nThe release and validate workflows called the v1.2.0 commit,"
                                           " whose release steps pin Rocky 9 and Fedora 44 image digests quay.io no"
                                           " longer serves, so"), message)
        self.assertIn(f"which pins the digest `{IMAGES['el9']}` resolves to on 2026-11-01 and the digest"
                      f" `{IMAGES['fc44']}` resolves to on {REFRESH_DATE}.", message)
        self.assertIn(f"from the pinned {self.initial[:7]} it changes image pins (el9, fc44) and CHANGELOG.md alone",
                      stdout)

    def test_a_refreshed_pin_without_its_date_is_refused(self):
        sha = self.push_refresh(comment=False)
        status, _, stderr = self.repin(sha, "--pr", "31")
        self.assertEqual(status, 1)
        self.assertIn(f"the FC44_IMAGE line at {sha[:7]} carries no `# resolved <date>` comment, which refresh-images"
                      " writes and the caller's pin comment names; refresh the pin with refresh-images", stderr)

    def test_a_caller_pinned_by_a_tag_is_read_at_origin_without_a_local_tag(self):
        self.git(self.tools, "tag", "v1.2.0", self.initial)
        self.git(self.tools, "push", "--quiet", "origin", "v1.2.0")
        self.git(self.tools, "tag", "-d", "v1.2.0")
        self.commit_caller(self.caller_pins("v1.2.0"), "pin the release by tag")
        sha = self.push_refresh()
        self.answer_caller_pull_request()
        status, stdout, stderr = self.repin(sha, "--pr", "31")
        self.assertEqual(status, 0, stderr)
        self.assertIn(f"from the pinned {self.initial[:7]} it changes image pins (fc44) and CHANGELOG.md alone", stdout)
        self.assertEqual(self.git(self.tools, "tag", "--list", "v1.2.0"), "")
        self.assertIn(f"called the v1.2.0 commit,", unwrap(self.git(self.caller, "log", "-1", "--format=%B", CALLER_BRANCH)))

    def test_a_caller_pin_absent_from_origin_is_refused(self):
        self.commit_caller(self.caller_pins("v9.9.9", "v9.9.9"), "pin a tag that does not exist")
        sha = self.push_refresh()
        status, _, stderr = self.repin(sha, "--pr", "31")
        self.assertEqual(status, 1)
        self.assertIn("the `uses:` lines pin @v9.9.9, which is not a commit, a tag or a branch on ai-tools-assets-tools"
                      " origin; fix the pins, then run again", stderr)
        self.assert_caller_unchanged()

    def test_a_rocky_refresh_is_pinned(self):
        """A dist other than the last in DIST_IMAGES order, which a false test at a function's end once dropped."""
        repository = IMAGES["el9"].rsplit(":", 1)[0]
        self.answer_registry({f"{repository}@{CURRENT['el9']}": manifest("el9 current")})
        self.answer_pinned("fc44")
        sha = self.commit_tools({"packaging/release-steps.sh": self.refreshed_script("el9", "2026-11-01"),
                                 "CHANGELOG.md": CHANGELOG.replace("## [Unreleased]\n", "## [Unreleased]\n\n### Fixed\n\n"
                                                                   "- **Rocky 9 image.** A refresh.\n")},
                                "build(packaging): pin the Rocky 9 image quay.io serves")
        self.answer_open_pull_requests(CALLER_SLUG)
        self.answer("gh", ["pr", "list", "--repo", CALLER_SLUG, "--head"])
        self.answer("gh", ["pr", "create"], stdout=f"{CALLER_PULL_REQUEST_URL}\n")
        status, stdout, stderr = self.repin(sha, "--pr", "31")
        self.assertEqual(status, 0, stderr)
        self.assertIn(f"set-release.yml@{sha} # develop 2026-11-01, el9 digest, ai-tools-assets-tools#31\n",
                      self.caller_file(f"ci/{REFRESH_ID}-tools-el9-digest", "release.yml"))
        self.assertIn(f"from the pinned {self.initial[:7]} it changes image pins (el9) and CHANGELOG.md alone", stdout)
        self.assertTrue(self.git(self.caller, "log", "-1", "--format=%s", f"ci/{REFRESH_ID}-tools-el9-digest")
                        == "ci: call the ai-tools-assets-tools commit that pins the Rocky 9 image")

    def test_a_workflow_file_without_a_final_newline_keeps_its_shape(self):
        files = self.caller_pins()
        files[".github/workflows/release.yml"] = files[".github/workflows/release.yml"].rstrip("\n")
        self.commit_caller(files, "no final newline")
        sha = self.push_refresh()
        self.answer_caller_pull_request()
        status, _, stderr = self.repin(sha, "--pr", "31")
        self.assertEqual(status, 0, stderr)
        self.assertIn(f"set-release.yml@{sha} # develop", self.caller_file(CALLER_BRANCH, "release.yml"))
        self.assertEqual(self.git(self.caller, "diff", "--numstat", f"develop..{CALLER_BRANCH}", "--",
                                  ".github/workflows/release.yml").split("\t")[:2], ["1", "1"])

    def test_a_pin_both_a_tag_and_a_branch_carry_is_refused(self):
        self.git(self.tools, "tag", "v1.2.0", self.initial)
        self.git(self.tools, "push", "--quiet", "origin", "v1.2.0")
        self.commit_tools({"README.md": "unreleased\n"}, "an unreleased change")
        self.git(self.tools, "push", "--quiet", "origin", "develop:refs/heads/v1.2.0")
        self.commit_caller(self.caller_pins("v1.2.0"), "pin by a name a branch carries too")
        sha = self.push_refresh()
        status, _, stderr = self.repin(sha, "--pr", "31")
        self.assertEqual(status, 1)
        self.assertIn("the `uses:` lines pin @v1.2.0, which names both a tag and a branch on ai-tools-assets-tools"
                      " origin; pin a tag or a full commit SHA, then run again", stderr)
        self.assert_caller_unchanged()

    def test_a_pull_request_number_github_malforms_is_refused(self):
        sha = self.push_refresh()
        for answer in ("31\n    unexpected: true\n", "abc\n", "0\n"):
            self.answer("gh", ["api", f"repos/{TOOLS_SLUG}/commits/{sha}/pulls"], stdout=answer, first=True)
            status, _, stderr = self.repin(sha)
            self.assertEqual(status, 1, answer)
            self.assertIn(f"GitHub names the pull request of commit {sha[:7]} as `{' '.join(answer.split())}`, not a"
                          " number; pass it with `--pr <number>`", stderr)
            self.assert_caller_unchanged()

    def test_a_diagnostic_beside_the_pull_request_number_stays_out_of_the_pin(self):
        sha = self.push_refresh()
        self.answer("gh", ["api", f"repos/{TOOLS_SLUG}/commits/{sha}/pulls"], stdout="31\n",
                    stderr="warning: a diagnostic gh prints\n")
        self.answer_caller_pull_request()
        status, _, stderr = self.repin(sha)
        self.assertEqual(status, 0, stderr)
        self.assertIn(f"set-release.yml@{sha} # develop {REFRESH_DATE}, fc44 digest, ai-tools-assets-tools#31\n",
                      self.caller_file(CALLER_BRANCH, "release.yml"))

    def test_conflicting_pin_comments_are_refused(self):
        sha = self.push_refresh()
        files = self.caller_pins()
        files[".github/workflows/release.yml"] = self.caller_pins(comment="v9.9.9")[".github/workflows/release.yml"]
        self.commit_caller(files, "a comment that disagrees")
        status, _, stderr = self.repin(sha, "--pr", "31")
        self.assertEqual(status, 1)
        self.assertIn("the `uses:` lines carry different pin comments (.github/workflows/release.yml: `# v9.9.9`,"
                      " .github/workflows/validate.yml: `# v1.2.0`); make them one, then run again", stderr)
        self.assert_caller_unchanged()

    def test_a_readable_comment_beside_an_unreadable_one_leaves_the_ref_to_stand(self):
        sha = self.push_refresh()
        files = self.caller_pins()
        files[".github/workflows/validate.yml"] = self.caller_pins(comment="")[".github/workflows/validate.yml"]
        self.commit_caller(files, "a comment missing")
        self.answer_caller_pull_request()
        status, _, stderr = self.repin(sha, "--pr", "31")
        self.assertEqual(status, 0, stderr)
        self.assertIn(f"the comment after @{self.initial} in .github/workflows/validate.yml is not `# vX.Y.Z` or"
                      f" `# develop …`; the body names the previous pin as {self.initial[:7]}", stderr)
        self.assertIn(f"called the {self.initial[:7]} commit,",
                      unwrap(self.git(self.caller, "log", "-1", "--format=%B", CALLER_BRANCH)))

    def test_a_commit_that_changes_another_file_is_refused(self):
        sha = self.push_refresh({"README.md": "a change\n"})
        status, _, stderr = self.repin(sha, "--pr", "31")
        self.assertEqual(status, 1)
        self.assertIn(f"{sha[:7]} changes README.md, which is not an image pin or `CHANGELOG.md`", stderr)

    def assert_refused_with_the_caller_unchanged(self, status: int, stderr: str, *phrases: str):
        self.assertEqual(status, 1, stderr)
        for phrase in phrases:
            self.assertIn(phrase, stderr)
        self.assert_caller_unchanged()

    def test_a_file_list_that_fails_is_refused(self):
        """A failed `git diff --name-only` read as its output lists no file, or the allowed files alone, so a commit
        that changes another file passed as a refresh."""
        sha = self.push_refresh({"README.md": "a change\n"})
        self.answer_caller_pull_request()
        for stdout in ("", "CHANGELOG.md\npackaging/release-steps.sh\n"):
            with self.subTest(stdout=stdout):
                self.fault("git", '*" diff --name-only "*', stdout=stdout)
                status, _, stderr = self.repin(sha, "--pr", "31")
                self.assert_refused_with_the_caller_unchanged(
                    status, stderr, f"::error::release-steps: the files {sha[:7]} changes against {sha}^ could not be"
                    f" listed in {self.tools} (`git diff`: ", "); nothing was written: run the same command again")
                self.assertEqual(len(self.faulted()), 1)
                self.fault_log.write_text("", encoding="utf-8")

    def test_a_line_diff_that_fails_part_way_is_refused(self):
        """A failed `git diff --unified=0` read as its output passes the hunks it printed before it failed, so a later
        change to another line of the script passed as a refresh."""
        sha = self.push_refresh({"packaging/release-steps.sh": self.refreshed_script() + "\n# an unrelated change\n"})
        self.answer_caller_pull_request()
        hunks = self.git(self.tools, "diff", "--unified=0", "--no-color", f"{sha}^", sha, "--",
                         "packaging/release-steps.sh").splitlines()
        second_hunk = [index for index, line in enumerate(hunks) if line.startswith("@@")][1]
        for lines in (0, second_hunk):
            with self.subTest(lines=lines):
                self.fault("git", '*" diff --unified=0 "*', lines=lines)
                status, _, stderr = self.repin(sha, "--pr", "31")
                self.assert_refused_with_the_caller_unchanged(
                    status, stderr, f"::error::release-steps: the lines {sha[:7]} changes in packaging/release-steps.sh"
                    f" against {sha}^ could not be read in {self.tools} (`git diff`: ")
                self.assertEqual(len(self.faulted()), 1)
                self.fault_log.write_text("", encoding="utf-8")

    def test_a_script_read_that_fails_part_way_is_refused(self):
        """Each `git show` of the target's script that fails after its pins and DIST_IMAGES printed: the parent check's,
        the check from the caller's pin, and the read whose pins the registry is asked for."""
        sha = self.push_refresh()
        self.answer_caller_pull_request()
        script = self.git(self.tools, "show", f"{sha}:packaging/release-steps.sh").splitlines()
        lines = next(index for index, line in enumerate(script) if line.startswith("declare -A DIST_IMAGES=")) + 1
        for call in (1, 2, 3):
            with self.subTest(call=call):
                self.fault("git", '*" show "*":packaging/release-steps.sh "', lines=lines, call=call)
                status, _, stderr = self.repin(sha, "--pr", "31")
                self.assert_refused_with_the_caller_unchanged(
                    status, stderr, f"::error::release-steps: packaging/release-steps.sh at {sha[:7]} could not be read"
                    f" in {self.tools} (`git show`: ")
                self.assertEqual(len(self.faulted()), 1)
                self.fault_log.write_text("", encoding="utf-8")

    def test_a_status_read_that_fails_keeps_an_uncommitted_edit_out_of_the_pin(self):
        """A failed `git status --porcelain` read as a clean tree, and the pins are read from the working tree while
        the change is built on committed develop, so an uncommitted edit of a `uses:` line reached the pull request."""
        sha = self.push_refresh()
        workflow = self.caller / ".github" / "workflows" / "release.yml"
        edited = workflow.read_text(encoding="utf-8").replace("set-release.yml@", "validate.yml@")
        workflow.write_text(edited, encoding="utf-8")
        self.answer_caller_pull_request()
        self.fault("git", '*" status --porcelain "*')
        status, _, stderr = self.repin(sha, "--pr", "31")
        self.assert_refused_with_the_caller_unchanged(
            status, stderr, f"::error::release-steps: `git status` could not read the tree of {self.caller} (")
        self.assertEqual(len(self.faulted()), 1)
        self.assertEqual(workflow.read_text(encoding="utf-8"), edited)

    def test_a_status_whose_output_cannot_be_read_back_is_refused(self):
        """`git status` succeeds and its captured output is replaced by a directory before the helper reads it: the
        read-back failed while the status stayed 0, and the dirty tree read as clean."""
        sha = self.push_refresh()
        workflow = self.caller / ".github" / "workflows" / "release.yml"
        edited = workflow.read_text(encoding="utf-8").replace("set-release.yml@", "validate.yml@")
        workflow.write_text(edited, encoding="utf-8")
        self.answer_caller_pull_request()
        self.fault("git", '*" status --porcelain "*', after='rm -f "$_RELEASE_STEPS__TEMP_DIR/read-output";'
                   ' mkdir "$_RELEASE_STEPS__TEMP_DIR/read-output"')
        status, _, stderr = self.repin(sha, "--pr", "31")
        self.assert_refused_with_the_caller_unchanged(
            status, stderr, f"::error::release-steps: `git status` could not read the tree of {self.caller} (the output"
            " of `git` could not be read back)")
        self.assertEqual(len(self.faulted()), 1)
        self.assertEqual(workflow.read_text(encoding="utf-8"), edited)

    def test_a_pin_both_a_tag_and_a_branch_carry_is_refused_whatever_awk_answers(self):
        """The rows of the checked `ls-remote` answer are counted in bash, so a failing awk does not count them as
        one."""
        self.git(self.tools, "tag", "v1.2.0", self.initial)
        self.git(self.tools, "push", "--quiet", "origin", "v1.2.0")
        self.git(self.tools, "push", "--quiet", "origin", "develop:refs/heads/v1.2.0")
        self.commit_caller(self.caller_pins("v1.2.0"), "pin by a name a branch carries too")
        sha = self.push_refresh()
        self.fault("awk", "*")
        status, _, stderr = self.repin(sha, "--pr", "31")
        self.assert_refused_with_the_caller_unchanged(
            status, stderr, "the `uses:` lines pin @v1.2.0, which names both a tag and a branch on"
            " ai-tools-assets-tools origin")

    def test_a_release_commit_is_not_a_refresh(self):
        sha = self.commit_tools({"CHANGELOG.md": RELEASE_CHANGELOG}, "chore: release 1.3.0")
        status, _, stderr = self.repin(sha, "--pr", "31", "--dry-run")
        self.assertEqual(status, 1)
        self.assertIn(f"{sha[:7]} changes no image pin; a develop pin is for a digest refresh alone", stderr)

    def test_a_commit_not_on_develop_is_refused(self):
        self.git(self.tools, "switch", "--quiet", "-c", "topic")
        sha = self.commit_files(self.tools, {"CHANGELOG.md": CHANGELOG + "\n"}, "unmerged")
        self.git(self.tools, "switch", "--quiet", "develop")
        status, _, stderr = self.repin(sha[:12])
        self.assertEqual(status, 1)
        self.assertIn(f"{sha[:12]} is not on ai-tools-assets-tools `origin/develop` (fetched now); a develop pin"
                      " names a merged commit: merge the pull request, then run this command again", stderr)

    def test_unequal_pins_in_the_caller_are_refused(self):
        sha = self.push_refresh()
        self.commit_files(self.caller, {".github/workflows/validate.yml":
                                        caller_workflows(validate_ref="v1")["validate.yml"]}, "drift")
        self.git(self.caller, "push", "--quiet", "origin", "develop")
        status, _, stderr = self.repin(sha, "--pr", "31")
        self.assertEqual(status, 1)
        self.assertIn("the `uses:` lines pin different refs", stderr)

    def test_a_commit_github_names_no_pull_request_for_is_refused_after_the_retries(self):
        sha = self.push_refresh()
        self.answer("gh", ["api", f"repos/{TOOLS_SLUG}/commits/{sha}/pulls"])
        status, stdout, stderr = self.repin(sha)
        self.assertEqual(status, 1)
        self.assertIn(f"release-steps: retry 4/5: the pull request of commit for {sha[:7]} (an empty answer);"
                      " waiting 40 s", stdout)
        self.assertIn(f"no pull request names commit {sha[:7]} (`gh api repos/{TOOLS_SLUG}/commits/{sha[:7]}/pulls`"
                      " answered: an empty answer, 5 times over 100 s); pass it with `--pr <number>`", stderr)

    def test_a_pull_request_github_names_late_is_waited_for(self):
        sha = self.push_refresh()
        self.answer("gh", ["api", f"repos/{TOOLS_SLUG}/commits/{sha}/pulls"], times=2)
        self.answer("gh", ["api", f"repos/{TOOLS_SLUG}/commits/{sha}/pulls"], stdout="31\n")
        self.answer_caller_pull_request()
        status, _, stderr = self.repin(sha)
        self.assertEqual(status, 0, stderr)
        self.assertEqual(self.calls("sleep"), [["10"], ["20"]])
        self.assertIn("ai-tools-assets-tools#31", self.caller_file(CALLER_BRANCH, "release.yml"))

    def test_pins_already_at_the_target_change_nothing(self):
        sha = self.push_refresh()
        self.commit_files(self.caller, {f".github/workflows/{name}": text for name, text in
                                        caller_workflows(sha, sha, "develop").items()}, "pinned")
        self.git(self.caller, "push", "--quiet", "origin", "develop")
        status, stdout, stderr = self.repin(sha, "--pr", "31")
        self.assertEqual(status, 0, stderr)
        self.assertIn(f"release-steps: ok: the `uses:` lines already pin @{sha}; nothing to do", stdout)
        self.assertEqual(self.git(self.caller, "branch", "--list", "ci/*"), "")

    def test_an_unreadable_old_comment_is_named_and_the_ref_stands_for_it(self):
        sha = self.push_refresh()
        self.commit_caller(self.caller_pins(comment="tools"), "comment")
        self.answer_caller_pull_request()
        status, _, stderr = self.repin(sha, "--pr", "31")
        self.assertEqual(status, 0, stderr)
        self.assertIn(f"the comment after @{self.initial} in .github/workflows/release.yml is not `# vX.Y.Z` or"
                      f" `# develop …`; the body names the previous pin as {self.initial[:7]}", stderr)
        self.assertIn(f"called the {self.initial[:7]} commit,",
                      unwrap(self.git(self.caller, "log", "-1", "--format=%B", CALLER_BRANCH)))

    def test_a_rerun_after_the_push_opens_the_pull_request_once(self):
        sha = self.push_refresh()
        self.answer_open_pull_requests(CALLER_SLUG)
        self.answer("gh", ["pr", "list", "--repo", CALLER_SLUG, "--head"])
        self.answer("gh", ["pr", "create"], stderr="HTTP 502: Bad Gateway\n", status=1, times=1)
        self.answer("gh", ["pr", "create"], stdout=f"{CALLER_PULL_REQUEST_URL}\n")
        self.assertEqual(self.repin(sha, "--pr", "31")[0], 1)
        tip = self.git(self.caller, "rev-parse", CALLER_BRANCH)
        status, stdout, stderr = self.repin(sha, "--pr", "31")
        self.assertEqual(status, 0, stderr)
        self.assertIn(f"release-steps: ok: branch {CALLER_BRANCH} already carries this change; continuing from it",
                      stdout)
        self.assertIn(f"release-steps: ok: origin/{CALLER_BRANCH} = {tip[:7]}, pushed before", stdout)
        self.assertEqual(len(self.pull_request_creations()), 2)

    REDIRECT_WARNING = "warning: redirecting to https://github.com/dag-node/ai-tools-assets.git/\n"

    def test_a_warning_beside_the_branch_read_is_not_a_branch_on_origin(self):
        """git prints a redirect warning on stderr beside a successful `ls-remote`; merged into the answer, it read as
        the tip of a branch on origin and refused the re-pin."""
        sha = self.push_refresh()
        self.answer_caller_pull_request()
        self.fault("git", f'*" ls-remote origin refs/heads/{CALLER_BRANCH} "*', warning=self.REDIRECT_WARNING)
        status, _, stderr = self.repin(sha, "--pr", "31")
        self.assertEqual(status, 0, stderr)
        self.assertEqual(self.remote_branch(self.caller, CALLER_BRANCH), self.git(self.caller, "rev-parse", CALLER_BRANCH))
        self.assertEqual(len(self.pull_request_creations()), 1)
        self.assertGreaterEqual(len(self.faulted()), 1)

    def test_a_warning_beside_the_branch_read_resumes_after_the_push(self):
        sha = self.push_refresh()
        self.answer_open_pull_requests(CALLER_SLUG)
        self.answer("gh", ["pr", "list", "--repo", CALLER_SLUG, "--head"])
        self.answer("gh", ["pr", "create"], stderr="HTTP 502: Bad Gateway\n", status=1, times=1)
        self.answer("gh", ["pr", "create"], stdout=f"{CALLER_PULL_REQUEST_URL}\n")
        self.assertEqual(self.repin(sha, "--pr", "31")[0], 1)
        tip = self.git(self.caller, "rev-parse", CALLER_BRANCH)
        self.fault("git", f'*" ls-remote origin refs/heads/{CALLER_BRANCH} "*', warning=self.REDIRECT_WARNING)
        status, stdout, stderr = self.repin(sha, "--pr", "31")
        self.assertEqual(status, 0, stderr)
        self.assertIn(f"release-steps: ok: origin/{CALLER_BRANCH} = {tip[:7]}, pushed before", stdout)
        self.assertEqual(len(self.pull_request_creations()), 2)
        self.assertGreaterEqual(len(self.faulted()), 1)

    def test_a_warning_beside_an_absent_tag_does_not_read_as_the_tag(self):
        self.fault("git", '*" ls-remote origin refs/tags/v9.9.9 "*', warning=self.REDIRECT_WARNING)
        status, _, stderr = self.repin("v9.9.9")
        self.assertEqual(status, 1)
        self.assertIn("tag v9.9.9 is not on ai-tools-assets-tools origin; push the tag, or check the spelling", stderr)
        self.assertEqual(len(self.faulted()), 1)

    def test_the_tools_checkout_is_refused_as_a_caller_whatever_git_answers(self):
        """The caller check compares paths with the script's directory; it read the tools' toplevel with git, and
        a failed read passed the tools' own checkout as a caller."""
        self.fault("git", f'*" -C {self.tools}/packaging rev-parse --show-toplevel "*')
        status, _, stderr = self.run_steps(self.script, "repin-tools", "--id", REFRESH_ID, "abc1234", cwd=self.tools)
        self.assertEqual(status, 1)
        self.assertIn(f"{self.tools} is not a checkout of a caller with a `.github/workflows/`; run from the"
                      " repository root", stderr)

    def test_a_parent_read_that_fails_is_refused(self):
        sha = self.push_refresh()
        self.answer_caller_pull_request()
        self.fault("git", '*" rev-list --parents "*', stdout=f"{sha} {self.git(self.tools, 'rev-parse', f'{sha}^')}\n")
        status, _, stderr = self.repin(sha, "--pr", "31")
        self.assert_refused_with_the_caller_unchanged(
            status, stderr, f"::error::release-steps: the parents of {sha[:7]} could not be read in {self.tools} (")
        self.assertEqual(len(self.faulted()), 1)

    def test_two_refreshes_on_one_date_name_it_once_whatever_sort_answers(self):
        """The distinct dates are counted in bash; a sort that failed counted none and named each date apart."""
        repository = IMAGES["el9"].rsplit(":", 1)[0]
        self.answer_registry({f"{repository}@{CURRENT['el9']}": manifest("el9 current")})
        self.commit_tools({"packaging/release-steps.sh": self.refreshed_script("el9", REFRESH_DATE)},
                          "build(packaging): pin the Rocky 9 image quay.io serves")
        sha = self.push_refresh()
        self.answer_caller_pull_request()
        self.fault("sort", "*")
        status, _, stderr = self.repin(sha, "--pr", "31")
        self.assertEqual(status, 0, stderr)
        message = unwrap(self.git(self.caller, "log", "-1", "--format=%B", f"ci/{REFRESH_ID}-tools-el9-fc44-digest"))
        self.assertIn(f"which pins the digests `{IMAGES['el9']}` and `{IMAGES['fc44']}` resolve to on {REFRESH_DATE}.",
                      message)

    def test_a_last_line_without_a_newline_keeps_its_bytes(self):
        """The `uses:` line is the file's last and has no final newline. A failed read of the last byte read as
        a newline, and the rewrite added one where numstat still saw one line changed."""
        files = self.caller_pins()
        lines = files[".github/workflows/release.yml"].splitlines()
        uses = next(line for line in lines if "uses:" in line)
        original = "\n".join([line for line in lines if line != uses] + [uses])
        files[".github/workflows/release.yml"] = original
        self.commit_caller(files, "the pin last, without a final newline")
        sha = self.push_refresh()
        self.answer_caller_pull_request()
        self.fault("tail", '*" -c 1 "*')
        status, _, stderr = self.repin(sha, "--pr", "31")
        self.assert_refused_with_the_caller_unchanged(
            status, stderr, "::error::release-steps: the last byte of ", "; the file is unchanged")
        self.assertEqual(len(self.faulted()), 1)
        self.faults.clear()
        status, _, stderr = self.repin(sha, "--pr", "31")
        self.assertEqual(status, 0, stderr)
        written = subprocess.run(["git", "-C", str(self.caller), "show", f"{CALLER_BRANCH}:.github/workflows/release.yml"],
                                 env=self.environment(stubbed=()), capture_output=True, check=True).stdout
        pinned = uses.replace(f"@{self.initial} # v1.2.0",
                              f"@{sha} # develop {REFRESH_DATE}, fc44 digest, ai-tools-assets-tools#31")
        self.assertEqual(written, original.replace(uses, pinned).encode("utf-8"))

    def test_a_count_read_that_fails_is_refused_with_a_message(self):
        """`set -e` ended the run on a failed `git rev-list --count` with its own status and no `::error::` line."""
        sha = self.push_refresh()
        self.fault("git", '*" rev-list --left-right "*')
        status, _, stderr = self.repin(sha, "--pr", "31")
        self.assert_refused_with_the_caller_unchanged(
            status, stderr, f"::error::release-steps: `git rev-list` could not compare develop with origin/develop in"
            f" {self.caller} (exit status 2, no output); run the same command again")
        self.assertEqual(len(self.faulted()), 1)

    def test_a_commit_that_cannot_be_read_back_is_refused_and_a_rerun_finishes(self):
        sha = self.push_refresh()
        self.answer_caller_pull_request()
        self.fault("git", '*" rev-parse HEAD "*')
        status, _, stderr = self.repin(sha, "--pr", "31")
        self.assert_refused_with_the_caller_unchanged(
            status, stderr, f"::error::release-steps: the commit on {CALLER_BRANCH} was made in the scratch worktree and"
            " could not be read back; no branch was created and nothing was pushed: run the same command again")
        self.assertEqual(len(self.faulted()), 1)
        self.faults.clear()
        status, _, stderr = self.repin(sha, "--pr", "31")
        self.assertEqual(status, 0, stderr)
        self.assertEqual(self.remote_branch(self.caller, CALLER_BRANCH), self.git(self.caller, "rev-parse", CALLER_BRANCH))
        self.assertEqual(len(self.pull_request_creations()), 1)

    def test_an_origin_unreadable_after_the_push_is_refused_and_a_rerun_opens_the_pull_request(self):
        sha = self.push_refresh()
        self.answer_caller_pull_request()
        self.fault("git", f'*" ls-remote origin refs/heads/{CALLER_BRANCH} "*', call=2)
        status, _, stderr = self.repin(sha, "--pr", "31")
        self.assertEqual(status, 1, stderr)
        self.assertIn(f"::error::release-steps: the push of {CALLER_BRANCH} ran and origin could not be read after it"
                      " (exit status 2, no output); the pull request is not open: run the same command again", stderr)
        tip = self.git(self.caller, "rev-parse", CALLER_BRANCH)
        self.assertEqual(self.remote_branch(self.caller, CALLER_BRANCH), tip)
        self.assertEqual(self.pull_request_creations(), [])
        self.faults.clear()
        status, stdout, stderr = self.repin(sha, "--pr", "31")
        self.assertEqual(status, 0, stderr)
        self.assertIn(f"release-steps: ok: origin/{CALLER_BRANCH} = {tip[:7]}, pushed before", stdout)
        self.assertEqual(len(self.pull_request_creations()), 1)

    def test_a_pull_request_answer_without_a_url_ends_in_the_ok_line(self):
        sha = self.push_refresh()
        self.answer_open_pull_requests(CALLER_SLUG)
        self.answer("gh", ["pr", "list", "--repo", CALLER_SLUG, "--head"])
        self.answer("gh", ["pr", "create"], stdout="created\n")
        status, stdout, stderr = self.repin(sha, "--pr", "31")
        self.assertEqual(status, 0, stderr)
        self.assertIn("release-steps: ok: the pull request is open\n", stdout)
        self.assertEqual(len(self.pull_request_creations()), 1)

    def test_a_dry_run_writes_nothing(self):
        sha = self.push_refresh()
        self.answer_caller_pull_request()
        status, stdout, stderr = self.repin(sha, "--pr", "31", "--dry-run")
        self.assertEqual(status, 0, stderr)
        self.assertIn(f"+    uses: dag-node/ai-tools-assets-tools/.github/workflows/validate.yml@{sha}", stdout)
        self.assertIn("\nci: call the ai-tools-assets-tools commit that pins the Fedora 44 image\n", stdout)
        self.assertEqual(self.git(self.caller, "branch", "--list", "ci/*"), "")
        self.assertEqual(self.pull_request_creations(), [])

    def test_a_run_from_the_tools_checkout_is_refused(self):
        status, _, stderr = self.run_steps(self.script, "repin-tools", "--id", REFRESH_ID, "abc1234", cwd=self.tools)
        self.assertEqual(status, 1)
        self.assertIn(f"{self.tools} is not a checkout of a caller with a `.github/workflows/`; run from the"
                      " repository root", stderr)

    def test_a_script_outside_a_tools_tree_is_refused(self):
        loose = self.root / "loose" / "packaging" / "release-steps.sh"
        loose.parent.mkdir(parents=True)
        shutil.copy(self.script, loose)
        status, _, stderr = self.repin("abc1234", script=loose)
        self.assertEqual(status, 1)
        self.assertIn("run from ai-tools-assets-tools's tree, in the caller's checkout: `bash"
                      " <ai-tools-assets-tools>/packaging/release-steps.sh repin-tools …`", stderr)

    def test_no_target_is_refused(self):
        status, _, stderr = self.repin()
        self.assertEqual(status, 1)
        self.assertIn("give a commit SHA of the tools (a digest refresh) or a release tag `vX.Y.Z`", stderr)

    def test_a_caller_without_a_pin_of_the_tools_is_refused(self):
        self.commit_files(self.caller, {".github/workflows/validate.yml": "name: validate\non: push\njobs: {}\n",
                                        ".github/workflows/release.yml": "name: release\non: push\njobs: {}\n"},
                          "no pins")
        self.git(self.caller, "push", "--quiet", "origin", "develop")
        status, _, stderr = self.repin("abc1234")
        self.assertEqual(status, 1)
        self.assertIn("no `uses: dag-node/ai-tools-assets-tools/.github/workflows/…` line under .github/workflows/;"
                      " nothing to re-pin", stderr)

    def test_a_prerelease_tag_is_refused(self):
        status, _, stderr = self.repin("v1.3.0-rc.1")
        self.assertEqual(status, 1)
        self.assertIn("v1.3.0-rc.1 is a prerelease; a caller pins a final release", stderr)

    def test_a_tag_absent_from_origin_is_refused(self):
        status, _, stderr = self.repin("v9.9.9")
        self.assertEqual(status, 1)
        self.assertIn("tag v9.9.9 is not on ai-tools-assets-tools origin; push the tag, or check the spelling", stderr)

    def test_a_lightweight_tag_is_refused(self):
        self.git(self.tools, "tag", "v1.3.0")
        self.git(self.tools, "push", "--quiet", "origin", "v1.3.0")
        status, _, stderr = self.repin("v1.3.0")
        self.assertEqual(status, 1)
        self.assertIn("v1.3.0 is not an annotated tag; a release tag is created with git tag -s", stderr)

    def test_an_unsigned_tag_is_refused(self):
        self.git(self.tools, "tag", "-a", "-m", "1.3.0", "v1.3.0")
        self.git(self.tools, "push", "--quiet", "origin", "v1.3.0")
        status, _, stderr = self.repin("v1.3.0")
        self.assertEqual(status, 1)
        self.assertIn("v1.3.0 carries no PGP signature; a release tag is created with git tag -s", stderr)


def shell_functions(*names: str) -> str:
    """The text of the named functions of the release steps, for a test that runs a text parser on its own."""
    text = RELEASE_STEPS.read_text(encoding="utf-8")
    functions = []
    for name in names:
        match = (re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n", text, flags=re.M | re.S)
                 or re.search(rf"^{re.escape(name)}\(\) \{{ .*\}}\n", text, flags=re.M))
        assert match, name
        functions.append(match.group(0))
    return "".join(functions)


class Cleanup(unittest.TestCase):
    """The exit traps of the release steps and of sign-rpms.sh, run with a cleanup command that fails, beside a control
    where it succeeds: each removal still runs, and the failure is named in a warning."""

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def keyring(self, name: str) -> pathlib.Path:
        """A scratch directory shaped as one holding an imported secret key: a public test marker, no key."""
        directory = self.root / name
        (directory / "gnupg" / "private-keys-v1.d").mkdir(parents=True)
        (directory / "private-keys-v1.d").mkdir()
        (directory / "marker").write_text("public test marker\n", encoding="utf-8")
        return directory

    def test_the_signers_trap_removes_the_tree_when_the_agent_does_not_stop(self):
        text = SIGN_RPMS.read_text(encoding="utf-8")
        definitions = text[:text.rindex('\nmain "$@"')]
        trap = re.search(r"^\s*(trap .* EXIT)$", text, flags=re.M).group(1)
        for status in (0, 2):
            with self.subTest(status=status):
                workdir = self.keyring(f"workdir-{status}")
                script = f'{definitions}\nworkdir="$1"\ngpgconf() {{ return {status}; }}\n{trap}\nexit 0\n'
                completed = subprocess.run([BASH, "-c", script, "_", str(workdir)], capture_output=True, text=True)
                self.assertFalse(workdir.exists(), completed.stderr)
                self.assertRegex(completed.stdout, r"^sign-rpms: the scratch tree held [0-9]+ KiB\n$")
                warning = f"::warning::sign-rpms: the gpg-agent of {workdir}/gnupg did not stop"
                (self.assertIn if status else self.assertNotIn)(warning, completed.stderr)

    def test_the_release_steps_trap_removes_every_directory_when_a_removal_before_it_fails(self):
        functions = shell_functions("_release_steps__remove_scratch_and_container", "_release_steps__warn")
        for status in (0, 1):
            with self.subTest(status=status):
                first, second = self.keyring(f"first-{status}"), self.keyring(f"second-{status}")
                script = (f'set -euo pipefail\n{functions}container=ai-tools-assets-sign-1\n_RELEASE_STEPS__WORKTREE=""\n'
                          f'scratch_directories=("$1" "$2")\nsudo() {{ return {status}; }}\n'
                          f'gpgconf() {{ return {status}; }}\ntrap _release_steps__remove_scratch_and_container EXIT\n'
                          'exit 0\n')
                completed = subprocess.run([BASH, "-c", script, "_", str(first), str(second)], capture_output=True,
                                           text=True)
                self.assertFalse(first.exists(), completed.stderr)
                self.assertFalse(second.exists(), completed.stderr)
                warnings = [f"::warning::release-steps: the container ai-tools-assets-sign-1 was not removed",
                            f"::warning::release-steps: the gpg-agent of {first} did not stop",
                            f"::warning::release-steps: the gpg-agent of {second} did not stop"]
                for warning in warnings:
                    (self.assertIn if status else self.assertNotIn)(warning, completed.stderr)


class SwapCheck(unittest.TestCase):
    """The check preflight, sign-files and sign-rpm run before the key enters a process, on /proc/swaps fixtures."""

    HEADER = "Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n"

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.root)

    def check(self, swaps: pathlib.Path):
        functions = shell_functions("_release_steps__require_swap_off", "_release_steps__die",
                                    "_release_steps__format_one_line")
        script = (f'set -euo pipefail\n_RELEASE_STEPS__DIE_PREFIX=""\n_RELEASE_STEPS__DIE_SUFFIX=""\n{functions}'
                  '_RELEASE_STEPS__PROC_SWAPS="$1"\n_release_steps__require_swap_off\necho checked\n')
        completed = subprocess.run([BASH, "-c", script, "_", str(swaps)], capture_output=True, text=True)
        return completed.returncode, completed.stdout, completed.stderr

    def test_the_header_alone_passes(self):
        swaps = self.root / "swaps"
        swaps.write_text(self.HEADER, encoding="utf-8")
        self.assertEqual(self.check(swaps), (0, "checked\n", ""))

    def test_a_device_line_refuses(self):
        swaps = self.root / "swaps"
        swaps.write_text(self.HEADER + "/mnt/swapfile                           file\t\t4194300\t\t0\t\t-2\n",
                         encoding="utf-8")
        status, stdout, stderr = self.check(swaps)
        self.assertEqual((status, stdout), (1, ""))
        self.assertIn(f"::error::release-steps: {swaps} does not hold its header line alone (Filename Type Size Used"
                      " Priority /mnt/swapfile file 4194300 0 -2), so swap is not shown to be off; the signing key is"
                      " imported only while it lists no device, which preflight sets with `sudo swapoff -a`", stderr)

    def test_an_empty_or_unreadable_file_refuses(self):
        empty = self.root / "empty"
        empty.write_text("", encoding="utf-8")
        status, stdout, stderr = self.check(empty)
        self.assertEqual((status, stdout), (1, ""))
        self.assertIn(f"::error::release-steps: {empty} does not hold its header line alone (), so swap is not shown"
                      " to be off", stderr)
        for swaps in (self.root / "absent", self.root):
            with self.subTest(swaps=swaps.name):
                status, stdout, stderr = self.check(swaps)
                self.assertEqual((status, stdout), (1, ""))
                self.assertIn(f"::error::release-steps: {swaps} could not be read (", stderr)
                self.assertIn("so swap is not shown to be off; the signing key is not imported", stderr)


class FloorPython(unittest.TestCase):
    """The interpreter check test-floor runs inside the container, against a python3 that reports a version, and
    test-floor's refusal of a directory it would not mount; the container run itself is proven by a workflow run."""

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.root)

    def check(self, python3=None):
        functions = shell_functions("_release_steps__require_floor_python", "_release_steps__die",
                                    "_release_steps__format_one_line")
        environment = {"PATH": str(self.root)}
        if python3 is not None:
            environment["BASH_FUNC_python3%%"] = python3
        script = (f'set -euo pipefail\n_RELEASE_STEPS__DIE_PREFIX=""\n_RELEASE_STEPS__DIE_SUFFIX=""\n{functions}'
                  '_release_steps__require_floor_python\necho checked\n')
        completed = subprocess.run([BASH, "-c", script], env=environment, capture_output=True, text=True)
        return completed.returncode, completed.stdout, completed.stderr

    def test_python_3_9_passes(self):
        self.assertEqual(self.check('() { echo "Python 3.9.21"; }'), (0, "Python 3.9.21\nchecked\n", ""))

    def test_a_later_python_refuses(self):
        status, stdout, stderr = self.check('() { echo "Python 3.14.4"; }')
        self.assertEqual((status, stdout), (1, "Python 3.14.4\n"))
        self.assertIn("::error::release-steps: python3 is Python 3.14.4, not Python 3.9; the floor suite runs only on"
                      " the floor's interpreter", stderr)

    def test_a_python3_that_fails_or_is_absent_refuses(self):
        for name, python3 in (("fails", '() { echo "no interpreter" >&2; return 1; }'), ("absent", None)):
            with self.subTest(python3=name):
                status, stdout, stderr = self.check(python3)
                self.assertEqual((status, stdout), (1, ""))
                self.assertIn("::error::release-steps: `python3 --version` failed (", stderr)

    def test_a_directory_outside_the_checkout_refuses_before_podman(self):
        (self.root / "tests").mkdir()
        for directory in ("../tests", "/tmp", "tests/../..", "absent", ""):
            with self.subTest(directory=directory):
                completed = subprocess.run([BASH, str(RELEASE_STEPS), "test-floor", directory], cwd=self.root,
                                           capture_output=True, text=True)
                self.assertEqual(completed.returncode, 1)
                self.assertIn(f"::error::release-steps: {directory} is not a directory under {self.root}, named by"
                              " a relative path", completed.stderr)
                self.assertNotIn("podman", completed.stdout + completed.stderr)


class ReleaseHeadlines(unittest.TestCase):
    """The changelog parser the release mode's body is built from, run on text alone, so its cases need no signed tag."""

    def headlines(self, changelog: str, version="1.3.0") -> str:
        script = shell_functions("_release_steps__list_release_headlines", "_release_steps__format_headline")
        completed = subprocess.run(
            [BASH, "-c", f'set -euo pipefail\n{script}\n_release_steps__list_release_headlines "$1" "$2"', "_",
             changelog, version], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        return completed.stdout

    def test_adjacent_bold_entries_each_keep_their_label(self):
        self.assertEqual(self.headlines(RELEASE_CHANGELOG),
                         "Image check\nDigest refresh\nA release pulls every image before the first signature\n")

    def test_adjacent_plain_entries_each_give_their_first_sentence(self):
        self.assertEqual(self.headlines("## [1.3.0]\n- First plain sentence. A second one.\n- Second plain sentence.\n"),
                         "First plain sentence\nSecond plain sentence\n")

    def test_a_wrapped_entry_is_one_headline(self):
        self.assertEqual(self.headlines("## [1.3.0]\n\n- A sentence that wraps\n  onto a second line. More.\n"
                                        "- **Bold label.** Then\n  more.\n"),
                         "A sentence that wraps onto a second line\nBold label\n")

    def test_the_next_section_is_not_read(self):
        self.assertEqual(self.headlines("## [1.3.0]\n- **Ours.** Here.\n## [1.2.0]\n- **Theirs.** There.\n"), "Ours\n")

    def test_an_absent_section_gives_no_headline(self):
        self.assertEqual(self.headlines(CHANGELOG), "")


@unittest.skipUnless(gpg_runs(), "signing a tag needs gpg and a tmpfs /dev/shm")
class RepinToolsToARelease(RepinCase):
    """repin-tools to a tag signed by a key generated for the test, served by curl standing in for the key URL."""

    stubbed = ("skopeo", "gh", "sleep", "curl")

    def setUp(self):
        super().setUp()
        # The release fixture keeps the Fedora pin the tools shipped with, so the registry answers for it too.
        self.answer_pinned("fc44")
        self.gnupg = self.root / "gnupg"
        self.gnupg.mkdir(mode=0o700)
        self.gpg("--quick-generate-key", "--passphrase", "", "Test Maintainer <maintainer@example.org>", "ed25519",
                 "sign", "never")
        self.primary = next(line.split(":")[9] for line in self.gpg("--with-colons", "--list-keys").splitlines()
                            if line.startswith("fpr:"))
        self.answer("curl", [], stdout=self.gpg("--armor", "--export", self.primary))

    def tearDown(self):
        # The generated keyring's gpg-agent is stopped before its home is removed under it.
        subprocess.run(["gpgconf", "--kill", "gpg-agent"], env=dict(os.environ, GNUPGHOME=str(self.gnupg)),
                       capture_output=True)
        super().tearDown()

    def gpg(self, *arguments: str) -> str:
        return subprocess.run(["gpg", "--homedir", str(self.gnupg), "--batch", "--pinentry-mode", "loopback",
                               *arguments], capture_output=True, text=True, check=True).stdout

    def release(self, signers: str, changelog=RELEASE_CHANGELOG) -> str:
        """The 1.3.0 release commit on the tools' develop, its release.yml pinning <signers>, tagged and signed."""
        release_yml = (REPOSITORY / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        release_yml = re.sub(r"TAG_SIGNER_PRIMARY_FINGERPRINTS: \S+", f"TAG_SIGNER_PRIMARY_FINGERPRINTS: {signers}",
                             release_yml)
        sha = self.commit_files(self.tools, {"CHANGELOG.md": changelog,
                                             ".github/workflows/release.yml": release_yml}, "chore: release 1.3.0")
        environment = dict(self.environment(stubbed=()), GNUPGHOME=str(self.gnupg))
        subprocess.run(["git", "-C", str(self.tools), "-c", f"user.signingkey={self.primary}", "tag", "-s", "-m",
                        "1.3.0", "v1.3.0"], env=environment, check=True, capture_output=True)
        self.git(self.tools, "push", "--quiet", "origin", "develop", "v1.3.0")
        return sha

    def test_a_release_is_pinned_with_its_changelog_headlines(self):
        sha = self.release(self.primary)
        self.answer("gh", ["release", "view", "v1.3.0"], stdout="false false\n")
        self.answer("gh", ["api"], stdout="success https://github.com/dag-node/ai-tools-assets-tools/actions/runs/7\n")
        self.answer_caller_pull_request()
        status, stdout, stderr = self.repin("v1.3.0")
        self.assertEqual(status, 0, stderr)
        self.assertIn(f"set-release.yml@{sha} # v1.3.0\n",
                      self.caller_file(f"ci/{REFRESH_ID}-tools-1.3.0", "release.yml"))
        message = self.git(self.caller, "log", "-1", "--format=%B", f"ci/{REFRESH_ID}-tools-1.3.0")
        self.assertTrue(message.startswith("ci: call the ai-tools-assets-tools 1.3.0 workflows\n\n"
                                           "The release and validate workflows previously called the v1.2.0 commit.\n"
                                           f"They now call the 1.3.0 commit `{sha[:7]}` (release `v1.3.0`):\n\n"
                                           "- Image check\n- Digest refresh\n"
                                           "- A release pulls every image before the first signature\n"), message)
        self.assertIn(f"signed by {self.primary}", stdout)

    def test_a_caller_that_signs_its_commits_signs_the_re_pin_from_its_own_keyring(self):
        """The tag check's scratch keyring holds public keys alone; the commit after it reaches the caller's."""
        self.release(self.primary)
        self.answer("gh", ["release", "view", "v1.3.0"], stdout="false false\n")
        self.answer("gh", ["api"], stdout="success https://github.com/dag-node/ai-tools-assets-tools/actions/runs/7\n")
        self.answer_caller_pull_request()
        self.git(self.caller, "config", "commit.gpgsign", "true")
        self.git(self.caller, "config", "user.signingkey", self.primary)
        with mock.patch.dict(os.environ, {"GNUPGHOME": str(self.gnupg)}):
            status, _, stderr = self.repin("v1.3.0")
            self.assertEqual(status, 0, stderr)
            signer = self.git(self.caller, "log", "-1", "--format=%GP", f"ci/{REFRESH_ID}-tools-1.3.0")
        self.assertEqual(signer, self.primary)

    def test_a_tag_signed_by_another_key_is_refused(self):
        self.release("0" * 40)
        status, _, stderr = self.repin("v1.3.0")
        self.assertEqual(status, 1)
        self.assertIn(f"v1.3.0 is signed by primary {self.primary}, which is not on the pinned list: {'0' * 40}",
                      stderr)

    def test_a_release_run_that_failed_is_refused(self):
        self.release(self.primary)
        self.answer("gh", ["release", "view", "v1.3.0"], stdout="false false\n")
        self.answer("gh", ["api"], stdout="failure https://github.com/dag-node/ai-tools-assets-tools/actions/runs/7\n")
        status, _, stderr = self.repin("v1.3.0")
        self.assertEqual(status, 1)
        self.assertIn("the release run for v1.3.0 concluded failure"
                      " (https://github.com/dag-node/ai-tools-assets-tools/actions/runs/7); fix or re-run it before"
                      " re-pinning", stderr)

    def test_a_changelog_that_names_another_version_is_refused(self):
        self.release(self.primary, RELEASE_CHANGELOG.replace("## [1.3.0]", "## [1.3.1]"))
        status, _, stderr = self.repin("v1.3.0")
        self.assertEqual(status, 1)
        self.assertIn("the tools' `CHANGELOG.md` at v1.3.0 opens its newest release with `## [1.3.1]`, not"
                      " `## [1.3.0]`; the tag and the changelog disagree", stderr)

    def test_a_release_section_without_entries_is_refused(self):
        self.release(self.primary, CHANGELOG.replace("## [Unreleased]\n", "## [Unreleased]\n\n## [1.3.0] - 2026-11-20\n"))
        status, _, stderr = self.repin("v1.3.0")
        self.assertEqual(status, 1)
        self.assertIn("the `## [1.3.0]` section of `CHANGELOG.md` has no entries; the body needs at least one", stderr)

    def test_a_tag_without_a_release_is_refused(self):
        self.release(self.primary)
        self.answer("gh", ["release", "view", "v1.3.0"], stderr="release not found\n", status=1)
        status, _, stderr = self.repin("v1.3.0")
        self.assertEqual(status, 1)
        self.assertIn("v1.3.0 has no GitHub release; a caller pins a final release", stderr)


if __name__ == "__main__":
    unittest.main()
