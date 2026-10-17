# SPDX-License-Identifier: MIT
"""The one signature check `sign-set` and `verify-set` make: `gpgv` over a detached signature, against one key.

`gpgv` runs with `--homedir` set to a scratch directory the caller owns and removes, so the keyring it reads is the key
the caller passed and no keyring of the user's own GnuPG home, and with `--status-fd 1`. The machine-readable status
lines and the exit status decide the outcome; the human-readable output does not. A set is signed once, so a status
stream reporting more than one signature (a second `NEWSIG`, `VALIDSIG` or result line) is UNABLE, and every other line
is read as the one signature's. `BADSIG` is a MISMATCH, and so are `EXPSIG`, `EXPKEYSIG`, `REVKEYSIG` and
`KEYREVOKED`, since `gpgv` exits 0 for a good signature by an expired or a revoked key; `KEYEXPIRED` alone decides
nothing, as `gpgv` prints it for an expired subkey the signature does not use. `NO_PUBKEY`, `ERRSIG` and a non-zero exit
are UNABLE. VERIFIED needs a zero exit, `GOODSIG`, and a `VALIDSIG` with its nine fields: the first names the signing
key, and the tenth, where present, the primary, which is the fingerprint a host's binding pins. ai-tools-base's verifier
takes the same policy. An armored key is dearmored here, by its base64 body, so verifying needs `gpgv` alone.

`scratch_directory` makes the scratch directory the commands write the key, the inventory and its signature into:
a `tempfile.TemporaryDirectory` removed on exit, on an exception and on SIGTERM or SIGHUP, whose default action would end
the interpreter without removing it. The two signals are blocked while the handlers are installed and the directory is
created, and again while it is removed, so a signal that arrives in either window waits instead of leaving
the directory behind.
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
# The status lines gpgv prints once per signature, each ending that signature's check.
SIGNATURE_RESULTS = ("GOODSIG", "BADSIG", "EXPSIG", "EXPKEYSIG", "REVKEYSIG", "ERRSIG")
KEY_REFUSALS = ("EXPKEYSIG", "REVKEYSIG", "KEYREVOKED")
VALIDSIG_FIELDS = 9


@contextlib.contextmanager
def scratch_directory(prefix: str) -> Iterator[pathlib.Path]:
    """A new private directory under the user's temp directory, removed when the context ends.

    For the context's extent SIGTERM and SIGHUP raise SystemExit(128 + the signal number), so the interpreter unwinds
    through the removal, and `subprocess.run` kills a child it is waiting on first; the previous handlers and signal
    mask are restored after. A signal that arrives before the directory is created is held until the context is
    entered and then raises there; one that arrives during the removal is held until the previous handler is back,
    which receives it. Call it from the main thread, the only one `signal.signal` accepts.
    """

    def terminate(signal_number, frame):
        raise SystemExit(128 + signal_number)

    mask = signal.pthread_sigmask(signal.SIG_BLOCK, TERMINATION_SIGNALS)
    try:
        previous = {signal_number: signal.signal(signal_number, terminate) for signal_number in TERMINATION_SIGNALS}
        try:
            with tempfile.TemporaryDirectory(prefix=prefix) as scratch:
                signal.pthread_sigmask(signal.SIG_SETMASK, mask)
                try:
                    yield pathlib.Path(scratch)
                finally:
                    signal.pthread_sigmask(signal.SIG_BLOCK, TERMINATION_SIGNALS)
        finally:
            for signal_number, handler in previous.items():
                signal.signal(signal_number, signal.SIG_DFL if handler is None else handler)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, mask)


@dataclass(frozen=True)
class SignatureCheck:
    """What gpgv found: `outcome` is VERIFIED, MISMATCH (a bad or expired signature, or one by an expired or revoked
    key) or UNABLE (gpgv did not run or did not complete the check, the key does not hold the signer, or the file holds
    more than one signature), the split ai-tools-base's verifier makes; the keys are set when VERIFIED, `error`
    otherwise."""

    outcome: int
    signing_key: Optional[str] = None
    primary: Optional[str] = None
    error: Optional[str] = None


def dearmor(text: bytes) -> Optional[bytes]:
    """The binary packets of every armored public key block in `text`, or None when it does not hold a well-formed
    block."""
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
    try:
        completed = subprocess.run(["gpgv", "--homedir", str(scratch), "--status-fd", "1", "--keyring", str(keyring),
                                    str(signature_path), str(data_path)], capture_output=True, text=True)
    except OSError as error:
        return SignatureCheck(UNABLE, error=f"`gpgv` could not be run: {error.strerror or error}")
    records = [fields[1:] for fields in (line.split() for line in completed.stdout.splitlines())
               if len(fields) >= 2 and fields[0] == "[GNUPG:]"]
    keywords = [record[0] for record in records]
    if (keywords.count("NEWSIG") > 1 or keywords.count("VALIDSIG") > 1
            or sum(keywords.count(keyword) for keyword in SIGNATURE_RESULTS) > 1):
        return SignatureCheck(UNABLE, error="the signature file holds more than one signature; a set is signed once")
    status = {record[0]: record[1:] for record in records}
    reason = completed.stderr.strip().splitlines()[-1:] or ["no status"]
    if "BADSIG" in status:
        return SignatureCheck(MISMATCH, error="the signature does not match SHA256SUMS")
    if "EXPSIG" in status:
        return SignatureCheck(MISMATCH, error="the signature is expired")
    if any(keyword in status for keyword in KEY_REFUSALS):
        return SignatureCheck(MISMATCH, error="the signing key is expired or revoked")
    if "NO_PUBKEY" in status:
        return SignatureCheck(UNABLE, error=f"the signature is by {status['NO_PUBKEY'][0] if status['NO_PUBKEY'] else 'a key'}, which the given key does not hold")
    if "ERRSIG" in status or completed.returncode != 0:
        return SignatureCheck(UNABLE, error=f"gpgv could not check the signature (exit {completed.returncode}: {reason[0]})")
    validsig = status.get("VALIDSIG")
    if "GOODSIG" in status and validsig is not None and len(validsig) >= VALIDSIG_FIELDS:
        signing_key = validsig[0].upper()
        primary = validsig[VALIDSIG_FIELDS].upper() if len(validsig) > VALIDSIG_FIELDS else signing_key
        if FINGERPRINT_PATTERN.match(signing_key) and FINGERPRINT_PATTERN.match(primary):
            return SignatureCheck(VERIFIED, signing_key=signing_key, primary=primary)
    return SignatureCheck(UNABLE, error=f"gpgv did not report a complete good signature ({reason[0]})")
