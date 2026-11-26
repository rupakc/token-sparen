"""Gating-only file-content detection (fix round 2): substitutions, stdin redirects, wrappers,
more dump binaries and interpreters, file-reading tool names."""

from __future__ import annotations

import pytest

from token_sparen.compress import families, filedump


@pytest.mark.parametrize(
    "cmd",
    [
        # 1. substitutions and eval
        'echo "$(cat app.log)"',
        "echo $(cat app.log)",
        "echo `tail -n 900 app.log`",
        'printf "%s" "$(< app.log)"',
        "echo $(<app.log)",
        "eval cat app.log",
        "eval 'head -n 3000 app.log'",
        'x="$(echo hi && cat app.log)"; echo "$x"',
        # 2. stdin from a file
        "tee < app.log",
        "wc -c < app.log",
        "python3 script.py < app.log",
        "0< app.log tr a b",
        "tr a-z A-Z <app.log",
        'while read line; do echo "$line"; done < app.log',
        # 3. dd, cp to stdout
        "dd if=app.log bs=1M",
        "cp app.log /dev/stdout",
        "cp app.log /dev/stderr",
        "cp app.log -",
        # 4. wrappers
        "ssh host cat /var/log/app.log",
        "ssh -p 2222 -i key.pem user@host 'tail -n 2000 /var/log/app.log'",
        "su -c 'cat /etc/app.conf' app",
        "docker exec -it web cat /app/config.json",
        "docker exec -u root -w /app web tail -n 500 app.log",
        "docker run --rm -v $PWD:/w -w /w alpine cat data.json",
        "podman exec c1 cat /etc/hosts",
        "nerdctl run --rm img head -n 900 /data.txt",
        "docker compose exec api cat /app/settings.py",
        "kubectl exec pod-1 -c app -- cat /app/app.log",
        "kubectl exec -it pod-1 -- sh -c 'tail -n 1000 /app/app.log'",
        "adb shell cat /sdcard/log.txt",
        "script -q -c 'cat app.log' /dev/null",
        "watch -n 5 cat app.log",
        "unbuffer cat app.log",
        "ionice -c 3 cat app.log",
        "strace -e trace=open -o /tmp/t cat app.log",
        "chroot /srv/root cat /etc/app.conf",
        "busybox cat app.log",
        "coreutils cat app.log",
        "gcat app.log",
        "ghead -n 900 app.log",
        "gtail -f app.log",
        'powershell -Command "Get-Content app.log"',
        # 5. more binaries and interpreters
        "fold -w 80 notes.txt",
        "fmt notes.txt",
        "paste a.txt b.txt",
        "column -t data.tsv",
        "pr -2 notes.txt",
        "expand src.py",
        "unexpand src.py",
        "base64 secret.bin",
        "gunzip -c app.log.gz",
        "gzip -dc app.log.gz",
        "xz -dc app.log.xz",
        "xzcat app.log.xz",
        "bzcat app.log.bz2",
        "zstdcat app.log.zst",
        "tar -xOf bundle.tar config.json",
        "tar xOf bundle.tar",
        "tar -xf bundle.tar --to-stdout config.json",
        "unzip -p bundle.zip config.json",
        "view src.py",
        "most app.log",
        "batcat src.py",
        "highlight -O ansi src.py",
        "pygmentize -l python src.py",
        "glow README.md",
        "mdcat README.md",
        "yq . config.yaml",
        "gsutil cat gs://bucket/app.log",
        "gcloud storage cat gs://bucket/app.log",
        "aws s3 cp s3://bucket/app.log -",
        "perl -ne 'print if /x/' app.log",
        "perl -pe 's/a/b/' app.log",
        'perl -e \'open(my $f, "<", "app.log"); print <$f>\'',
        "ruby -e 'puts File.read(\"app.log\")'",
        "ruby -e 'puts File.readlines(\"app.log\")'",
        "ruby -e 'print IO.read(\"app.log\")'",
        "node -e \"require('fs').createReadStream('app.log').pipe(process.stdout)\"",
        "pwsh -c \"[IO.File]::ReadAllText('app.log')\"",
        "[System.IO.File]::ReadAllLines('app.log')",
        "python3 -c 'import fileinput; [print(l) for l in fileinput.input()]' app.log",
    ],
)
def test_file_dump(cmd: str) -> None:
    assert families.is_file_dump(cmd) is True
    assert filedump.is_file_dump(cmd) is True


@pytest.mark.parametrize(
    "cmd",
    [
        "grep -rn TODO src",
        "rg -n foo",
        "git blame src/app.py",
        "git log -p -5",
        "git diff HEAD~1",
        "echo $(date)",
        "echo `whoami`",
        "echo $((1 + 2))",
        "cat <<EOF\nhello\nEOF",
        "python3 - <<'EOF'\nprint(1)\nEOF",
        "wc -l <<< 'a b c'",
        "ssh host uptime",
        "docker exec web ls -la",
        "docker run --rm alpine echo hi",
        "kubectl exec pod-1 -- env",
        "watch -n 5 kubectl get pods",
        "dd if=/dev/zero of=out.bin bs=1M count=1",
        "cp a.txt b.txt",
        "tar -xf bundle.tar",
        "unzip bundle.zip",
        "gzip app.log",
        "perl -e 'print 1+1'",
        "ruby -e 'puts 42'",
        "aws s3 cp app.log s3://bucket/app.log",
        "pytest | column -t",
    ],
)
def test_not_a_file_dump(cmd: str) -> None:
    assert filedump.is_file_dump(cmd) is False


@pytest.mark.parametrize(
    "name",
    [
        "Read",
        "read",
        "cat",
        "view",
        "open",
        "mcp__fs__read_file",
        "mcp__fs__read_multiple_files",
        "mcp__files__ReadFile",
        "mcp__repo__get_file_contents",
        "mcp__x__file_read",
        "mcp__x__read_text_file",
        "mcp__x__cat_file",
        "mcp__editor__view_file",
        "mcp__editor__open_file",
        "mcp__docs__fetch_document",
        "mcp__drive__download_file",
        "mcp__media__getMedia",
        "mcp__srv__read_resource",
        "mcp__cms__get_content",
        "mcp__notes__read_page",
        "mcp__srv__cat",
    ],
)
def test_file_reading_tool_names(name: str) -> None:
    assert filedump.is_file_tool(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "Bash",
        "Grep",
        "WebFetch",
        "WebSearch",
        "Agent",
        "mcp__api__list_users",
        "mcp__github__create_issue",
        "mcp__x__get_weather",
        "mcp__x__readme_generator",
        "mcp__x__thread_summary",
        "mcp__x__category_list",
        "mcp__x__review_pr",
    ],
)
def test_other_tool_names(name: str) -> None:
    assert filedump.is_file_tool(name) is False
