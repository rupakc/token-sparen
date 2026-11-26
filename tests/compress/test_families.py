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


# ------------------------------------------------------------- gating: any segment (fix round 1)


@pytest.mark.parametrize(
    "cmd",
    [
        "wc -l app.log && tail -n 3000 app.log",
        "ls -la logs && cat logs/app.log",
        "pwd && cat app.log",
        "git status; cat app.log",
        "git status\ncat app.log",
        "make || cat build.log",
        'for f in *.log; do cat "$f"; done',
        'while read f; do cat "$f"; done < list.txt',
        "if [ -f a.log ]; then tail -n 500 a.log; fi",
        "find . -name '*.log' -exec cat {} \\;",
        "find . -name '*.log' -exec tail -n 100 {} +",
        "find . -type f -execdir head -50 {} \\;",
        "sudo -iu app cat /srv/app.log",
        "sudo -u app -i cat /srv/app.log",
        "sort data.csv",
        "uniq -c counts.txt",
        "cut -d, -f1,3 data.csv",
        "cut -d ' ' -f 2 data.txt",
        "rev notes.txt",
        "strings -n 8 binary.bin",
        "tac app.log",
        "python3 -c \"print(open('app.log').read())\"",
        "python -c 'import pathlib; print(pathlib.Path(\"a.json\").read_text())'",
        "node -e \"console.log(require('fs').readFileSync('a.json', 'utf8'))\"",
        "bash -c 'pwd && cat app.log'",
        "ls | xargs cat",
        "cat < app.log",
        "sed -n '1,400p' big.py",
        "sed -e 's/a/b/' big.py",
        "awk '{print $1}' data.txt",
        "jq . package.json",
        "head -50 big.py",
        "Get-Content app.log",
        "gc app.log -Tail 3000",
        "type app.log",
        "cd x && echo start && GET-CONTENT -Path app.log",
    ],
)
def test_gating_file_dump_in_any_segment(cmd: str) -> None:
    assert families.is_file_dump(cmd) is True


@pytest.mark.parametrize(
    "cmd",
    [
        "pytest -q && git diff",
        "git log | sort -k2",
        "cat <<EOF | wc -l",  # a here-document is not a file
        "pytest -q | tail -n 50",
        "git log | head -100",
        "curl -s https://example.org | jq .",
        "kubectl logs pod | sed -n '1,100p'",
        "ps aux | sort -k3 | head",
        "git diff | cut -c1-80",
        "python3 -c 'print(1 + 1)'",
        "node -e 'console.log(42)'",
        "for i in 1 2 3; do echo $i; done",
        "find . -name '*.py' -exec wc -l {} +",
        "npm test || echo failed",
        "",
        "'unbalanced",
    ],
)
def test_gating_not_a_file_dump(cmd: str) -> None:
    assert families.is_file_dump(cmd) is False


@pytest.mark.parametrize(
    ("cmd", "family"),
    [
        ("wc -l app.log && tail -n 3000 app.log", "listing"),  # replay keeps the first program
        ("pwd && cat app.log", "other"),
        ("sort data.csv", "other"),
        ("sudo -iu app cat /srv/app.log", "file_dump"),  # combined short options fixed
    ],
)
def test_replay_family_stays_first_program(cmd: str, family: str) -> None:
    assert families.command_family(cmd) == family
