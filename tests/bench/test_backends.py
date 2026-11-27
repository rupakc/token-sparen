"""Execution backends (M5 Task 6): fake backend end to end, Docker backend via a mocked CLI.

No test here runs Docker or `claude`: the Docker backend's subprocess calls go to `FakeDocker`,
which records them and plays the container's part (writing what `entry.sh` writes to
/run-out); `entry.sh` itself runs on POSIX hosts with a fake `timeout` and a fake agent.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from bench import arms as am
from bench import backends as bk
from bench import freeze as fz
from bench import leak as lk
from bench import tasks as tk

SECRET = "sk-ant-oat01-" + "Q" * 40
MODEL = "claude-sonnet-4-6"
ARMS = am.load_arms()
TASK = tk.FAKE_TASKS[0]
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
GIT += ["-c", "commit.gpgSign=false", "-c", "init.defaultBranch=main", "-c", "core.autocrlf=false"]
POSIX = pytest.mark.skipif(os.name != "posix", reason="entry.sh runs under a POSIX shell")


@pytest.fixture(scope="module")
def frozen(tmp_path_factory: pytest.TempPathFactory) -> fz.FrozenPlugin:
    root = tmp_path_factory.mktemp("plugin-src")
    files = {
        ".claude-plugin/plugin.json": b'{"name": "token-sparen", "version": "0.0.0"}\n',
        "hooks/hooks.json": b"{}\n",
        "skills/token-sparen/SKILL.md": b"# skill\n",
        "token_sparen/__init__.py": b"",
    }
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "p"]):
        subprocess.run([*GIT, "-C", str(root), *args], check=True, capture_output=True)
    dest = tmp_path_factory.mktemp("frozen-parent") / "frozen"
    return fz.freeze(root, "HEAD", dest, dry_run=True)


def _req(
    tmp_path: Path,
    arm: str = "baseline",
    *,
    frozen: fz.FrozenPlugin | None = None,
    timeout_s: int = 600,
    attempt: int = 1,
    tag: str = "r",
) -> bk.RunRequest:
    state_in = tmp_path / f"state-in-{tag}"
    state_in.mkdir(exist_ok=True)
    return bk.RunRequest(
        task=TASK,
        arm=ARMS[arm],
        seed=0,
        stage="screening",
        model=MODEL,
        effort="high",
        max_turns=80,
        timeout_s=timeout_s,
        state_in=state_in,
        run_dir=tmp_path / f"run-{tag}",
        frozen=frozen if ARMS[arm].uses_frozen_plugin else None,
        attempt=attempt,
        campaign="c1",
        problem_statement="calc.total(n) misses n.",
    )


def _env_base() -> dict[str, str]:
    base = {"CLAUDE_CODE_OAUTH_TOKEN": SECRET, "ANTHROPIC_API_KEY": "sk-ant-api-nope"}
    for key in ("PATH", "SYSTEMROOT"):
        if key in os.environ:
            base[key] = os.environ[key]
    return base


# --- argv --------------------------------------------------------------------------------


def test_argv_pins_flags(tmp_path: Path, frozen: fz.FrozenPlugin) -> None:
    argv = bk.build_claude_argv(_req(tmp_path, "token-sparen", frozen=frozen), mcp_path="/m.json")
    assert argv[0] == "claude"
    pairs = {argv[i]: argv[i + 1] for i in range(1, len(argv) - 1)}
    assert pairs["--model"] == MODEL and pairs["--effort"] == "high"
    assert pairs["--max-turns"] == "80"
    assert pairs["--output-format"] == "stream-json"
    assert pairs["--permission-mode"] == "bypassPermissions"
    assert pairs["--setting-sources"] == "user"
    assert pairs["--mcp-config"] == "/m.json"
    assert pairs["--plugin-dir"] == "/opt/plugin/token-sparen"
    for flag in ("--verbose", "--include-hook-events", "--strict-mcp-config"):
        assert flag in argv
    prompt = pairs["-p"]
    assert prompt.startswith(bk.PROMPT_FILE.read_text(encoding="utf-8").strip()[:40])
    assert prompt.rstrip().endswith("calc.total(n) misses n.")
    assert "--append-system-prompt" not in argv

    base = bk.build_claude_argv(_req(tmp_path), mcp_path="/m.json")
    assert "--plugin-dir" not in base
    concise = bk.build_claude_argv(_req(tmp_path, "concise"), mcp_path="/m.json")
    i = concise.index("--append-system-prompt")
    assert concise[i + 1] == ARMS["concise"].append_system_prompt
    wrapped = bk.build_claude_argv(_req(tmp_path, "headroom"), mcp_path="/m.json")
    assert wrapped[:4] == ["headroom", "wrap", "claude", "--"] and wrapped[4] == "-p"


def test_container_name_and_key(tmp_path: Path) -> None:
    req = _req(tmp_path, attempt=2)
    assert req.key_id == f"c1/screening/baseline/0/{TASK.instance_id}"
    name = bk.container_name(req.key_id, 2)
    assert name.startswith("tsb-") and name.endswith("-a2") and len(name) == len("tsb-") + 12 + 3
    assert name == bk.container_name(req.key_id, 2) != bk.container_name(req.key_id, 3)


def test_fixed_env_pins_global_constraints() -> None:
    env = bk.fixed_env(80)
    assert env["CLAUDE_CODE_MAX_TURNS"] == "80"
    for key in (
        "DISABLE_AUTOUPDATER",
        "IS_SANDBOX",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
        "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB",
        "DO_NOT_TRACK",
    ):
        assert env[key] == "1"


# --- fake backend ------------------------------------------------------------------------


def _fake(scenario: str) -> bk.FakeBackend:
    return bk.FakeBackend(scenario=lambda _req: scenario, environ=_env_base())


@pytest.mark.parametrize(
    ("scenario", "status", "has_patch", "promoted"),
    [
        ("success", ("ok", "success"), True, True),
        ("fail", ("ok", "success"), False, True),
        ("quota", ("infra_fail", "rate"), False, False),
        ("auth", ("infra_fail", "auth"), False, False),
        ("api", ("infra_fail", "api"), False, False),
        ("inert", ("infra_fail", "load"), True, False),
        ("timeout", ("agent_fail", "timeout"), True, True),
        ("leak", ("infra_fail", "leak"), False, False),
        ("crash", ("infra_fail", "unknown"), False, False),
    ],
)
def test_fake_backend_scenarios(
    tmp_path: Path,
    frozen: fz.FrozenPlugin,
    scenario: str,
    status: tuple[str, str],
    has_patch: bool,
    promoted: bool,
) -> None:
    req = _req(tmp_path, "token-sparen", frozen=frozen)
    out = _fake(scenario).run(req)
    assert out.status == status, out.problems
    assert ("n + 1" in out.patch) is has_patch
    assert (out.state_out is not None) is promoted
    if promoted:
        assert out.state_out is not None
        assert (out.state_out / ".token-sparen" / "projects").is_dir()
        assert list((out.state_out / ".claude" / "projects").rglob("*.jsonl"))
    if scenario == "leak":
        assert (req.run_dir / lk.QUARANTINE_MARKER).is_file()
        assert [p.name for p in req.run_dir.iterdir()] == [lk.QUARANTINE_MARKER]
    if scenario == "success":
        assert out.metrics is not None and out.metrics.session_id
        assert json.loads((req.run_dir / "exit.json").read_text(encoding="utf-8")) == {
            "agent_exit": 0,
            "timed_out": False,
            "patch_exit": 0,
        }
    assert out.wall_s >= 0


def test_fake_backend_state_in_reaches_the_run_and_carries(
    tmp_path: Path, frozen: fz.FrozenPlugin
) -> None:
    req = _req(tmp_path, "token-sparen", frozen=frozen)
    marker = req.state_in / ".token-sparen" / "global" / "carried.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("{}", encoding="utf-8")
    out = _fake("success").run(req)
    assert out.state_out is not None
    assert (out.state_out / ".token-sparen" / "global" / "carried.json").is_file()


def test_fake_backend_baseline_and_refusals(tmp_path: Path, frozen: fz.FrozenPlugin) -> None:
    out = _fake("success").run(_req(tmp_path, "baseline", tag="b"))
    assert out.status == ("ok", "success")
    # a frozen-plugin arm without a frozen plugin is a load failure, never a run of nothing
    no_plugin = replace(_req(tmp_path, "token-sparen", tag="np"), frozen=None)
    assert _fake("success").run(no_plugin).status == ("infra_fail", "load")
    # a run directory that already holds outputs is refused
    used = _req(tmp_path, tag="used")
    used.run_dir.mkdir()
    (used.run_dir / "stream.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(bk.BackendError):
        _fake("success").run(used)
    # a task that is not the fake task
    other = replace(_req(tmp_path, tag="o"), task=replace(TASK, base_commit="0" * 40))
    with pytest.raises(bk.BackendError):
        _fake("success").run(other)


def test_fake_backend_refuses_changed_frozen_plugin(
    tmp_path: Path, frozen: fz.FrozenPlugin
) -> None:
    changed = replace(frozen, plugin_hash="0" * 64)
    req = _req(tmp_path, "token-sparen", frozen=changed)
    with pytest.raises(fz.FrozenPluginChanged):
        _fake("success").run(req)


def test_fake_backend_image_and_disk_contract(tmp_path: Path) -> None:
    fake = _fake("success")
    fake.ensure_image("img:1")
    assert bk.release_image(fake, "img:1", keep=frozenset()) is True
    assert bk.release_image(fake, "img:2", keep=frozenset({"img:2"})) is False
    assert fake.removed == ["img:1"] and fake.ensured == ["img:1"]
    assert fake.free_disk_gb() > 30
    assert fake.reap("c1") == 0


def test_token_never_in_argv_or_files(tmp_path: Path, frozen: fz.FrozenPlugin) -> None:
    fake = _fake("success")
    req = _req(tmp_path, "token-sparen", frozen=frozen)
    out = fake.run(req)
    assert out.status == ("ok", "success")
    assert fake.last_argv and not any(SECRET in a for a in fake.last_argv)
    assert "ANTHROPIC_API_KEY" not in fake.last_env_names
    assert lk.scan_for_token([req.run_dir], SECRET) == []

    docker = FakeDocker(container=_container_success)
    backend = _docker(tmp_path, docker)
    dreq = _req(tmp_path, "token-sparen", frozen=frozen, tag="d")
    assert backend.run(dreq).status == ("ok", "success")
    argv, env, _ = docker.run_call()
    assert not any(SECRET in a for a in argv)
    names = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
    assert names and all("=" not in n for n in names)
    assert "CLAUDE_CODE_OAUTH_TOKEN" in names and env["CLAUDE_CODE_OAUTH_TOKEN"] == SECRET
    assert "ANTHROPIC_API_KEY" not in names and "ANTHROPIC_API_KEY" not in env
    assert set(names) <= set(env)  # every name has its value in the client's explicit env
    assert lk.scan_for_token([dreq.run_dir], SECRET) == []


# --- docker backend (mocked CLI) ---------------------------------------------------------


def _cp(argv: list[str], out: str = "", rc: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, rc, out, "")


Container = Callable[[Path, Mapping[str, str]], None]


class FakeDocker:
    """Records docker CLI calls; plays the container for `docker run`."""

    def __init__(
        self,
        *,
        container: Container | None = None,
        key_ids: str = "",
        oom: bool = False,
        hang: bool = False,
        run_rc: int = 0,
        busy: str = "",
        image_present: bool = True,
        df: str = "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
        "overlay 100000000 1000 62500000 1% /\n",
    ) -> None:
        self.calls: list[tuple[list[str], dict[str, str], float | None]] = []
        self.container, self.key_ids, self.oom, self.hang = container, key_ids, oom, hang
        self.run_rc, self.busy, self.image_present, self.df = run_rc, busy, image_present, df

    def __call__(
        self, argv: list[str], env: Mapping[str, str], timeout: float | None
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), dict(env), timeout))
        assert argv[0] == "docker"
        if argv[1] == "ps":
            label = argv[argv.index("--filter") + 1]
            return _cp(argv, self.key_ids if label.startswith("label=tsb.key=") else self.busy)
        if argv[1] == "run" and "--rm" not in argv:
            if self.hang:
                raise subprocess.TimeoutExpired(argv, timeout or 0)
            mounts = dict(_mounts(argv))
            if self.container is not None:
                self.container(Path(mounts["/run-out"]), env)
            return _cp(argv, rc=self.run_rc)
        if argv[1] == "run":  # disk probe
            return _cp(argv, self.df)
        if argv[1] == "inspect":
            return _cp(argv, "true\n" if self.oom else "false\n")
        if argv[1:3] == ["image", "inspect"]:
            return _cp(argv, rc=0 if self.image_present else 1)
        return _cp(argv)

    def run_call(self) -> tuple[list[str], dict[str, str], float | None]:
        runs = [c for c in self.calls if c[0][1] == "run" and "--rm" not in c[0]]
        assert len(runs) == 1
        return runs[0]

    def last_index(self, *prefix: str) -> int:
        hits = [i for i, c in enumerate(self.calls) if c[0][: len(prefix)] == list(prefix)]
        assert hits, f"no call {prefix}"
        return hits[-1]

    def index(self, *prefix: str) -> int:
        for i, (argv, _, _) in enumerate(self.calls):
            if argv[: len(prefix)] == list(prefix):
                return i
        raise AssertionError(f"no call {prefix}")


def _mounts(argv: list[str]) -> list[tuple[str, str]]:
    """(container path, host path + ":ro" when read-only) of every -v."""
    out = []
    for i, a in enumerate(argv):
        if a == "-v":
            spec = argv[i + 1]
            ro = spec.endswith(":ro")
            src, dst = spec.removesuffix(":ro").rsplit(":", 1)
            out.append((dst, src + (":ro" if ro else "")))
    return out


STREAM = [
    {
        "type": "system",
        "subtype": "init",
        "session_id": "s1",
        "model": MODEL,
        "claude_code_version": "2.1.150",
        "plugins": [],
        "mcp_servers": [],
    },
    {
        "type": "system",
        "subtype": "hook_response",
        "hook_event": "SessionStart",
        "hook_name": "SessionStart",
        "command": 'python3 -I "/opt/plugin/token-sparen/hooks/run.py" SessionStart',
        "exit_code": 0,
        "outcome": "success",
        "session_id": "s1",
    },
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": "s1",
        "num_turns": 1,
        "total_cost_usd": 0.01,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    },
]


def _write_container_outputs(
    out: Path, *, agent_exit: int, timed_out: bool, stream: list[dict[str, Any]], ts: bool
) -> None:
    lines = [json.dumps(e) for e in stream]
    if ts:
        init = dict(
            stream[0], plugins=[{"name": "token-sparen", "path": "/opt/plugin/token-sparen"}]
        )
        lines[0] = json.dumps(init)
    (out / "stream.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "patch.diff").write_text("diff --git a/calc.py b/calc.py\n+n + 1\n", encoding="utf-8")
    sess = out / "state-out" / ".token-sparen" / "projects" / ("0" * 16) / "sessions" / "s1.json"
    sess.parent.mkdir(parents=True)
    sess.write_text("{}", encoding="utf-8")
    exit_doc = {"agent_exit": agent_exit, "timed_out": timed_out, "patch_exit": 0}
    (out / "exit.json").write_text(json.dumps(exit_doc), encoding="utf-8")


def _container_success(out: Path, env: Mapping[str, str]) -> None:
    _write_container_outputs(out, agent_exit=0, timed_out=False, stream=STREAM, ts=True)


def _container_timeout(out: Path, env: Mapping[str, str]) -> None:
    _write_container_outputs(out, agent_exit=124, timed_out=True, stream=STREAM[:2], ts=True)


def _docker(tmp_path: Path, docker: FakeDocker, **kw: Any) -> bk.DockerBackend:
    toolchain = tmp_path / "toolchain"
    templates = tmp_path / "templates"
    for name in ("baseline", "token-sparen"):
        (templates / name / ".claude").mkdir(parents=True, exist_ok=True)
    toolchain.mkdir(exist_ok=True)
    return bk.DockerBackend(toolchain, templates, runner=docker, environ=_env_base(), **kw)


def test_docker_command_limits_and_mounts(tmp_path: Path, frozen: fz.FrozenPlugin) -> None:
    docker = FakeDocker(container=_container_success)
    backend = _docker(tmp_path, docker)
    req = _req(tmp_path, "token-sparen", frozen=frozen)
    out = backend.run(req)
    assert out.status == ("ok", "success"), out.problems
    argv, env, timeout = docker.run_call()
    name = bk.container_name(req.key_id, req.attempt)
    flags = {argv[i]: argv[i + 1] for i in range(len(argv) - 1) if argv[i].startswith("--")}
    assert flags["--name"] == name
    assert flags["--platform"] == "linux/arm64"
    assert flags["--memory"] == "4g" and flags["--memory-swap"] == "4g"
    assert flags["--cpus"] == "4" and flags["--pids-limit"] == "4096"
    assert flags["--pull"] == "never"
    labels = [argv[i + 1] for i, a in enumerate(argv) if a == "--label"]
    assert labels == ["tsb.campaign=c1", f"tsb.key={req.key_id}"]
    mounts = dict(_mounts(argv))
    assert mounts["/opt/bench"] == f"{tmp_path / 'toolchain'}:ro"
    assert mounts["/opt/plugin/token-sparen"] == f"{frozen.path}:ro"
    assert mounts["/state-in"] == f"{req.state_in}:ro"
    assert mounts["/run-out"] == str(req.run_dir)  # the only writable mount
    assert mounts["/opt/template"] == f"{tmp_path / 'templates' / 'token-sparen'}:ro"
    assert mounts["/opt/tsb/entry.sh"].endswith(":ro") and mounts["/opt/tsb/patch.py"].endswith(
        ":ro"
    )
    writable = [dst for dst, src in mounts.items() if not src.endswith(":ro")]
    assert writable == ["/run-out"]
    names = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
    assert "ANTHROPIC_API_KEY" not in names and "HOME" not in names
    assert env["TSB_TIMEOUT_S"] == "600" and "TSB_TIMEOUT_S" in names
    assert env["TOKEN_SPAREN_PYTHON"] == "/opt/bench/bin/python3.12"
    assert ".headroom/logs" in env["TSB_STATE_PATHS"].splitlines()
    assert ".token-sparen" in env["TSB_STATE_PATHS"].splitlines()
    assert ".claude/projects" in env["TSB_STATE_PATHS"].splitlines()
    assert env["TSB_PATCH_EXCLUDES"].splitlines() == list(ARMS["token-sparen"].patch_excludes)
    assert env["TSB_BASE_COMMIT"] == TASK.base_commit
    # image, then the entry script, then the claude argv
    image_at = argv.index(TASK.image)
    assert argv[image_at + 1 : image_at + 3] == ["/opt/tsb/entry.sh", "claude"]
    assert flags["--entrypoint"] == "/bin/sh"
    # the host backstop is the agent timeout + 120 s; the agent timeout runs inside
    assert timeout == 600 + bk.BACKSTOP_S == 720
    entry = bk.ENTRY_SCRIPT.read_text(encoding="utf-8")
    assert 'timeout --signal=TERM --kill-after=30 "$TSB_TIMEOUT_S"' in entry
    # client env is explicit: no stray host variable reaches docker
    assert set(env) <= set(bk.CLIENT_ENV_KEYS) | set(names)
    # container inspected for OOM, then removed
    removals = [i for i, c in enumerate(docker.calls) if c[0] == ["docker", "rm", "-f", name]]
    assert removals[0] < docker.index("docker", "run", "--name") < docker.index("docker", "inspect")
    assert docker.index("docker", "inspect") < removals[-1]


def test_docker_oom_is_infra(tmp_path: Path) -> None:
    docker = FakeDocker(container=_container_success, oom=True)
    out = _docker(tmp_path, docker).run(_req(tmp_path))
    assert out.status == ("infra_fail", "docker") and out.state_out is None


def test_docker_client_exit_125_is_infra(tmp_path: Path) -> None:
    docker = FakeDocker(container=None, run_rc=125)
    out = _docker(tmp_path, docker).run(_req(tmp_path))
    assert out.status == ("infra_fail", "docker") and out.patch == ""


def test_orphan_container_killed_before_rerun(tmp_path: Path) -> None:
    docker = FakeDocker(container=_container_success, key_ids="dead1\ndead2\n")
    req = _req(tmp_path, attempt=2)
    _docker(tmp_path, docker).run(req)
    run_at = docker.index("docker", "run", "--name")
    ps_at = docker.index("docker", "ps", "-aq", "--filter", f"label=tsb.key={req.key_id}")
    assert ps_at < docker.index("docker", "rm", "-f", "dead1", "dead2") < run_at
    name = bk.container_name(req.key_id, 2)
    assert docker.index("docker", "rm", "-f", name) < run_at


def test_refuses_when_another_bench_container_runs(tmp_path: Path) -> None:
    docker = FakeDocker(container=_container_success, busy="other\n")
    with pytest.raises(bk.BackendError):
        _docker(tmp_path, docker).run(_req(tmp_path))
    assert not [c for c in docker.calls if c[0][1] == "run"]


def test_reap_kills_campaign_containers(tmp_path: Path) -> None:
    docker = FakeDocker(busy="a\nb\n")
    assert _docker(tmp_path, docker).reap("c1") == 2
    assert ["docker", "ps", "-aq", "--filter", "label=tsb.campaign=c1"] in [
        c[0] for c in docker.calls
    ]
    assert ["docker", "rm", "-f", "a", "b"] in [c[0] for c in docker.calls]
    assert _docker(tmp_path, FakeDocker()).reap("c1") == 0


def test_timeout_inside_container_keeps_patch_and_state(
    tmp_path: Path, frozen: fz.FrozenPlugin
) -> None:
    docker = FakeDocker(container=_container_timeout)
    out = _docker(tmp_path, docker).run(_req(tmp_path, "token-sparen", frozen=frozen))
    assert out.status == ("agent_fail", "timeout")
    assert "n + 1" in out.patch
    assert out.state_out is not None and (out.state_out / ".token-sparen").is_dir()


def test_backstop_kill_at_timeout_plus_120(tmp_path: Path) -> None:
    docker = FakeDocker(hang=True)
    req = _req(tmp_path, timeout_s=900)
    out = _docker(tmp_path, docker).run(req)
    _, _, timeout = docker.run_call()
    assert timeout == 900 + 120
    name = bk.container_name(req.key_id, req.attempt)
    assert docker.index("docker", "rm", "-f", name) < docker.index("docker", "run", "--name")
    assert docker.index("docker", "kill", name) < docker.last_index("docker", "rm", "-f", name)
    assert out.status == ("infra_fail", "docker")
    assert out.patch == "" and out.state_out is None
    assert any("backstop" in p for p in out.problems)


def test_image_removed_after_task_block_unless_kept(tmp_path: Path) -> None:
    docker = FakeDocker()
    backend = _docker(tmp_path, docker)
    assert bk.release_image(backend, "img:a", keep=frozenset({"img:b"})) is True
    assert docker.index("docker", "image", "rm", "img:a") < docker.index(
        "docker", "image", "prune", "-f"
    )
    n = len(docker.calls)
    assert bk.release_image(backend, "img:b", keep=frozenset({"img:b"})) is False
    assert len(docker.calls) == n


def test_ensure_image_pulls_only_when_missing(tmp_path: Path) -> None:
    present = FakeDocker()
    _docker(tmp_path, present).ensure_image("img:a")
    assert not [c for c in present.calls if c[0][1] == "pull"]
    missing = FakeDocker(image_present=False)
    _docker(tmp_path, missing).ensure_image("img:a")
    assert ["docker", "pull", "--platform", "linux/arm64", "img:a"] in [c[0] for c in missing.calls]


def test_free_disk_is_min_of_host_and_docker(tmp_path: Path) -> None:
    docker = FakeDocker()  # 62_500_000 KiB available in the VM = 64.0 GB
    backend = _docker(tmp_path, docker)
    host = shutil.disk_usage(tmp_path).free / 1e9
    assert backend.free_disk_gb() == pytest.approx(min(host, 64.0))
    broken = _docker(tmp_path, FakeDocker(df="garbage"))
    assert broken.free_disk_gb() == 0.0  # unknown = fail closed (pauses the scheduler)


# --- entry.sh (POSIX hosts; the container's part) ----------------------------------------


def _entry_env(tmp_path: Path, scenario: str, timeout_rc: int) -> tuple[dict[str, str], Path]:
    root = tmp_path / "ctr"
    template, state_in, out, home = (root / n for n in ("template", "state-in", "out", "home"))
    for d in (template / ".claude", state_in / ".token-sparen" / "global", out):
        d.mkdir(parents=True)
    (state_in / ".token-sparen" / "global" / "carried.json").write_text("{}", encoding="utf-8")
    os.symlink("/", state_in / "escape")  # never carried into the run home
    testbed = root / "testbed"
    tk.make_fake_repo(testbed)
    bindir = root / "bin"
    bindir.mkdir()
    # fake coreutils `timeout`: runs the command (dropping the 3 timeout args), then
    # reports `timeout_rc` the way GNU timeout does when the limit fired
    fake_timeout = bindir / "timeout"
    fake_timeout.write_text(
        f'#!/bin/sh\nshift 3\n"$@"\nrc=$?\n[ {timeout_rc} -ne 0 ] && exit {timeout_rc}\nexit $rc\n',
        encoding="utf-8",
    )
    fake_claude = bindir / "claude"
    fake_claude.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" -I "{tk.FAKE_DIR / "agent.py"}" "$@"\n',
        encoding="utf-8",
    )
    for f in (fake_timeout, fake_claude):
        f.chmod(0o755)
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "TSB_BIN": str(bindir),
        "TSB_HOME_DIR": str(home),
        "TSB_TEMPLATE": str(template),
        "TSB_STATE_IN": str(state_in),
        "TSB_OUT": str(out),
        "TSB_TESTBED": str(testbed),
        "TSB_PYTHON": sys.executable,
        "TSB_PATCH_PY": str(bk.PATCH_SCRIPT),
        "TSB_TIMEOUT_S": "5",
        "TSB_BASE_COMMIT": tk.FAKE_BASE_COMMIT,
        "TSB_PATCH_EXCLUDES": "\n".join(ARMS["token-sparen"].patch_excludes),
        "TSB_STATE_PATHS": ".token-sparen\n.claude/projects\n.headroom/logs\n../escape\n",
        "TSB_FAKE_SCENARIO": scenario,
        "CLAUDE_CODE_OAUTH_TOKEN": SECRET,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    return env, out


def _entry(env: Mapping[str, str], *args: str) -> int:
    return subprocess.run(
        ["/bin/sh", str(bk.ENTRY_SCRIPT), *args], env=dict(env), capture_output=True, check=False
    ).returncode


@POSIX
def test_entry_script_timeout_keeps_patch_and_state(tmp_path: Path) -> None:
    env, out = _entry_env(tmp_path, "timeout", 124)
    argv = ["claude", "-p", "fix", "--model", MODEL, "--plugin-dir", "/opt/plugin/token-sparen"]
    assert _entry(env, *argv) == 0
    assert json.loads((out / "exit.json").read_text(encoding="utf-8")) == {
        "agent_exit": 124,
        "timed_out": True,
        "patch_exit": 0,
    }
    assert "n + 1" in (out / "patch.diff").read_text(encoding="utf-8")
    state = out / "state-out"
    assert (state / ".token-sparen" / "global" / "carried.json").is_file()
    assert list((state / ".token-sparen" / "projects").rglob("*.json"))
    assert list((state / ".claude" / "projects").rglob("*.jsonl"))
    assert not (state / "escape").exists() and not (tmp_path / "ctr" / "escape").exists()
    assert not [p for p in state.rglob("*") if p.is_symlink()]
    assert lk.scan_for_token([out], SECRET) == []
    home = tmp_path / "ctr" / "home"
    assert not (home / "escape").exists()
    assert json.loads((home / "mcp.json").read_text(encoding="utf-8")) == {"mcpServers": {}}


@POSIX
def test_entry_script_success_and_symlinked_state_path(tmp_path: Path) -> None:
    env, out = _entry_env(tmp_path, "success", 0)
    # a run that turns a state path into a link: not copied out
    env["TSB_FAKE_SCENARIO"] = "symlink"
    assert _entry(env, "claude", "-p", "fix", "--model", MODEL) == 0
    doc = json.loads((out / "exit.json").read_text(encoding="utf-8"))
    assert doc == {"agent_exit": 0, "timed_out": False, "patch_exit": 0}
    assert not (out / "state-out" / ".token-sparen").exists()
    assert (out / "stream.jsonl").read_text(encoding="utf-8").count('"type"') >= 3
