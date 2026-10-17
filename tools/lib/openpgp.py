# SPDX-License-Identifier: MIT
"""The one signature check `sign-set` and `verify-set` make: `gpgv` over a detached signature, against one key.

`gpgv` runs with `--homedir` set to a scratch directory the caller owns and removes, so the keyring it reads is the
key the caller passed and nothing in the user's own GnuPG home, and with `--status-fd 1`, whose machine-readable lines
decide the outcome; its exit status and its human-readable output do not. A good signature is a `VALIDSIG` line: its
fields name the signing key and, where it differs, the primary, which is the fingerprint a host's binding pins. An
armored key is dearmored here, by its base64 body, so verifying needs `gpgv` alone.
"""
from __future__ import annotations

import base64
import binascii
import pathlib
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import List, Optional

FINGERPRINT_PATTERN = re.compile(r"^[0-9A-F]{40}$")
ARMOR_BEGIN = re.compile(r"^-----BEGIN PGP PUBLIC KEY BLOCK-----\s*$")
ARMOR_END = re.compile(r"^-----END PGP PUBLIC KEY BLOCK-----\s*$")
VERIFIED = "verified"
MISMATCH = "mismatch"
UNABLE = "unable"


@dataclass(frozen=True)
class SignatureCheck:
    """What gpgv found: `outcome` is VERIFIED, MISMATCH (a bad signature, or one by another key) or UNABLE (gpgv did
    not run, or could not read the signature or the key); the keys are set when VERIFIED, `error` otherwise."""

    outcome: str
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


def valid_signature(scratch: pathlib.Path, key: bytes, data: bytes, signature: bytes) -> SignatureCheck:
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
        return SignatureCheck(MISMATCH, error=f"the signature is by {status['NO_PUBKEY'][0] if status['NO_PUBKEY'] else 'a key'}, which the given key does not hold")
    validsig = status.get("VALIDSIG")
    if validsig and "GOODSIG" in status:
        signing_key = validsig[0].upper()
        primary = validsig[9].upper() if len(validsig) > 9 else signing_key
        if FINGERPRINT_PATTERN.match(signing_key) and FINGERPRINT_PATTERN.match(primary):
            return SignatureCheck(VERIFIED, signing_key=signing_key, primary=primary)
    reason = completed.stderr.strip().splitlines()[-1:] or ["no status"]
    return SignatureCheck(UNABLE, error=f"gpgv could not verify the signature ({reason[0]})")
