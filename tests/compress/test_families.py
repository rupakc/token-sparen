"""Shared command-family classifier (production gating and E1 replay)."""

from __future__ import annotations

import pytest

from token_sparen.compress import families


@pytest.mark.parametrize(
    ("cmd", "family"),
    [
        # leading setup segments are skipped
        ("source .venv/bin/activate && pytest -q", "test"),
        (". ./env.sh && pytest -q", "test"),
        ("export A=b; pytest", "test"),
        ("export A=b B=c && cargo test", "test"),
        ("echo start && pytest -x", "test"),
        ("printf 'x\\n' ; make", "build"),
        ("echo hi | jq .", "other"),  # a pipe from echo: jq reads stdin, not a file
        ("echo hi", "other"),
        # wrappers with arguments
        ("timeout 60 pytest", "test"),
        ("timeout -s KILL 5m go test ./...", "test"),
        ("sudo -u app cat /etc/app.conf", "file_dump"),
        ("sudo -u app -g grp ls -la", "listing"),
        ("nice -n 10 make", "build"),
        ("env -u FOO pytest", "test"),
        # shells: the inner script is classified
        ("bash -c 'cat big.py'", "file_dump"),
        ('sh -c "pytest -q"', "test"),
        ("bash -lc 'cd x && npm test'", "test"),
        ("zsh -c 'git diff'", "vcs_read"),
        ("bash script.sh", "other"),
        # interpreters and JS runners
        ("python3.12 -m pytest -q", "test"),
        ("python3.11 -m mypy pkg", "build"),
        ("/usr/bin/python3 -m pytest", "test"),
        ("yarn jest --ci", "test"),
        ("pnpm exec vitest run", "test"),
        ("pnpm exec tsc --noEmit", "build"),
        ("npm exec -- jest", "test"),
        ("yarn eslint src", "build"),
        ("bun x jest", "test"),
        # file dumps
        ("find . -name '*.py' | xargs cat", "file_dump"),
        ("git ls-files | xargs -n 1 cat", "file_dump"),
        ("rg -l foo | xargs head -n 40", "file_dump"),
        ("find . | xargs wc -l", "listing"),
        ("git show HEAD~1:src/app.py", "file_dump"),
        ("git show abc123:README.md | head -50", "file_dump"),
        ("git cat-file -p HEAD:src/x.py", "file_dump"),
        ("git cat-file blob abc123", "file_dump"),
        ("git show HEAD", "vcs_read"),
        ("git show --stat HEAD", "vcs_read"),
        ("git cat-file -t HEAD", "other"),
        ("od -c data.bin", "file_dump"),
        ("xxd file.bin", "file_dump"),
        ("nl -ba x.py", "file_dump"),
        ("awk '{print $1}' data.txt", "file_dump"),
        ("jq . package.json", "file_dump"),
        ("less x.log", "file_dump"),
        ("bat src/main.rs", "file_dump"),
        ("cd sub && cat a.txt", "file_dump"),
    ],
)
def test_command_family(cmd: str, family: str) -> None:
    assert families.command_family(cmd) == family


@pytest.mark.parametrize(
    ("cmd", "dump"),
    [
        ("cat big.py", True),
        ("head -200 x.py", True),
        ("sed -n '1,400p' x.py", True),
        ("git show main:a.py", True),
        ("git diff", False),
        ("pytest -q", False),
        ("ls -R", False),
        ("curl -s https://example.org/api", False),
        ("", False),
    ],
)
def test_is_file_dump(cmd: str, dump: bool) -> None:
    assert families.is_file_dump(cmd) is dump


def test_replay_reexports_the_shared_classifier() -> None:
    from token_sparen.replay import replay

    assert replay.command_family is families.command_family
