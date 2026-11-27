"""Supply-chain and CI least-privilege guards (text level, stdlib only: no YAML parser)."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
PLUGIN_TREE = ("hooks", "skills", ".claude-plugin", "token_sparen")
CONTENTS_WRITE_JOBS = frozenset({"draft-release", "push-tree", "finalize", "wiki"})
ID_TOKEN_JOBS = frozenset({"provenance", "publish-pypi", "deploy-pages"})
_USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)(.*)$")
_PINNED = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}")
# isolated (`-I`): a repository's committed settings env must not inject PYTHONPATH/PYTHONSTARTUP
_HOOK_COMMAND = re.compile(r'python3 -I "\$\{CLAUDE_PLUGIN_ROOT\}/hooks/[a-z_]+\.py"( \w+)?')


def _workflows() -> list[Path]:
    found = sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])
    assert found, "no workflows found"
    return found


def _lines(path: Path) -> list[str]:
    return [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _top_block(lines: list[str], key: str) -> list[str] | None:
    """The lines of a top-level ``key:`` block (the key line included), None if absent."""
    for i, line in enumerate(lines):
        if line.startswith(f"{key}:"):
            block = [line]
            for nxt in lines[i + 1 :]:
                if not nxt.startswith((" ", "\t")):
                    break
                block.append(nxt)
            return block
    return None


def _jobs(lines: list[str]) -> dict[str, list[str]]:
    """Job id -> its lines (jobs are the 2-space-indented keys under ``jobs:``)."""
    block = _top_block(lines, "jobs") or []
    jobs: dict[str, list[str]] = {}
    current: str | None = None
    for line in block[1:]:
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m:
            current = m.group(1)
            jobs[current] = []
        elif current is not None:
            jobs[current].append(line)
    return jobs


def _grants(lines: list[str], scope: str) -> bool:
    return any(re.search(rf"\b{re.escape(scope)}:\s*write\b", line) for line in lines)


def test_runtime_dependencies_empty() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["dependencies"] == []
    assert data["build-system"]["requires"] == ["setuptools>=77"]


def test_actions_pinned_by_sha() -> None:
    for wf in _workflows():
        for line in _lines(wf):
            m = _USES.match(line)
            if m is None:
                continue
            ref, rest = m.group(1).strip("'\""), m.group(2)
            if ref.startswith("./"):
                continue  # a reusable workflow of this repository
            assert _PINNED.fullmatch(ref), f"{wf.name}: {ref} is not pinned to a commit SHA"
            assert re.search(r"#\s*v\d", rest), f"{wf.name}: {ref} needs a `# vX.Y.Z` comment"


def test_workflows_least_privilege() -> None:
    for wf in _workflows():
        lines = _lines(wf)
        text = "\n".join(lines)
        top = _top_block(lines, "permissions")
        assert top is not None, f"{wf.name}: no top-level permissions"
        assert not any("write" in line for line in top), f"{wf.name}: top-level write grant"
        assert "secrets: inherit" not in text, wf.name
        assert "pull_request_target" not in text, wf.name
        for job, body in _jobs(lines).items():
            if _grants(body, "contents"):
                assert job in CONTENTS_WRITE_JOBS, f"{wf.name}: job {job} has contents: write"
            if _grants(body, "id-token"):
                assert job in ID_TOKEN_JOBS, f"{wf.name}: job {job} has id-token: write"
            for scope in ("actions", "packages", "pull-requests", "issues", "deployments"):
                assert not _grants(body, scope), f"{wf.name}: job {job} has {scope}: write"
            assert not re.search(r"permissions:\s*write-all", "\n".join(body)), job


def test_least_privilege_parser_sees_grants() -> None:
    """The text-level checks above can fail: a write grant in an unlisted job is found."""
    sample = [
        "permissions:",
        "  contents: read",
        "jobs:",
        "  build:",
        "    permissions:",
        "      contents: write",
        "  wiki:",
        "    permissions:",
        "      contents: write",
    ]
    jobs = _jobs(sample)
    assert set(jobs) == {"build", "wiki"}
    assert _grants(jobs["build"], "contents") and "build" not in CONTENTS_WRITE_JOBS
    assert not _grants(_top_block(sample, "permissions") or [], "contents")


def test_ci_is_reusable_and_fails_closed_on_main() -> None:
    lines = _lines(WORKFLOWS / "ci.yml")
    text = "\n".join(lines)
    on = "\n".join(_top_block(lines, "on") or [])
    assert "workflow_call:" in on and "TOKEN_SPAREN_DENYLIST:" in on
    assert re.search(r"TOKEN_SPAREN_DENYLIST:\s*\n\s*required: false", on)
    assert "schedule:" in on and "cron:" in on
    jobs = _jobs(lines)
    denylist = "\n".join(jobs["denylist"])
    assert "--require-private-words" in denylist
    assert "github.event_name == 'push' && github.ref == 'refs/heads/main'" in denylist
    assert "secrets.TOKEN_SPAREN_DENYLIST" in denylist
    assert text.count("secrets.") == 1  # the deny-list is the only secret, passed explicitly
    chain = "\n".join(jobs["supply-chain"])
    assert "tests/test_supply_chain.py" in chain
    assert "pip-audit" in chain and "continue-on-error: true" in chain
    for job, body in jobs.items():
        joined = "\n".join(body)
        if "actions/setup-python" in joined and "pip install" in joined:
            assert "cache: pip" in joined, f"job {job} has no pip cache"
        if "actions/checkout" in joined:
            assert "persist-credentials: false" in joined, f"job {job} keeps the token"


def test_dependabot_watches_actions_and_pip() -> None:
    text = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    assert "version: 2" in text
    assert 'package-ecosystem: "github-actions"' in text
    assert 'package-ecosystem: "pip"' in text


def _plugin_files() -> list[Path]:
    out: list[Path] = []
    for top in PLUGIN_TREE:
        for path in (ROOT / top).rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                out.append(path)
    return out


def test_no_unpinned_launchers_in_plugin() -> None:
    """What the plugin runs (hook config, launchers, the skill's commands) never downloads an
    unpinned package; the package's own code never spawns a downloading launcher at all (it
    only parses such commands when reading tool output)."""
    assert not (ROOT / "bin").exists(), "no top-level bin/ in the plugin"
    launcher = re.compile(r"\b(npx|uvx|bunx|pnpx|pipx run)\s+(?:-{1,2}\S+\s+)*(\S+)")
    spawned = re.compile(r"""\[\s*["'](?:npx|uvx|bunx|pnpx|pipx)["']""")
    for path in _plugin_files():
        if path.suffix == ".py" and path.parts[len(ROOT.parts)] == "token_sparen":
            text = path.read_text(encoding="utf-8")
            assert not spawned.search(text), f"{path.relative_to(ROOT)} spawns a launcher"
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for m in launcher.finditer(text):
            package = m.group(2)
            assert re.search(r"(?:@|==)\d", package), (
                f"{path.relative_to(ROOT)}: unpinned `{m.group(1)} {package}`"
            )


def test_hook_commands_only_plugin_root() -> None:
    data = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    commands = [
        hook["command"]
        for groups in data["hooks"].values()
        for group in groups
        for hook in group["hooks"]
    ]
    assert commands
    for command in commands:
        assert _HOOK_COMMAND.fullmatch(command), command
        script = command.split('"')[1].replace("${CLAUDE_PLUGIN_ROOT}/", "")
        assert (ROOT / script).is_file(), script


def test_threat_model_pinning_tests_exist() -> None:
    """Every pinning test the threat model names exists (a renamed test must update the doc)."""
    doc = (ROOT / "docs" / "user" / "privacy-security.md").read_text(encoding="utf-8")
    rows = [line for line in doc.splitlines() if line.startswith("| ") and "`tests/" in line]
    assert len(rows) >= 15
    for row in rows:
        cell = row.rstrip(" |").rsplit(" | ", 1)[1]
        current: Path | None = None
        for token in re.findall(r"`([^`]+)`", cell):
            if "::" in token:
                rel, name = token.split("::", 1)
                current = ROOT / rel
            else:
                name = token
            assert current is not None and current.is_file(), token
            source = current.read_text(encoding="utf-8")
            assert re.search(rf"^def {re.escape(name)}\(", source, re.M), f"{current}::{name}"


# Subcommands the skill may run without a permission prompt: read-only (stats, show, doctor) or
# moving toward "token-sparen does less" (session disable, prune restore).
PREAPPROVED = ("stats", "show", "doctor", "session disable", "prune restore")


def test_skill_preapprovals_are_read_only_or_restoring() -> None:
    text = (ROOT / "skills" / "token-sparen" / "SKILL.md").read_text(encoding="utf-8")
    front = text.split("---", 2)[1]
    rules = re.findall(r"^\s*-\s*'?(Bash\(.*\))'?\s*$", front, re.MULTILINE)
    assert rules
    allowed = {f"Bash(token-sparen {c}:*)" for c in PREAPPROVED} | {
        f'Bash(python3 -I "${{CLAUDE_PLUGIN_ROOT}}/hooks/run_cli.py" {c}:*)' for c in PREAPPROVED
    }
    for rule in rules:
        assert rule in allowed, f"unexpected pre-approval {rule}"
    tools = re.findall(r"^\s*-\s*'?([A-Za-z]+)", front.split("allowed-tools:", 1)[1], re.M)
    assert set(tools) == {"Bash"}, tools  # no Write, Edit or other tool is pre-approved


def test_shipped_and_release_code_never_imports_tests() -> None:
    """Release tooling, the package and the bench harness never depend on a test module (a test
    refactor must not break a release mid-way; `tests/` is absent from sdist and the tree)."""
    import ast

    offenders: list[str] = []
    for top in ("scripts", "token_sparen", "bench"):
        for path in sorted((ROOT / top).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module]
                for name in names:
                    if name == "tests" or name.startswith("tests."):
                        offenders.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")
    assert offenders == []
