# SPDX-License-Identifier: MIT
"""The one signature check `sign-set` and `verify-set` make: `gpgv` over a detached signature, against one key.

`gpgv` runs with `--homedir` set to a scratch directory the caller owns and removes, so the keyring it reads is the
key the caller passed and nothing in the user's own GnuPG home, and with `--status-fd 1`, whose machine-readable lines
decide the outcome; its exit status and its human-readable output do not. A good signature is a `VALIDSIG` line: its
fields name the signing key and, where it differs, the primary, which is the fingerprint a host's binding pins. An
armored key is dearmored here, by its base64 body, so verifying needs `gpgv` alone. `scratch_directory` is the one
scratch directory the commands write the key, the inventory and its signature into: a `tempfile.TemporaryDirectory`
removed on exit, on an exception and on SIGTERM or SIGHUP, whose default action would end the interpreter without
removing it.
"""
from __future__ import annotations

import base64
import binascii
import contextlib
import pathlib
import re
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Iterator, List, Optional

FINGERPRINT_PATTERN = re.compile(r"^[0-9A-F]{40}$")
ARMOR_BEGIN = re.compile(r"^-----BEGIN PGP PUBLIC KEY BLOCK-----\s*$")
ARMOR_END = re.compile(r"^-----END PGP PUBLIC KEY BLOCK-----\s*$")
# The status contract of ai-tools-base's verifier, which verify-set exits with; the inventory half reports under it.
VERIFIED = 0
MISMATCH = 1
UNABLE = 2
TERMINATION_SIGNALS = (signal.SIGTERM, signal.SIGHUP)


@contextlib.contextmanager
def scratch_directory(prefix: str) -> Iterator[pathlib.Path]:
    """A new private directory under the user's temp directory, removed when the context ends.

    For the context's extent SIGTERM and SIGHUP raise SystemExit(128 + the signal number), so the interpreter unwinds
    through the removal, and `subprocess.run` kills a child it is waiting on first; the previous handlers are restored
    after. Call it from the main thread, the only one `signal.signal` accepts.
    """

    def terminate(signal_number, frame):
        raise SystemExit(128 + signal_number)

    previous = {signal_number: signal.signal(signal_number, terminate) for signal_number in TERMINATION_SIGNALS}
    try:
        with tempfile.TemporaryDirectory(prefix=prefix) as scratch:
            yield pathlib.Path(scratch)
    finally:
        for signal_number, handler in previous.items():
            signal.signal(signal_number, signal.SIG_DFL if handler is None else handler)


@dataclass(frozen=True)
class SignatureCheck:
    """What gpgv found: `outcome` is VERIFIED, MISMATCH (a signature gpgv rejects) or UNABLE (gpgv did not run, could
    not read the signature, or the key does not hold the signer), the split ai-tools-base's verifier makes; the keys are
    set when VERIFIED, `error` otherwise."""

    outcome: int
    signing_key: Optional[str] = None
    primary: Optional[str] = None
    error: Optional[str] = None


def dearmor(text: bytes) -> Optional[bytes]:
    """The binary packets of every armored public key block in `text`, or None when it holds no well-formed block."""
    blocks: List[bytes] = []
    body: Optional[List[str]] = None
    in_headers = False
    for line in text.decode("ascii", "replace").splitlines():
        if body is None:
            if ARMOR_BEGIN.match(line):
                body, in_headers = [], True
            continue
        if ARMOR_END.match(line):
            try:
                blocks.append(base64.b64decode("".join(body), validate=True))
            except (binascii.Error, ValueError):
                return None
            body = None
            continue
        if in_headers:
            if not line.strip():
                in_headers = False
            elif ":" not in line:
                in_headers = False
                body.append(line.strip())
            continue
        if line.startswith("="):
            continue  # the CRC-24 line; gpgv checks the packets themselves
        body.append(line.strip())
    if body is not None or not blocks:
        return None
    return b"".join(blocks)


def check_signature(scratch: pathlib.Path, key: bytes, data: bytes, signature: bytes) -> SignatureCheck:
    """Check `signature` over `data` against the binary key `key`, with gpgv's home in the empty directory `scratch`."""
    if shutil.which("gpgv") is None:
        return SignatureCheck(UNABLE, error="`gpgv` is not on PATH")
    keyring, data_path, signature_path = scratch / "key.gpg", scratch / "SHA256SUMS", scratch / "SHA256SUMS.asc"
    keyring.write_bytes(key)
    data_path.write_bytes(data)
    signature_path.write_bytes(signature)
    completed = subprocess.run(["gpgv", "--homedir", str(scratch), "--status-fd", "1", "--keyring", str(keyring),
                                str(signature_path), str(data_path)], capture_output=True, text=True)
    status = {}
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == "[GNUPG:]":
            status.setdefault(fields[1], fields[2:])
    if "BADSIG" in status:
        return SignatureCheck(MISMATCH, error="the signature does not match SHA256SUMS")
    if "EXPKEYSIG" in status or "REVKEYSIG" in status:
        return SignatureCheck(MISMATCH, error="the signing key is expired or revoked")
    if "NO_PUBKEY" in status:
        return SignatureCheck(UNABLE, error=f"the signature is by {status['NO_PUBKEY'][0] if status['NO_PUBKEY'] else 'a key'}, which the given key does not hold")
    validsig = status.get("VALIDSIG")
    if validsig and "GOODSIG" in status:
        signing_key = validsig[0].upper()
        primary = validsig[9].upper() if len(validsig) > 9 else signing_key
        if FINGERPRINT_PATTERN.match(signing_key) and FINGERPRINT_PATTERN.match(primary):
            return SignatureCheck(VERIFIED, signing_key=signing_key, primary=primary)
    reason = completed.stderr.strip().splitlines()[-1:] or ["no status"]
    return SignatureCheck(UNABLE, error=f"gpgv could not verify the signature ({reason[0]})")
