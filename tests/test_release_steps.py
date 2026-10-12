# SPDX-License-Identifier: MIT
"""Drives the subcommands of packaging/release-steps.sh that a checkout runs: check-images.

The registry is `command_stub.py` standing in for skopeo, exported to the script as a bash function, so a test decides
which manifest each reference returns. Each test runs a copy of the release steps whose image pins name digests
of the stub's manifests, and asserts before its first run that the stub, not an installed skopeo, answers the call.
The release itself (the pull in preflight, the hardened verify runs) runs on the GitHub runner alone and is proven by
a release run.
"""
from __future__ import annotations

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
        self.log = self.root / "stub.log"
        self.log.write_text("", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.root)

    def answer(self, command: str, arguments: list, stdout="", stderr="", status=0, times=None):
        self.answers.append({"command": command, "arguments": arguments, "stdout": stdout, "stderr": stderr,
                             "status": status, "times": times})

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
        (self.root / "answers.json").write_text(json.dumps(self.answers), encoding="utf-8")
        environment = self.environment(path, stubbed)
        for command in self.stubbed if stubbed is None else stubbed:
            kind = subprocess.run([BASH, "-c", f"type -t {command}"], env=environment, capture_output=True, text=True)
            self.assertEqual(kind.stdout.strip(), "function", f"{command} does not resolve to the stub")
        completed = subprocess.run([BASH, str(script), *arguments], cwd=cwd, env=environment, capture_output=True,
                                   text=True)
        return completed.returncode, completed.stdout, completed.stderr

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


if __name__ == "__main__":
    unittest.main()
