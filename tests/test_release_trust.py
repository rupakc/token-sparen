"""`scripts/release_trust.py`: the release job trusts the owner's public key and nothing else."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts import release_trust as rt

EMAIL = "owner@example.com"
ED25519 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIB1bEBgXH7bGm9JXp3ySyVd6n6lYJb2D2p0W0e8u5X1a c"


def test_allowed_signers_line_binds_one_key_to_the_email() -> None:
    line = rt.allowed_signers_line(EMAIL, f"  {ED25519}\n")
    assert line == f'{EMAIL} namespaces="git" {" ".join(ED25519.split()[:2])}\n'


@pytest.mark.parametrize(
    ("email", "key"),
    [
        ("", ED25519),
        ("not-an-email", ED25519),
        (EMAIL, "ssh-dss AAAAB3 old"),
        (EMAIL, f"{ED25519}\n{ED25519}"),  # two keys: only one is ever trusted
        (EMAIL, "garbage"),
    ],
)
def test_bad_input_fails_closed(email: str, key: str) -> None:
    with pytest.raises(rt.TrustError):
        rt.allowed_signers_line(email, key)


def test_empty_key_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(rt.TrustError, match="empty"):
        rt.trust(EMAIL, "  \n", tmp_path / "signers")
    assert (
        rt.main(
            ["trust", "--email", EMAIL, "--key-env", "K", "--signers", str(tmp_path / "s")], env={}
        )
        == 1
    )
    assert not (tmp_path / "s").exists()


class FakeRun:
    """Records commands; answers gpg listings from ``listings``."""

    def __init__(self, listings: dict[str, str], rc: int = 0) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.listings = listings
        self.rc = rc

    def __call__(self, argv: list[str], *, stdin: str | None = None) -> Any:
        self.calls.append((argv, stdin))
        out = ""
        if "--list-keys" in argv:
            out = self.listings.get("keyring", "")
        elif "show-only" in argv:
            out = self.listings.get("key", "")
        return subprocess.CompletedProcess(argv, self.rc, out, "")


PGP = "-----BEGIN PGP PUBLIC KEY BLOCK-----\n\nmQENBF...\n-----END PGP PUBLIC KEY BLOCK-----\n"
FPR = "A" * 40
ONE_KEY = (
    f"pub:-:255:22:K:1::::::scESC:\nfpr:::::::::{FPR}:\n"
    f"sub:-:255:18:S:1:::::e:\nfpr:::::::::{'B' * 40}:\n"
)


def test_pgp_key_is_imported_with_ultimate_trust_into_an_empty_keyring(tmp_path: Path) -> None:
    run = FakeRun({"keyring": "", "key": ONE_KEY})
    assert rt.trust(EMAIL, PGP, tmp_path / "s", run=run) == FPR
    argvs = [argv for argv, _ in run.calls]
    assert ["gpg", "--batch", "--import"] in argvs
    assert (["gpg", "--batch", "--import-ownertrust"], f"{FPR}:6:\n") in run.calls
    assert ["git", "config", "--global", "gpg.minTrustLevel", "fully"] in argvs
    assert not (tmp_path / "s").exists()


def test_pgp_refuses_a_non_empty_keyring_or_several_keys(tmp_path: Path) -> None:
    with pytest.raises(rt.TrustError, match="not empty"):
        rt.trust(EMAIL, PGP, tmp_path / "s", run=FakeRun({"keyring": ONE_KEY, "key": ONE_KEY}))
    with pytest.raises(rt.TrustError, match="exactly one"):
        rt.trust(EMAIL, PGP, tmp_path / "s", run=FakeRun({"key": ONE_KEY + ONE_KEY}))
    with pytest.raises(rt.TrustError, match="exactly one"):
        rt.trust(EMAIL, PGP, tmp_path / "s", run=FakeRun({"key": ONE_KEY}, rc=2))


def test_verify_tag_needs_success_and_the_trusted_signer() -> None:
    def fake(rc: int, err: str) -> rt.Runner:
        return lambda argv, **_: subprocess.CompletedProcess(argv, rc, "", err)

    good_ssh = f'Good "git" signature for {EMAIL} with ED25519 key SHA256:x\n'
    assert rt.verify_tag("v1.0.0", EMAIL, run=fake(0, good_ssh)) is None
    gpg = "[GNUPG:] GOODSIG X o\n[GNUPG:] VALIDSIG F\n[GNUPG:] TRUST_ULTIMATE 0 pgp\n"
    assert rt.verify_tag("v1.0.0", EMAIL, run=fake(0, gpg)) is None
    other = 'Good "git" signature for someone@example.com with ED25519 key SHA256:x\n'
    assert rt.verify_tag("v1.0.0", EMAIL, run=fake(0, other)) is not None
    assert rt.verify_tag("v1.0.0", EMAIL, run=fake(0, "[GNUPG:] GOODSIG X o\n")) is not None
    assert rt.verify_tag("v1.0.0", EMAIL, run=fake(1, good_ssh)) is not None


@pytest.mark.skipif(
    shutil.which("ssh-keygen") is None or shutil.which("git") is None,
    reason="needs ssh-keygen and git",
)
def test_only_the_owner_ssh_key_verifies_a_real_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("owner", "other"):
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", tmp_path / name],
            check=True,
        )
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)

    git("init", "-q")
    git("config", "user.email", EMAIL)
    git("config", "user.name", "Owner")
    git("commit", "-q", "--allow-empty", "-m", "chore(release): v1.0.0")
    for tag, key in (("v1.0.0", "owner"), ("v1.0.1", "other")):
        signed = git(
            "-c", "gpg.format=ssh", "-c", f"user.signingkey={tmp_path / key}",
            "tag", "-s", "-m", tag, tag,
        )  # fmt: skip
        if signed.returncode != 0:
            pytest.skip(f"this git cannot sign with SSH keys: {signed.stderr.strip()}")
    git("tag", "-a", "-m", "unsigned", "v1.0.2")
    env = {"KEY": (tmp_path / "owner.pub").read_text(encoding="utf-8")}
    signers = tmp_path / "allowed_signers"
    argv = ["trust", "--email", EMAIL, "--key-env", "KEY", "--signers", str(signers)]
    assert rt.main(argv, env=env) == 0
    assert signers.read_text(encoding="utf-8").count("\n") == 1
    monkeypatch.chdir(repo)
    assert rt.verify_tag("v1.0.0", EMAIL) is None
    assert rt.verify_tag("v1.0.1", EMAIL) is not None  # signed, but not by the owner
    assert rt.verify_tag("v1.0.2", EMAIL) is not None  # unsigned
    assert rt.verify_tag("v1.0.0", "someone@example.com") is not None
    assert rt.main(["verify-tag", "v1.0.0", "--email", EMAIL]) == 0
    assert rt.main(["verify-tag", "v1.0.1", "--email", EMAIL]) == 1
    assert "allowedsignersfile" in git("config", "--global", "--list").stdout.lower()
    capsys.readouterr()
