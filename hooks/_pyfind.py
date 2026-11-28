"""Find a Python >= 3.11 when the ``python3`` that started a launcher is older.

Used by ``hooks/run.py``, ``hooks/run_cli.py``, the statusline launcher and ``token-sparen
doctor``. It runs on whatever ``python3`` the host found first (macOS ships 3.9), so it must parse
and run on Python 3.6: stdlib only, no annotations, no ``subprocess`` keywords newer than 3.6.

Search order (first candidate whose own ``sys.version_info >= (3, 11)`` wins):
``$TOKEN_SPAREN_PYTHON`` -> ``python3.15`` ... ``python3.11`` -> ``python3`` -> (Windows)
``py -3.15`` ... ``py -3.11``, ``py -3`` -> ``uv python find ">=3.11"`` (never downloads; skipped
when ``TOKEN_SPAREN_NO_UV`` is set). The result is cached in ``<state root>/launcher.json``.
"""

import hashlib
import json
import os
import stat
import sys
import time

MIN_VERSION = (3, 11)
NEWEST_MINOR = 15
REEXEC_FLAG = "TOKEN_SPAREN_REEXEC"
FORCE_FLAG = "TOKEN_SPAREN_LAUNCHER_FORCE_REEXEC"
CACHE_FILE = "launcher.json"
CACHE_ENTRIES = 32
NEGATIVE_TTL = 600.0  # seconds a "nothing found" result is trusted before searching again
PROBE_TIMEOUT = 5.0
SEARCH_DEADLINE = 2.0  # seconds for the whole search (probes and uv), well inside the hook timeout
PROBE = "import sys; v = sys.version_info; print(v[0], v[1], sys.executable)"
MISSING = "no python >= 3.11"


def _truthy(value):
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def forced(env):
    """Test seed: re-exec even on a new interpreter. Honoured only together with a
    ``TOKEN_SPAREN_HOME`` that ``state_root`` accepts (outside the project, ours)."""
    raw = env.get("TOKEN_SPAREN_HOME")
    if not _truthy(env.get(FORCE_FLAG)) or not raw:
        return False
    try:
        return state_root(env) == os.path.abspath(raw)
    except Exception:  # noqa: BLE001
        return False


# --- path safety (same rule as token_sparen.config.env_path) -----------------------------------


def _ident(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _chain(path):
    out = set()
    cur = path
    while True:
        ident = _ident(cur)
        if ident is not None:
            out.add(ident)
        parent = os.path.dirname(cur)
        if parent == cur:
            return out
        cur = parent


def _work_tree(start):
    cur = start
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _inside_project(env, path):
    """``path`` lies inside the hook's project dir, the cwd or their git work trees (compared by
    file identity; a directory that contains the home dir is no project)."""
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = ""
    target = os.path.realpath(os.path.join(cwd, path) if cwd else path)
    inside = _chain(target)
    home = _chain(os.path.realpath(os.path.expanduser("~")))
    for raw in (env.get("CLAUDE_PROJECT_DIR") or "", cwd):
        if not raw:
            continue
        base = os.path.realpath(raw)
        for root in (base, _work_tree(base)):
            ident = None if root is None else _ident(root)
            if ident is not None and ident not in home and ident in inside:
                return True
    return False


def _owned(path):
    """POSIX: ``path`` (or, if missing, its nearest existing parent) is ours, or that parent is a
    root-owned sticky directory such as /tmp."""
    getuid = getattr(os, "getuid", None)
    if os.name != "posix" or getuid is None:
        return True
    cur = path
    while not os.path.lexists(cur):
        parent = os.path.dirname(cur)
        if parent == cur:
            return False
        cur = parent
    try:
        st = os.stat(cur)
    except OSError:
        return False
    if st.st_uid == getuid():
        return True
    return cur != path and st.st_uid == 0 and bool(st.st_mode & stat.S_ISVTX)


def state_root(env):
    """``TOKEN_SPAREN_HOME`` when safe to honour, else ``~/.token-sparen``."""
    raw = env.get("TOKEN_SPAREN_HOME")
    if raw:
        try:
            path = os.path.abspath(raw)
            if _owned(path) and not _inside_project(env, path):
                return path
        except (OSError, ValueError):
            pass
    return os.path.join(os.path.expanduser("~"), ".token-sparen")


def _safe_file(path):
    """POSIX: owned by us or root and not group/world-writable."""
    getuid = getattr(os, "getuid", None)
    if os.name != "posix" or getuid is None:
        return True
    st = os.stat(path)
    return st.st_uid in (getuid(), 0) and not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)


def trusted_interpreter(env, path):
    """``TOKEN_SPAREN_PYTHON`` may be honoured: it resolves outside the current project and its
    git work tree, and (POSIX) the interpreter file and its directory are owned by the user or
    root and not group- or world-writable (a repository's committed settings env can set it)."""
    try:
        real = os.path.realpath(path)
        if _inside_project(env, path) or _inside_project(env, real):
            return False
        return _safe_file(real) and _safe_file(os.path.dirname(real))
    except (OSError, ValueError):
        return False


def _env_python(env, which):
    raw = env.get("TOKEN_SPAREN_PYTHON")
    if not raw:
        return None
    return raw if os.path.dirname(raw) else which(raw)


def _log_rejected(env, raw):
    """One ``errors.jsonl`` line per rejected ``TOKEN_SPAREN_PYTHON`` value (a short hash of it
    in ``launcher.json``); best effort."""
    try:
        home = state_root(env)
        if not _cache_usable(home):
            return
        shown = hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()[:16]
        data = _read_cache(home)
        if data.get("rejected_python") == shown:
            return
        data["rejected_python"] = shown
        _write_cache(home, data)
        os.makedirs(os.path.join(home, "logs"), mode=0o700, exist_ok=True)
        rec = {
            "ts": time.time(),
            "kind": "launcher",
            "where": "launcher",
            "error": "TOKEN_SPAREN_PYTHON ignored (inside the current project, or the "
            "interpreter or its directory is writable by others or not owned by you or root); "
            "set it outside any repository, e.g. in your shell profile",
        }
        with open(os.path.join(home, "logs", "errors.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:  # noqa: BLE001
        return


# --- probing ----------------------------------------------------------------------------------


def probe(cmd, run=None, timeout=PROBE_TIMEOUT):
    """``(major, minor, executable)`` of the interpreter ``cmd`` (a list), or None."""
    import subprocess  # lazy: the cached path never needs it

    run = subprocess.run if run is None else run
    if timeout <= 0:
        return None
    try:
        proc = run(
            list(cmd) + ["-I", "-c", PROBE],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            universal_newlines=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            return None
        major, minor, exe = proc.stdout.strip().split(" ", 2)
        return int(major), int(minor), exe
    except Exception:  # noqa: BLE001 - any odd candidate is simply not a candidate
        return None


def _real(path):
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return path


def _stamp(path):
    try:
        st = os.stat(path)
    except (OSError, TypeError, ValueError):
        return None
    return [_real(path), st.st_mtime_ns]


def _uv_python(env, which, run, timeout=PROBE_TIMEOUT):
    import subprocess

    run = subprocess.run if run is None else run
    uv = which("uv")
    if not uv or timeout <= 0:
        return None
    child = dict(env)
    child["UV_PYTHON_DOWNLOADS"] = "never"
    child.pop("VIRTUAL_ENV", None)
    try:
        proc = run(
            [uv, "python", "find", ">=3.11"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            universal_newlines=True,
            timeout=timeout,
            env=child,
            cwd=os.path.expanduser("~"),  # no project discovery: a repo's .venv is not ours
        )
    except Exception:  # noqa: BLE001
        return None
    lines = proc.stdout.strip().splitlines() if proc.returncode == 0 else []
    return lines[-1].strip() if lines else None


def _candidates(env, which, run, platform, left):
    """``(cmd, how)`` pairs in search order; lazy so the search stops at the first hit.
    ``left()`` is the time left before the search deadline."""
    path = _env_python(env, which)
    if path:
        if trusted_interpreter(env, path):
            yield [path], "TOKEN_SPAREN_PYTHON"
        else:
            _log_rejected(env, env.get("TOKEN_SPAREN_PYTHON") or "")
    for minor in range(NEWEST_MINOR, MIN_VERSION[1] - 1, -1):
        name = f"python3.{minor}"
        path = which(name)
        if path:
            yield [path], name
    path = which("python3")
    if path:
        yield [path], "python3"
    if platform == "win32":
        launcher = which("py")
        if launcher:
            for minor in range(NEWEST_MINOR, MIN_VERSION[1] - 1, -1):
                yield [launcher, f"-3.{minor}"], f"py -3.{minor}"
            yield [launcher, "-3"], "py -3"
    if not _truthy(env.get("TOKEN_SPAREN_NO_UV")):
        path = _uv_python(env, which, run, min(PROBE_TIMEOUT, left()))
        if path:
            yield [path], "uv python find"


def search(env, current_version, which, run, platform, self_exe, deadline=SEARCH_DEADLINE):
    own = _real(self_exe) if self_exe else None
    tried = set()
    end = time.monotonic() + deadline

    def left():
        return end - time.monotonic()

    for cmd, how in _candidates(env, which, run, platform, left):
        if left() <= 0:
            return None
        if len(cmd) == 1:
            real = _real(cmd[0])
            if real in tried or (real == own and tuple(current_version[:2]) < MIN_VERSION):
                continue
            tried.add(real)
        got = probe(cmd, run, min(PROBE_TIMEOUT, left()))
        if got is None or (got[0], got[1]) < MIN_VERSION:
            continue
        # a single path is used as given (a venv's python must keep its venv); a ``py`` launcher
        # selection resolves to the interpreter it picked
        return (cmd[0] if len(cmd) == 1 else got[2]), how
    return None


# --- cache --------------------------------------------------------------------------------------


def _read_cache(home):
    try:
        with open(os.path.join(home, CACHE_FILE), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_cache(home, data):
    path = os.path.join(home, CACHE_FILE)
    tmp = f"{path}.tmp-{os.getpid()}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with open(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _cache_key(env, root, self_exe, platform):
    parts = [
        _real(root) if root else "",
        _stamp(self_exe) if self_exe else None,
        env.get("TOKEN_SPAREN_PYTHON") or "",
        env.get("PATH") or "",
        _truthy(env.get("TOKEN_SPAREN_NO_UV")),
        platform,
    ]
    if env.get("TOKEN_SPAREN_PYTHON"):
        # whether it may be honoured depends on the project (inside or outside it)
        try:
            cwd = os.getcwd()
        except OSError:
            cwd = ""
        parts += [env.get("CLAUDE_PROJECT_DIR") or "", cwd]
    blob = json.dumps(parts, sort_keys=True).encode("utf-8", "surrogatepass")
    return hashlib.sha256(blob).hexdigest()[:24]


def _fingerprint(env, which, platform):
    """What a fresh search would look at, without probing: a "nothing found" result is reused
    only while no named candidate appeared, changed or vanished."""
    names = [f"python3.{m}" for m in range(MIN_VERSION[1], NEWEST_MINOR + 1)]
    names += ["python3"] + (["py"] if platform == "win32" else [])
    raw = env.get("TOKEN_SPAREN_PYTHON")
    seen = [_stamp(raw if raw and os.path.dirname(raw) else (which(raw) if raw else None))]
    for name in names:
        path = which(name)
        seen.append(_stamp(path) if path else None)
    return hashlib.sha256(json.dumps(seen).encode("utf-8", "surrogatepass")).hexdigest()[:24]


def _cache_usable(home):
    return os.path.isdir(home) and _owned(home)


def find_python(
    env,
    current_version,
    which=None,
    run=None,
    platform=None,
    self_exe=None,
    root=None,
    use_cache=True,
    now=None,
):
    """``(path, how)`` of a Python >= 3.11 other than an older current one, or None."""
    platform = sys.platform if platform is None else platform
    self_exe = sys.executable if self_exe is None else self_exe
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) if root is None else root
    now = time.time() if now is None else now
    if which is None:
        search_path = env.get("PATH")

        def which(name):
            import shutil  # lazy: the cached path never needs it

            return shutil.which(name, path=search_path)

    home = state_root(env) if use_cache else ""
    cached = use_cache and _cache_usable(home)
    key = _cache_key(env, root, self_exe, platform) if cached else ""
    data = _read_cache(home) if cached else {}
    entries = data.get("entries") if isinstance(data.get("entries"), dict) else {}
    hit = entries.get(key) if cached else None
    if isinstance(hit, dict):
        if (
            hit.get("none")
            and now - float(hit.get("ts", 0)) < NEGATIVE_TTL
            and hit.get("seen") == _fingerprint(env, which, platform)
        ):
            return None
        if (
            hit.get("path")
            and hit.get("stamp") == _stamp(hit["path"])
            and (hit.get("how") != "TOKEN_SPAREN_PYTHON" or trusted_interpreter(env, hit["path"]))
        ):
            return hit["path"], hit.get("how", "cache")
    found = search(env, current_version, which, run, platform, self_exe)
    if cached:
        entry = {"none": True, "ts": now, "seen": _fingerprint(env, which, platform)}
        if found is not None:
            entry = {"path": found[0], "how": found[1], "stamp": _stamp(found[0]), "ts": now}
        entries[key] = entry
        if len(entries) > CACHE_ENTRIES:
            for old in sorted(entries, key=lambda k: entries[k].get("ts", 0))[:-CACHE_ENTRIES]:
                del entries[old]
        data["entries"] = entries
        _write_cache(home, data)
    return found


# --- launching and the "nothing found" warning ---------------------------------------------------


def relaunch(path, args, platform=None, execv=None, run=None):
    """Run ``path args...`` in place of this process. POSIX: ``execv`` (never returns unless it
    fails: then None). Windows: a child with inherited stdio; returns its exit code (None if it
    could not start). The child sees ``TOKEN_SPAREN_REEXEC=1`` and refuses a second hop."""
    platform = sys.platform if platform is None else platform
    if platform == "win32":
        child = dict(os.environ)
        child[REEXEC_FLAG] = "1"
        try:
            if run is None:
                import subprocess

                run = subprocess.run
            return run([path] + list(args), env=child).returncode
        except Exception:  # noqa: BLE001
            return None
    os.environ[REEXEC_FLAG] = "1"
    try:
        sys.stdout.flush()
        (execv or os.execv)(path, [path] + list(args))
    except OSError:
        pass
    os.environ.pop(REEXEC_FLAG, None)
    return None


def warning_text(current_version):
    return (
        f"token-sparen is inactive: hooks started with Python {current_version[0]}."
        f"{current_version[1]} and no Python >= 3.11 was found. Install Python 3.11 or newer "
        "(found automatically on PATH), or export TOKEN_SPAREN_PYTHON=/path/to/python3.x in the "
        "shell profile Claude Code is started from (a value set in a project's settings is "
        "ignored), restart Claude Code, then run `token-sparen doctor`."
    )


def log_fallback(env, error, now=None):
    """The launcher answered the no-op ``{}`` without running the hook: one ``errors.jsonl``
    line per reason and day (an O_EXCL marker per reason), so ``doctor`` shows it and the plugin
    never does nothing silently; best effort, never raises."""
    now = time.time() if now is None else now
    try:
        home = state_root(env)
        logs = os.path.join(home, "logs")
        os.makedirs(logs, mode=0o700, exist_ok=True)
        if not _owned(home):
            return
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        tag = hashlib.sha256(error.encode("utf-8", "replace")).hexdigest()[:12]
        marker = os.path.join(logs, "launcher-fallback-" + tag + "-" + day)
        try:
            os.close(os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            return
        for name in os.listdir(logs):
            if name.startswith("launcher-fallback-") and not name.endswith("-" + day):
                try:
                    os.unlink(os.path.join(logs, name))
                except OSError:
                    pass
        rec = {"ts": float(now), "kind": "launcher", "where": "launcher", "error": error}
        with open(os.path.join(logs, "errors.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:  # noqa: BLE001 - fail open
        return


def report_missing(env, current_version, now=None):
    """Log one ``errors.jsonl`` line per day (exactly once: an O_EXCL day marker); returns the
    user-facing warning when this call logged it, else ''."""
    now = time.time() if now is None else now
    home = state_root(env)
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    try:
        logs = os.path.join(home, "logs")
        os.makedirs(logs, mode=0o700, exist_ok=True)
        if not _owned(home):
            return ""
        marker = os.path.join(logs, "launcher-warned-" + day)
        try:
            os.close(os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            return ""
        for name in os.listdir(logs):
            if name.startswith("launcher-warned-") and name != os.path.basename(marker):
                try:
                    os.unlink(os.path.join(logs, name))
                except OSError:
                    pass
        rec = {
            "ts": float(now),
            "kind": "launcher",
            "where": "launcher",
            "error": MISSING,
            "python": f"{current_version[0]}.{current_version[1]}",
        }
        with open(os.path.join(logs, "errors.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:  # noqa: BLE001 - fail open
        return ""
    return warning_text(current_version)
