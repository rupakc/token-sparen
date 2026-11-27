"""Trust exactly one signing key (the owner's) for git signature checks in a release job.

The release workflow (``.github/workflows/release.yml``) verifies that the pushed tag, and the
commit that last changed ``docs/release/gate-decision.toml`` (`release_check`), are signed by the
owner. GitHub's own "verified" badge accepts any key registered to the tagger's account, so the
job additionally trusts ONLY the public key the owner stored in the repository variable
``RELEASE_SIGNING_KEY`` (a public key: no secret needed), bound to the tagger email in
``RELEASE_TAGGER``.

* ``trust``: read the key from an environment variable (never argv). An SSH public key line
  (``ssh-ed25519 AAAA... [comment]``) becomes the only entry of an allowed-signers file and git's
  ``gpg.ssh.allowedSignersFile``; an ASCII-armoured OpenPGP public key (exactly one primary key)
  whose user ids include the tagger email is imported into a keyring that must be empty and
  given ultimate owner trust, with ``gpg.minTrustLevel = fully``. Empty or malformed input
  fails closed.
* ``verify-tag TAG``: ``git verify-tag`` must succeed AND name the trusted signer (SSH: a good
  signature *for* the tagger email; OpenPGP: a good, ultimately trusted signature).

Git config is written with ``--global`` so the checkout's work tree stays clean (tag mode of
`release_check` requires a clean tree).

Usage: ``python scripts/release_trust.py trust --email E --key-env RELEASE_SIGNING_KEY
--signers PATH`` then ``python scripts/release_trust.py verify-tag vX.Y.Z --email E``.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

Runner = Callable[..., subprocess.CompletedProcess[str]]

SSH_KEY = re.compile(
    r"(?P<type>ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)"
    r"|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com)"
    r" (?P<blob>[A-Za-z0-9+/]+={0,3})(?: .*)?"
)
EMAIL = re.compile(r"[^\s@\"<>]+@[^\s@\"<>]+")
PGP_BEGIN = "-----BEGIN PGP PUBLIC KEY BLOCK-----"


class TrustError(Exception):
    """The key or email cannot be trusted as given (fail closed)."""


def _run(argv: Sequence[str], *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        list(argv), input=stdin, capture_output=True, text=True, encoding="utf-8", check=False
    )


def _email(email: str) -> str:
    email = email.strip()
    if not EMAIL.fullmatch(email):
        raise TrustError("RELEASE_TAGGER is empty or not an email address")
    return email


def allowed_signers_line(email: str, key: str) -> str:
    """The single allowed-signers entry binding ``key`` to ``email`` (namespace ``git``)."""
    lines = [line for line in key.strip().splitlines() if line.strip()]
    if len(lines) != 1:
        raise TrustError("the SSH signing key must be exactly one public key line")
    m = SSH_KEY.fullmatch(lines[0].strip())
    if m is None:
        raise TrustError("the signing key is not an SSH public key line")
    return f'{_email(email)} namespaces="git" {m.group("type")} {m.group("blob")}\n'


def _git_config(run: Runner, key: str, value: str) -> None:
    proc = run(["git", "config", "--global", key, value])
    if proc.returncode != 0:
        raise TrustError(f"git config {key} failed: {proc.stderr.strip()}")


def _gpg_fingerprints(colons: str) -> list[str]:
    """Primary-key fingerprints of a ``--with-colons`` listing (the ``fpr`` after each ``pub``)."""
    out: list[str] = []
    want = False
    for line in colons.splitlines():
        fields = line.split(":")
        if fields[0] == "pub":
            want = True
        elif fields[0] == "fpr" and want:
            out.append(fields[9])
            want = False
        elif fields[0] == "sub":
            want = False
    return out


def _gpg_uid_emails(colons: str) -> set[str]:
    """Lower-cased emails of the ``uid`` records of a ``--with-colons`` listing."""
    out: set[str] = set()
    for line in colons.splitlines():
        fields = line.split(":")
        if fields[0] == "uid" and len(fields) > 9:
            uid = fields[9].strip()
            bracketed = re.search(r"<([^<>]+)>$", uid)
            address = bracketed.group(1) if bracketed else uid
            if EMAIL.fullmatch(address):
                out.add(address.lower())
    return out


def _trust_pgp(email: str, key: str, run: Runner) -> str:
    email = _email(email)
    listed = run(["gpg", "--batch", "--with-colons", "--list-keys"])
    if _gpg_fingerprints(listed.stdout):
        raise TrustError("the gpg keyring is not empty: refusing to trust more than one key")
    shown = run(
        ["gpg", "--batch", "--with-colons", "--import-options", "show-only", "--import"],
        stdin=key,
    )
    fprs = _gpg_fingerprints(shown.stdout)
    if shown.returncode != 0 or len(fprs) != 1:
        raise TrustError("the OpenPGP signing key must hold exactly one primary public key")
    if email.lower() not in _gpg_uid_emails(shown.stdout):
        raise TrustError("the OpenPGP signing key has no user id with the RELEASE_TAGGER email")
    imported = run(["gpg", "--batch", "--import"], stdin=key)
    if imported.returncode != 0:
        raise TrustError(f"gpg --import failed: {imported.stderr.strip()}")
    owner = run(["gpg", "--batch", "--import-ownertrust"], stdin=f"{fprs[0]}:6:\n")
    if owner.returncode != 0:
        raise TrustError(f"gpg --import-ownertrust failed: {owner.stderr.strip()}")
    _git_config(run, "gpg.minTrustLevel", "fully")
    return fprs[0]


def trust(email: str, key: str, signers: Path, *, run: Runner = _run) -> str:
    """Trust ``key`` (SSH or OpenPGP public key) for ``email`` only; returns what was trusted
    (the allowed-signers entry's key type, or the OpenPGP fingerprint)."""
    if not key.strip():
        raise TrustError("RELEASE_SIGNING_KEY is empty: no owner key to trust")
    if key.strip().startswith(PGP_BEGIN):
        return _trust_pgp(email, key, run)
    line = allowed_signers_line(email, key)
    signers.parent.mkdir(parents=True, exist_ok=True)
    signers.write_text(line, encoding="utf-8", newline="\n")
    _git_config(run, "gpg.ssh.allowedSignersFile", str(signers))
    return line.split(" ")[2]


def verify_tag(tag: str, email: str, *, run: Runner = _run) -> str | None:
    """None when ``tag`` carries a good signature by the trusted owner key, else the reason."""
    email = _email(email)
    proc = run(["git", "verify-tag", "--raw", tag])
    output = f"{proc.stdout}\n{proc.stderr}"
    if proc.returncode != 0:
        return f"git verify-tag {tag} failed: {output.strip()}"
    if f'Good "git" signature for {email} with ' in output:
        return None  # SSH: the allowed-signers principal (the tagger email) matched
    if "[GNUPG:] GOODSIG " in output and "[GNUPG:] TRUST_ULTIMATE" in output:
        return None  # OpenPGP: the only key in the keyring, ultimately trusted
    return f"tag {tag} is not signed by the trusted owner key"


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="release_trust", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_trust = sub.add_parser("trust", help="trust the owner's public key only")
    p_trust.add_argument("--email", required=True)
    p_trust.add_argument("--key-env", required=True, help="env var holding the public key")
    p_trust.add_argument("--signers", required=True, type=Path, help="allowed-signers file")
    p_verify = sub.add_parser("verify-tag", help="the tag is signed by the trusted key")
    p_verify.add_argument("tag")
    p_verify.add_argument("--email", required=True)
    args = parser.parse_args(argv)
    environ = os.environ if env is None else env
    try:
        if args.command == "trust":
            what = trust(args.email, environ.get(args.key_env, ""), args.signers)
            print(f"ok trusting one owner key ({what})")
            return 0
        problem = verify_tag(args.tag, args.email)
    except TrustError as exc:
        print(f"release_trust: {exc}", file=sys.stderr)
        return 1
    if problem is not None:
        print(f"release_trust: {problem}", file=sys.stderr)
        return 1
    print(f"ok {args.tag} is signed by the owner key")
    return 0


if __name__ == "__main__":
    sys.exit(main())
