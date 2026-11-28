"""Hook launcher interpreter search (`hooks/_pyfind.py`, `hooks/run.py`, `hooks/run_cli.py`).

No test depends on the interpreters installed on this machine: candidates are fake executables
in a temporary PATH (POSIX sh scripts that answer the version probe) or patched callables.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from sampleoutputs import big_pytest
from userconf import to_user_config

ROOT = Path(__file__).resolve().parent.parent
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="fake sh executables")


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def pyfind() -> ModuleType:
    return _load("ts_test_pyfind", ROOT / "hooks" / "_pyfind.py")


@pytest.fixture
def runpy_mod() -> ModuleType:
    return _load("ts_test_run", ROOT / "hooks" / "run.py")


def _fake_python(
    bindir: Path, name: str, version: tuple[int, int], log: Path | None = None
) -> Path:
    """A fake interpreter: answers the probe with ``major minor <own path>``."""
    bindir.mkdir(parents=True, exist_ok=True)
    path = bindir / name
    record = f'echo "$@" >> "{log}"\n' if log is not None else ""
    path.write_text(f'#!/bin/sh\n{record}echo "{version[0]} {version[1]} $0"\n', encoding="utf-8")
    path.chmod(0o755)
    return path


def _env(tmp_path: Path, bindir: Path, **extra: str) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {"PATH": str(bindir), "TOKEN_SPAREN_HOME": str(home), "TOKEN_SPAREN_NO_UV": "1"}
    env.update(extra)
    return env


# --- interpreter search ------------------------------------------------------------------------


@posix_only
def test_launcher_reexecs_newer_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runpy_mod: ModuleType
) -> None:
    bindir = tmp_path / "bin"
    _fake_python(bindir, "python3", (3, 9))
    newer = _fake_python(bindir, "python3.13", (3, 13))
    env = _env(tmp_path, bindir)
    calls: list[tuple[str, list[str]]] = []

    def execv(path: str, argv: list[str]) -> None:
        calls.append((path, list(argv)))
        raise SystemExit(0)

    monkeypatch.setattr(os, "execv", execv)
    monkeypatch.setattr(os, "environ", dict(env))
    with pytest.raises(SystemExit):
        runpy_mod._hop(["run.py", "PostToolUse"], os.environ, (3, 9))
    script = os.path.abspath(runpy_mod.__file__)
    assert calls == [(str(newer), [str(newer), "-I", "-S", script, "PostToolUse"])]
    assert os.environ["TOKEN_SPAREN_REEXEC"] == "1"


def test_candidates_descend_from_315_to_311(pyfind: ModuleType, tmp_path: Path) -> None:
    asked: list[str] = []

    def which(name: str) -> str | None:
        asked.append(name)
        return None

    env = {"TOKEN_SPAREN_HOME": str(tmp_path / "missing"), "TOKEN_SPAREN_NO_UV": "1"}
    assert pyfind.find_python(env, (3, 9), which=which, platform="linux") is None
    assert asked == [f"python3.{m}" for m in range(15, 10, -1)] + ["python3"]
    asked.clear()
    seen: list[list[str]] = []

    def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen.append(cmd[:2])
        if cmd[1] == "-3.12":
            return subprocess.CompletedProcess(cmd, 0, "3 12 C:\\Py312\\python.exe\n", "")
        return subprocess.CompletedProcess(cmd, 1, "", "")

    def which_win(name: str) -> str | None:
        asked.append(name)
        return "C:\\py.exe" if name == "py" else None

    found = pyfind.find_python(env, (3, 9), which=which_win, run=run, platform="win32")
    assert found == ("C:\\Py312\\python.exe", "py -3.12")
    assert [c[1] for c in seen] == ["-3.15", "-3.14", "-3.13", "-3.12"]


@posix_only
def test_highest_version_wins_and_env_override_first(pyfind: ModuleType, tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    _fake_python(bindir, "python3.12", (3, 12))
    best = _fake_python(bindir, "python3.14", (3, 14))
    env = _env(tmp_path, bindir)
    assert pyfind.find_python(env, (3, 9), use_cache=False) == (str(best), "python3.14")
    pinned = _fake_python(tmp_path / "opt", "python", (3, 11))
    env["TOKEN_SPAREN_PYTHON"] = str(pinned)
    assert pyfind.find_python(env, (3, 9), use_cache=False) == (
        str(pinned),
        "TOKEN_SPAREN_PYTHON",
    )


@posix_only
def test_uv_find_never_downloads(pyfind: ModuleType, tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    managed = _fake_python(tmp_path / "uvpy", "python3.12", (3, 12))
    log = tmp_path / "uv.log"
    uv = bindir / "uv"
    bindir.mkdir()
    uv.write_text(
        f'#!/bin/sh\necho "$UV_PYTHON_DOWNLOADS|$*" >> "{log}"\necho "{managed}"\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)
    env = _env(tmp_path, bindir)
    assert pyfind.find_python(env, (3, 9), use_cache=False) is None  # disabled by the user
    assert not log.exists()
    del env["TOKEN_SPAREN_NO_UV"]
    assert pyfind.find_python(env, (3, 9), use_cache=False) == (str(managed), "uv python find")
    assert log.read_text(encoding="utf-8").splitlines() == ["never|python find >=3.11"]


@posix_only
def test_candidate_probe_rejects_old_python(pyfind: ModuleType, tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    _fake_python(bindir, "python3.11", (3, 10))  # misnamed: the probe decides, not the name
    _fake_python(bindir, "python3", (3, 8))
    env = _env(
        tmp_path, bindir, TOKEN_SPAREN_PYTHON=str(_fake_python(tmp_path / "o", "py", (3, 9)))
    )
    assert pyfind.find_python(env, (3, 9), use_cache=False) is None
    broken = bindir / "python3.13"
    broken.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    broken.chmod(0o755)
    assert pyfind.find_python(env, (3, 9), use_cache=False) is None
    good = _fake_python(bindir, "python3", (3, 12))
    assert pyfind.find_python(env, (3, 9), use_cache=False) == (str(good), "python3")


@posix_only
def test_env_override_inside_project_is_ignored(
    pyfind: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = tmp_path / "proj"
    evil = _fake_python(proj / "tools", "python", (3, 12))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    env = _env(tmp_path, bindir, TOKEN_SPAREN_PYTHON=str(evil), CLAUDE_PROJECT_DIR=str(proj))
    monkeypatch.chdir(tmp_path)
    assert pyfind.find_python(env, (3, 9), use_cache=False) is None


@posix_only
def test_cache_invalidated_when_python3_changes(pyfind: ModuleType, tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    log = tmp_path / "probes.log"
    current = _fake_python(bindir, "python3", (3, 9))
    newer = _fake_python(bindir, "python3.12", (3, 12), log=log)
    env = _env(tmp_path, bindir)

    def probes() -> int:
        return len(log.read_text(encoding="utf-8").splitlines()) if log.exists() else 0

    def find() -> Any:
        return pyfind.find_python(env, (3, 9), self_exe=str(current), root=str(ROOT))

    assert find() == (str(newer), "python3.12") and probes() == 1
    assert (tmp_path / "home" / "launcher.json").is_file()
    assert find() == (str(newer), "python3.12") and probes() == 1  # served from the cache
    st = current.stat()
    os.utime(current, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000_000))  # python3 replaced
    assert find() == (str(newer), "python3.12") and probes() == 2
    newer.unlink()  # the cached interpreter disappeared
    assert find() is None
    assert probes() == 2
    other = _fake_python(bindir, "python3.13", (3, 13))
    assert find() == (str(other), "python3.13")


@posix_only
def test_cache_not_used_when_state_root_missing(pyfind: ModuleType, tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    _fake_python(bindir, "python3.12", (3, 12))
    env = {
        "PATH": str(bindir),
        "TOKEN_SPAREN_HOME": str(tmp_path / "nohome"),
        "TOKEN_SPAREN_NO_UV": "1",
    }
    assert pyfind.find_python(env, (3, 9)) is not None
    assert not (tmp_path / "nohome").exists()


# --- run.py -----------------------------------------------------------------------------------


@posix_only
def test_no_candidate_logs_once_and_prints_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runpy_mod: ModuleType
) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    env = _env(tmp_path, bindir)
    monkeypatch.setattr(os, "environ", dict(env))
    outs = []
    for _ in range(3):
        out = io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        assert runpy_mod._hop(["run.py", "PostToolUse"], os.environ, (3, 9)) == 0
        outs.append(json.loads(out.getvalue()))
    log = tmp_path / "home" / "logs" / "errors.jsonl"
    lines = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1
    assert lines[0]["kind"] == "launcher" and lines[0]["error"] == "no python >= 3.11"
    assert isinstance(lines[0]["ts"], float)
    # one clear warning, then the plain no-op
    assert "Python >= 3.11" in outs[0]["systemMessage"] and "doctor" in outs[0]["systemMessage"]
    assert outs[1:] == [{}, {}]


def test_reexec_flag_not_leaked(runpy_mod: ModuleType) -> None:
    env = {"TOKEN_SPAREN_REEXEC": "1"}
    assert runpy_mod._hop(["run.py", "PostToolUse"], env, (3, 12)) is None
    assert "TOKEN_SPAREN_REEXEC" not in env


def test_never_loops(
    runpy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def execv(*a: object) -> None:
        raise AssertionError("second hop")

    monkeypatch.setattr(os, "execv", execv)
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    env = {"TOKEN_SPAREN_REEXEC": "1", "TOKEN_SPAREN_HOME": str(tmp_path)}
    assert runpy_mod._hop(["run.py", "PostToolUse"], env, (3, 9)) == 0
    assert out.getvalue() == "{}\n" and "TOKEN_SPAREN_REEXEC" not in env


def test_windows_spawn_passthrough_exit_code(
    pyfind: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        seen.update(cmd=cmd, **kw)
        return subprocess.CompletedProcess(cmd, 3)

    def execv(*a: object) -> None:
        raise AssertionError("no execv on Windows")

    monkeypatch.setattr(os, "execv", execv)
    monkeypatch.delenv("TOKEN_SPAREN_REEXEC", raising=False)
    rc = pyfind.relaunch("C:\\Py\\python.exe", ["-I", "run.py", "Stop"], platform="win32", run=run)
    assert rc == 3
    assert seen["cmd"] == ["C:\\Py\\python.exe", "-I", "run.py", "Stop"]
    assert seen["env"]["TOKEN_SPAREN_REEXEC"] == "1"
    assert seen.get("stdin") is None and seen.get("stdout") is None  # inherited stdio
    assert "TOKEN_SPAREN_REEXEC" not in os.environ


def test_launcher_sources_parse_as_py36() -> None:
    for name in ("_pyfind.py", "run.py", "run_cli.py"):
        src = (ROOT / "hooks" / name).read_text(encoding="utf-8")
        ast.parse(src, filename=name, feature_version=(3, 6))
    sh = (ROOT / "token_sparen" / "assets" / "statusline.sh").read_text(encoding="utf-8")
    boot = sh.split("BOOT='", 1)[1].split("\n'\n", 1)[0]
    ast.parse(boot, filename="statusline BOOT", feature_version=(3, 6))


def test_fast_path_does_not_import_pyfind(runpy_mod: ModuleType) -> None:
    before = set(sys.modules)
    assert runpy_mod._hop(["run.py", "PostToolUse"], {}, (3, 12)) is None
    assert not any("pyfind" in m for m in set(sys.modules) - before)


def test_force_flag_needs_state_home(runpy_mod: ModuleType) -> None:
    env = {"TOKEN_SPAREN_LAUNCHER_FORCE_REEXEC": "1"}
    assert runpy_mod._hop(["run.py", "PostToolUse"], env, (3, 12)) is None


# --- end to end through real subprocesses (forced re-exec onto a wrapper of this interpreter) --


def _wrapper(tmp_path: Path, log: Path) -> Path:
    """A ``python3.12``-named wrapper that records its argv and runs the current interpreter."""
    path = tmp_path / "wrap" / "python3.12"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'#!/bin/sh\necho "$*" >> "{log}"\nexec "{sys.executable}" "$@"\n', encoding="utf-8"
    )
    path.chmod(0o755)
    return path


def _hook_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TOKEN_SPAREN_", "CLAUDE_"))}
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    env.update(
        TOKEN_SPAREN_HOME=str(tmp_path / "home"),
        CLAUDE_PROJECT_DIR=str(proj),
        TOKEN_SPAREN_ADAPTER__LATENCY_BUDGET_MS="5000",
        TOKEN_SPAREN_COMPRESSION__SAVE_DEADLINE_MS="5000",  # slow CI runners, like the budget
        TOKEN_SPAREN_LEARN__ENABLED="false",
        TOKEN_SPAREN_NO_UV="1",
    )
    env.update(extra)
    # widening settings belong in the user's config.toml (env may only narrow)
    return to_user_config(env)


def _errors_log(home: Path) -> str:
    """The hook's own account of a no-op, for an assertion message."""
    path = home / "logs" / "errors.jsonl"
    return "\nerrors.jsonl:\n" + (path.read_text(encoding="utf-8") if path.is_file() else "-")


def _post_payload(proj: Path) -> str:
    return json.dumps(
        {
            "session_id": "launcher",
            "cwd": str(proj),
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest -v"},
            "tool_use_id": "tu-1",
            "tool_response": {
                "stdout": big_pytest(50_000),
                "stderr": "",
                "interrupted": False,
                "isImage": False,
            },
        }
    )


@posix_only
def test_forced_reexec_runs_the_hook_end_to_end(tmp_path: Path) -> None:
    log = tmp_path / "wrap.log"
    wrap = _wrapper(tmp_path, log)
    env = _hook_env(tmp_path, TOKEN_SPAREN_LAUNCHER_FORCE_REEXEC="1", TOKEN_SPAREN_PYTHON=str(wrap))
    proc = subprocess.run(
        [sys.executable, "-I", str(ROOT / "hooks" / "run.py"), "PostToolUse"],
        input=_post_payload(tmp_path / "proj"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path / "proj",
        env=env,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "[token-sparen: kept " in proc.stdout, (
        proc.stdout[:300] + proc.stderr + _errors_log(tmp_path / "home")
    )
    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls[-1] == f"-I -S {ROOT / 'hooks' / 'run.py'} PostToolUse"


@posix_only
def test_run_cli_relaunches(tmp_path: Path) -> None:
    log = tmp_path / "wrap.log"
    wrap = _wrapper(tmp_path, log)
    env = _hook_env(tmp_path, TOKEN_SPAREN_LAUNCHER_FORCE_REEXEC="1", TOKEN_SPAREN_PYTHON=str(wrap))
    proc = subprocess.run(
        [sys.executable, "-I", str(ROOT / "hooks" / "run_cli.py"), "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path / "proj",
        env=env,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "doctor" in proc.stdout
    assert log.read_text(encoding="utf-8").splitlines()[-1].endswith("run_cli.py --help")


def test_run_cli_without_python_explains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load("ts_test_run_cli", ROOT / "hooks" / "run_cli.py")
    env = {"PATH": str(tmp_path), "TOKEN_SPAREN_HOME": str(tmp_path), "TOKEN_SPAREN_NO_UV": "1"}
    assert mod._hop(["run_cli.py", "stats"], env, (3, 9)) == 1
    err = capsys.readouterr().err
    assert "Python >= 3.11" in err and "TOKEN_SPAREN_PYTHON" in err


@posix_only
@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
def test_statusline_launcher_relaunches(tmp_path: Path) -> None:
    log = tmp_path / "wrap.log"
    wrap = _wrapper(tmp_path, log)
    env = _hook_env(
        tmp_path,
        TOKEN_SPAREN_LAUNCHER_FORCE_REEXEC="1",
        TOKEN_SPAREN_PYTHON=str(wrap),
        TOKEN_SPAREN_ROOT=str(ROOT),
        NO_COLOR="1",
    )
    proc = subprocess.run(
        [
            "sh",
            str(ROOT / "token_sparen" / "assets" / "statusline.sh"),
            "--compose",
            "--",
            "echo mine",
        ],
        input="{}",
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path / "proj",
        env=env,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("mine"), proc.stdout + proc.stderr
    assert "-I -S -c" in log.read_text(encoding="utf-8")


def _evil_pythonpath(tmp_path: Path) -> tuple[Path, Path]:
    """A PYTHONPATH dir (as a cloned repo's committed settings env could set) whose ``json`` and
    ``token_sparen`` modules leave a marker when imported."""
    evil = tmp_path / "evil"
    marker = tmp_path / "pwned"
    (evil / "token_sparen").mkdir(parents=True)
    code = f"open({str(marker)!r}, 'a').write(__name__ + '\\n')\n"
    (evil / "json.py").write_text(code, encoding="utf-8")
    (evil / "token_sparen" / "__init__.py").write_text(code, encoding="utf-8")
    return evil, marker


@posix_only
@pytest.mark.parametrize("forced", [False, True])
def test_pythonpath_cannot_inject_modules(tmp_path: Path, forced: bool) -> None:
    """hooks.json starts ``python3 -I``; the probe and the re-exec use ``-I`` too, so PYTHONPATH
    and the user site dir never reach the hook (direct and re-exec paths)."""
    evil, marker = _evil_pythonpath(tmp_path)
    log = tmp_path / "wrap.log"
    extra = {"PYTHONPATH": str(evil), "PYTHONSTARTUP": str(evil / "json.py")}
    if forced:
        extra.update(
            TOKEN_SPAREN_LAUNCHER_FORCE_REEXEC="1", TOKEN_SPAREN_PYTHON=str(_wrapper(tmp_path, log))
        )
    env = _hook_env(tmp_path, **extra)
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    for groups in hooks.values():
        command = groups[0]["hooks"][0]["command"]
        for name in ("python3", "py.exe -3", "python.exe"):  # each starts isolated
            assert f"{name} -I " in command, (name, command)
    proc = subprocess.run(
        [sys.executable, "-I", str(ROOT / "hooks" / "run.py"), "PostToolUse"],
        input=_post_payload(tmp_path / "proj"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path / "proj",
        env=env,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "[token-sparen: kept " in proc.stdout, (
        proc.stdout[:300] + proc.stderr + _errors_log(tmp_path / "home")
    )
    assert not marker.exists(), marker.read_text(encoding="utf-8")
    if forced:
        calls = log.read_text(encoding="utf-8").splitlines()
        assert calls and all(c.startswith("-I ") for c in calls)  # probe and re-exec


def _fake(bindir: Path, name: str, log: Path, *, drop: str | None = None, rc: int = 0) -> None:
    """A fake interpreter on PATH that logs its name; it runs the current Python (dropping one
    ``drop`` argument, as ``py`` consumes ``-3``), or exits ``rc`` like the Store alias stub."""
    body = f'echo "{name} $*" >> "{log}"\n'
    if rc:
        body += f"exit {rc}\n"
    else:
        shift = f'[ "$1" = "{drop}" ] && shift\n' if drop else ""
        body += f'{shift}exec "{sys.executable}" "$@"\n'
    path = bindir / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _fake_tool(bindir: Path, name: str, out: str) -> None:
    path = bindir / name
    path.write_text(f"#!/bin/sh\necho '{out}'\n", encoding="utf-8")
    path.chmod(0o755)


def _run_hook_command(tmp_path: Path, path: str, event: str = "PostToolUse") -> Any:
    env = _hook_env(tmp_path, PATH=path)
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    command = hooks[event][0]["hooks"][0]["command"]
    return subprocess.run(
        ["/bin/sh", "-c", command.replace("${CLAUDE_PLUGIN_ROOT}", ROOT.as_posix())],
        input=_post_payload(tmp_path / "proj"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path / "proj",
        env=env,
        timeout=60,
        check=False,
    )


# shell: "posix" (no cygpath), "wsl" (Windows PATH appended: py.exe and the python.exe stub are
# there, uname -r names microsoft; a cygpath decoy too), "gitbash" (cygpath, MINGW kernel)
@posix_only
@pytest.mark.parametrize(
    ("shell", "present", "used"),
    [
        ("posix", ("python3", "py", "python"), "python3"),  # a POSIX ``py`` may be another tool
        ("posix", ("python3", "py.exe", "python.exe"), "python3"),  # .exe never on POSIX
        ("wsl", ("python3", "py.exe", "python.exe", "cygpath"), "python3"),
        ("gitbash", ("python3", "py.exe", "python.exe", "cygpath"), "py.exe"),  # python3: stub
        ("gitbash", ("python3", "python.exe", "cygpath"), "python.exe"),
    ],
)
def test_hook_command_resolves_the_interpreter_per_platform(
    tmp_path: Path, shell: str, present: tuple[str, ...], used: str
) -> None:
    """v1 review M9 and C1: in a Windows shell the hooks start ``py.exe -3`` or ``python.exe``,
    never a bare ``python3`` (often missing or the Store alias stub); every POSIX shell, WSL
    included, starts ``python3`` and never a Windows ``.exe`` (it cannot open a Linux path and
    exits 2, which would block every tool call)."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    log = tmp_path / "interp.log"
    for name in present:
        if name == "cygpath":
            _fake_tool(bindir, name, "C:\\")
            continue
        stub = name == "python3" and shell == "gitbash"
        windows_only = name.endswith(".exe") and shell != "gitbash"  # exit 2 like the real one
        decoy = name == "py"  # e.g. an unrelated ``py`` tool: must not run
        rc = 9009 if stub else 2 if (decoy or windows_only) else 0
        _fake(bindir, name, log, drop="-3" if name == "py.exe" else None, rc=rc)
    kernel = {"wsl": "5.15.153.1-microsoft-standard-WSL2", "gitbash": "3.5.4-0bc1222b.x86_64"}
    if shell in kernel:
        _fake_tool(bindir, "uname", kernel[shell])
    proc = _run_hook_command(tmp_path, os.pathsep.join([str(bindir), "/usr/bin", "/bin"]))
    assert proc.returncode == 0, proc.stderr
    assert "[token-sparen: kept " in proc.stdout, (
        proc.stdout[:300] + proc.stderr + _errors_log(tmp_path / "home")
    )
    if shell == "posix":  # a normal hook run on a POSIX shell prints nothing to stderr
        assert proc.stderr == "", proc.stderr
    assert "not found" not in proc.stderr, proc.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls and calls[0].split()[0] == used, calls
    assert all(c.split()[0] == used for c in calls), calls  # one interpreter, no stub call
    assert " -I " in f" {calls[0]} "


@posix_only
@pytest.mark.parametrize("case", ["failing", "missing", "wsl-exe-only"])
def test_hook_command_fails_open(tmp_path: Path, case: str) -> None:
    """v1 review C1: whatever goes wrong before the hook can answer (the interpreter fails or is
    missing, only Windows interpreters exist under WSL), the command prints the no-op ``{}`` and
    exits 0, so the host never blocks a prompt or tool call because of token-sparen."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    log = tmp_path / "interp.log"
    if case == "failing":
        _fake(bindir, "python3", log, rc=1)
    if case == "wsl-exe-only":
        _fake(bindir, "py.exe", log, rc=2)
        _fake(bindir, "python.exe", log, rc=2)
        _fake_tool(bindir, "uname", "5.15.153.1-microsoft-standard-WSL2")
    for event in ("PreToolUse", "UserPromptSubmit", "PostToolUse"):
        proc = _run_hook_command(tmp_path, str(bindir), event)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "{}", proc.stdout
    if case == "wsl-exe-only":
        assert not log.exists() or "exe" not in log.read_text(encoding="utf-8")


# --- TOKEN_SPAREN_PYTHON safety (a committed settings env can set it) ----------------------------


@posix_only
def test_env_python_writable_by_others_is_rejected_and_logged_once(
    pyfind: ModuleType, tmp_path: Path
) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shared = _fake_python(tmp_path / "shared", "python", (3, 12))
    env = _env(tmp_path, bindir, TOKEN_SPAREN_PYTHON=str(shared))
    for target in (shared, shared.parent):
        target.chmod(0o777)
        assert pyfind.find_python(env, (3, 9), use_cache=False) is None
        assert pyfind.find_python(env, (3, 9), use_cache=False) is None
        target.chmod(0o755)
    log = tmp_path / "home" / "logs" / "errors.jsonl"
    lines = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1 and "TOKEN_SPAREN_PYTHON ignored" in lines[0]["error"]
    assert pyfind.find_python(env, (3, 9), use_cache=False) == (str(shared), "TOKEN_SPAREN_PYTHON")


@posix_only
def test_env_python_rechecked_on_cache_hit(
    pyfind: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = tmp_path / "projX"
    inside = _fake_python(proj, "py", (3, 12))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    env = _env(tmp_path, bindir, TOKEN_SPAREN_PYTHON=str(inside))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # prime the cache from outside the project
    assert pyfind.find_python(env, (3, 9), root=str(ROOT)) == (str(inside), "TOKEN_SPAREN_PYTHON")
    monkeypatch.chdir(proj)  # same cache key, now inside: the cached choice must not be served
    assert pyfind.find_python(env, (3, 9), root=str(ROOT)) is None
    monkeypatch.chdir(elsewhere)
    assert pyfind.find_python(env, (3, 9), root=str(ROOT)) is not None
    inside.chmod(0o777)  # became writable by others after it was cached
    assert pyfind.find_python(env, (3, 9), root=str(ROOT)) is None


def test_force_flag_needs_an_accepted_state_home(
    runpy_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = tmp_path / "proj"
    (proj / "inside").mkdir(parents=True)
    monkeypatch.chdir(proj)

    def execv(*a: object) -> None:
        raise AssertionError("forced re-exec with a rejected TOKEN_SPAREN_HOME")

    monkeypatch.setattr(os, "execv", execv)
    env = {
        "TOKEN_SPAREN_LAUNCHER_FORCE_REEXEC": "1",
        "TOKEN_SPAREN_HOME": str(proj / "inside"),  # inside the project: state_root rejects it
        "CLAUDE_PROJECT_DIR": str(proj),
        "TOKEN_SPAREN_PYTHON": sys.executable,
        "PATH": "",
        "TOKEN_SPAREN_NO_UV": "1",
    }
    assert runpy_mod._hop(["run.py", "PostToolUse"], env, (3, 12)) is None
    alone = {"TOKEN_SPAREN_HOME": str(tmp_path / "home"), "TOKEN_SPAREN_PYTHON": sys.executable}
    assert runpy_mod._hop(["run.py", "PostToolUse"], alone, (3, 12)) is None


@posix_only
def test_launcher_cache_is_private(pyfind: ModuleType, tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    _fake_python(bindir, "python3.12", (3, 12))
    env = _env(tmp_path, bindir)
    assert pyfind.find_python(env, (3, 9)) is not None
    assert (tmp_path / "home" / "launcher.json").stat().st_mode & 0o777 == 0o600


def test_search_has_an_overall_deadline(pyfind: ModuleType, tmp_path: Path) -> None:
    timeouts: list[float] = []

    def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        timeouts.append(kw["timeout"])
        time.sleep(0.3)
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    def which(name: str) -> str | None:
        return f"/fake/{name}"

    env = {"TOKEN_SPAREN_HOME": str(tmp_path / "none"), "TOKEN_SPAREN_NO_UV": "1"}
    assert pyfind.search(env, (3, 9), which, run, "linux", "/fake/self", deadline=1.0) is None
    assert 1 <= len(timeouts) <= 5 and all(t <= 1.0 for t in timeouts)
    assert pyfind.SEARCH_DEADLINE <= 2.0


# --- a no-op the launcher writes itself is never silent (errors.jsonl, shown by doctor) ----------


def test_broken_install_noop_is_logged_once_a_day(tmp_path: Path) -> None:
    """``hooks/run.py`` in a plugin tree whose package cannot be imported answers ``{}`` and
    leaves one ``errors.jsonl`` line per reason and day (never a silent ``{}``)."""
    plugin = tmp_path / "plugin"
    shutil.copytree(ROOT / "hooks", plugin / "hooks")
    (plugin / "token_sparen").mkdir()
    (plugin / "token_sparen" / "__init__.py").write_text("raise ImportError('x')\n", "utf-8")
    env = _hook_env(tmp_path)
    for _ in range(2):
        proc = subprocess.run(
            [sys.executable, "-I", str(plugin / "hooks" / "run.py"), "PostToolUse"],
            input=_post_payload(tmp_path / "proj"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=60,
            check=False,
        )
        assert proc.returncode == 0 and proc.stdout.strip() == "{}", proc.stdout + proc.stderr
    log = tmp_path / "home" / "logs" / "errors.jsonl"
    lines = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1, lines
    assert lines[0]["where"] == "launcher"
    assert lines[0]["error"].startswith("PostToolUse: cannot import token_sparen")


def test_log_fallback_rotates_its_day_markers(pyfind: ModuleType, tmp_path: Path) -> None:
    env = {"TOKEN_SPAREN_HOME": str(tmp_path / "home")}
    t0 = time.mktime((2026, 11, 2, 12, 0, 0, 0, 0, -1))
    pyfind.log_fallback(env, "a", now=t0)
    pyfind.log_fallback(env, "a", now=t0)
    pyfind.log_fallback(env, "b", now=t0)
    pyfind.log_fallback(env, "a", now=t0 + 86400.0)
    logs = tmp_path / "home" / "logs"
    text = (logs / "errors.jsonl").read_text(encoding="utf-8")
    assert [json.loads(x)["error"] for x in text.splitlines()] == ["a", "b", "a"]
    assert len(list(logs.glob("launcher-fallback-*"))) == 1  # yesterday's markers are gone
