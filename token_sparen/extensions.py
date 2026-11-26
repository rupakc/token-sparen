"""Extension loading: entry points and trusted drop-in files (spec section 9).

Two sources:

* Entry points (``token_sparen.compressors``, ``.decision_points``, ``.signals``,
  ``.adapters``). Installing a package is the act of trusting it.
* Drop-in ``*.py`` files in ``<project>/.token-sparen/extensions/`` and
  ``~/.token-sparen/extensions/``. A drop-in is imported **only** if the sha256 of its bytes is
  listed in ``~/.token-sparen/trusted_extensions.json`` (``token-sparen extensions trust``).
  Trust is by content, so any edit revokes it. The trust file is read only from the user's
  state home, never from a project, so a cloned repository cannot pre-trust itself.

Discovery (stat + hash) runs at most once per process and costs two ``lstat`` calls when no
extension directory exists. Drop-ins are imported lazily, the first time a lookup can use one,
from the exact bytes that were hashed (no re-read between check and use). Symlinked files and
directories, files not owned by the current user and world-writable files are skipped.

A drop-in declares what it provides in a module-level dict::

    TOKEN_SPAREN_EXTENSION = {"compressors": [...], "decision_points": [...], "signals": [...]}

Any failure (import error, bad manifest, wrong type, exception, dropped error line) disables the
extension for the rest of the process with one ``errors.jsonl`` line; the hook never crashes.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
import time
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any

from token_sparen.compress.base import Compressor, is_compressor, registry, unwrap
from token_sparen.config import Config, log_error
from token_sparen.learn import policy
from token_sparen.learn.policy import DecisionPoint
from token_sparen.store import jsonstore

GROUPS = (
    "token_sparen.compressors",
    "token_sparen.decision_points",
    "token_sparen.signals",
    "token_sparen.adapters",
)
TRUST_FILE = "trusted_extensions.json"
MANIFEST = "TOKEN_SPAREN_EXTENSION"
MAX_DROPIN_BYTES = 1_000_000
PREVIEW_LINES = 40
OVERRIDE_ENV = "TOKEN_SPAREN_EXTENSIONS__ALLOW_OVERRIDE"
_MANIFEST_KEYS = frozenset({"compressors", "decision_points", "signals"})
_SHA_RE = re.compile(r"[0-9a-f]{64}")

__all__ = [
    "GROUPS",
    "DropIn",
    "LoadedExtensions",
    "UnsafeFile",
    "discover",
    "entry_point_listing",
    "load_extensions",
    "preview",
    "reset",
    "trust",
    "trusted_hashes",
    "unwrap",
    "untrust",
]


class UnsafeFile(Exception):
    """A drop-in or trust file that must not be read; ``reason`` is a short slug."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class DropIn:
    """One discovered drop-in. ``status``: trusted, untrusted, changed-since-trust, skipped:*."""

    path: Path
    status: str
    sha256: str | None = None
    data: bytes | None = field(default=None, repr=False, compare=False)


def _entry_points(group: str) -> Iterable[Any]:
    return metadata.entry_points(group=group)


def _current_uid() -> int | None:
    getuid = getattr(os, "getuid", None)
    return int(getuid()) if getuid is not None else None


def _check_stat(st: os.stat_result) -> None:
    if not stat.S_ISREG(st.st_mode):
        raise UnsafeFile("not-a-file")
    uid = _current_uid()
    if uid is None:  # Windows: no POSIX owner/mode bits to check
        return
    if st.st_uid != uid:
        raise UnsafeFile("not-owned")
    if st.st_mode & stat.S_IWOTH:
        raise UnsafeFile("world-writable")


def _read_checked(path: Path) -> bytes:
    """Bytes of a regular, non-symlink file owned by the user and not world-writable."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise UnsafeFile("missing") from None
    except OSError:
        raise UnsafeFile("unreadable") from None
    if stat.S_ISLNK(st.st_mode):
        raise UnsafeFile("symlink")
    _check_stat(st)
    if st.st_size > MAX_DROPIN_BYTES:
        raise UnsafeFile("too-large")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise UnsafeFile("symlink" if exc.errno == errno.ELOOP else "unreadable") from None
    with os.fdopen(fd, "rb") as fh:
        _check_stat(os.fstat(fh.fileno()))  # the file actually opened, not the one stat'ed
        data = fh.read(MAX_DROPIN_BYTES + 1)
    if len(data) > MAX_DROPIN_BYTES:
        raise UnsafeFile("too-large")
    return data


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_trust(home: Path, env: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    """``{sha: {"path", "ts"}}`` from the user's trust file; unsafe or corrupt means ``{}``."""
    path = home / TRUST_FILE
    try:
        data = _read_checked(path)
    except UnsafeFile as exc:
        if exc.reason != "missing":
            log_error(env, "extensions.trust", f"{TRUST_FILE} ignored: {exc.reason}")
        return {}
    try:
        doc = json.loads(data.decode("utf-8"))
    except ValueError as exc:
        log_error(env, "extensions.trust", f"{TRUST_FILE} ignored: {exc}")
        return {}
    trusted = doc.get("trusted") if isinstance(doc, dict) else None
    if not isinstance(trusted, dict):
        return {}
    return {
        sha: entry
        for sha, entry in trusted.items()
        if isinstance(sha, str) and _SHA_RE.fullmatch(sha) and isinstance(entry, dict)
    }


def trusted_hashes(home: Path, env: Mapping[str, str]) -> set[str]:
    return set(_read_trust(home, env))


def _ext_dirs(project_dir: Path, home: Path) -> list[tuple[Path, tuple[Path, ...]]]:
    """(extensions dir, parents that must not be symlinks either)."""
    state = project_dir / ".token-sparen"
    return [(state / "extensions", (state,)), (home / "extensions", ())]


def discover(project_dir: Path, home: Path, env: Mapping[str, str]) -> list[DropIn]:
    """Every drop-in candidate with its trust status. Never imports anything."""
    found: list[DropIn] = []
    seen: set[tuple[int, int]] = set()
    trust: dict[str, dict[str, Any]] | None = None
    for ext_dir, parents in _ext_dirs(project_dir, home):
        try:
            st = os.lstat(ext_dir)
        except OSError:
            continue  # the common case: no directory, one stat call
        if stat.S_ISLNK(st.st_mode) or any(os.path.islink(p) for p in parents):
            found.append(DropIn(ext_dir, "skipped:symlinked-dir"))
            continue
        if not stat.S_ISDIR(st.st_mode):
            found.append(DropIn(ext_dir, "skipped:not-a-dir"))
            continue
        key = (st.st_dev, st.st_ino)
        if key in seen:
            continue
        seen.add(key)
        try:
            entries = sorted(
                (e for e in os.scandir(ext_dir) if e.name.endswith(".py")), key=lambda e: e.name
            )
        except OSError as exc:
            log_error(env, "extensions", f"cannot list {ext_dir.name}: {exc}")
            continue
        for entry in entries:
            path = Path(entry.path)
            if entry.is_symlink():
                found.append(DropIn(path, "skipped:symlink"))
                continue
            try:
                data = _read_checked(path)
            except UnsafeFile as exc:
                found.append(DropIn(path, f"skipped:{exc.reason}"))
                continue
            sha = _sha(data)
            if trust is None:
                trust = _read_trust(home, env)
            if sha in trust:
                found.append(DropIn(path, "trusted", sha, data))
                continue
            real = os.path.realpath(path)
            known = any(e.get("path") == real for e in trust.values())
            found.append(DropIn(path, "changed-since-trust" if known else "untrusted", sha))
    return found


def _validate_manifest(raw: Any) -> dict[str, list[Any]]:
    if not isinstance(raw, dict):
        raise TypeError(f"{MANIFEST} missing or not a dict (got {type(raw).__name__})")
    unknown = sorted(str(k) for k in raw if k not in _MANIFEST_KEYS)
    if unknown:
        raise ValueError(f"{MANIFEST} has unknown keys {unknown}")
    out: dict[str, list[Any]] = {}
    for key, items in raw.items():
        if not isinstance(items, list | tuple):
            raise TypeError(f"{MANIFEST}[{key!r}] must be a list")
        out[key] = list(items)
    return out


def _as_compressor(item: Any) -> Compressor:
    obj = item() if isinstance(item, type) else item
    if not is_compressor(obj):
        raise TypeError(f"{type(obj).__name__} is not a Compressor")
    result: Compressor = obj
    return result


def _as_signal(item: Any) -> Callable[..., Any]:
    if not callable(item):
        raise TypeError(f"{type(item).__name__} is not a callable signal")
    fn: Callable[..., Any] = item
    return fn


def _as_adapter(item: Any) -> Any:
    obj = item() if isinstance(item, type) else item
    if not (
        isinstance(getattr(obj, "name", None), str)
        and callable(getattr(obj, "parse", None))
        and callable(getattr(obj, "render", None))
    ):
        raise TypeError(f"{type(obj).__name__} is not a HostAdapter")
    return obj


@dataclass
class _Provided:
    compressors: list[Compressor] = field(default_factory=list)
    decision_points: list[DecisionPoint] = field(default_factory=list)
    signals: list[Callable[..., Any]] = field(default_factory=list)


def _import_dropin(d: DropIn) -> _Provided:
    """Execute the trusted bytes as module ``token_sparen_ext_<sha[:12]>`` and read its manifest."""
    assert d.sha256 is not None and d.data is not None
    name = f"token_sparen_ext_{d.sha256[:12]}"
    spec = importlib.util.spec_from_file_location(name, d.path)
    if spec is None:
        raise ImportError("no import spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        # The hashed bytes are executed, never a re-read of the path (no check/use race).
        # stdout belongs to the host protocol (one JSON line): extension prints go to stderr.
        with contextlib.redirect_stdout(sys.stderr):
            exec(compile(d.data, str(d.path), "exec", dont_inherit=True), module.__dict__)
        manifest = _validate_manifest(getattr(module, MANIFEST, None))
        return _Provided(
            compressors=[_as_compressor(x) for x in manifest.get("compressors", [])],
            decision_points=[policy.validate_point(x) for x in manifest.get("decision_points", [])],
            signals=[_as_signal(x) for x in manifest.get("signals", [])],
        )
    except BaseException:
        sys.modules.pop(name, None)
        raise


class LoadedExtensions:
    """Discovered extensions for one (project, home); everything is imported on demand."""

    def __init__(
        self,
        project_dir: Path,
        home: Path,
        dropins: list[DropIn],
        allow_override: bool,
        env: Mapping[str, str],
    ) -> None:
        self.project_dir = project_dir
        self.home = home
        self.dropins = dropins
        self.allow_override = allow_override
        self._env = env
        self._provided: _Provided | None = None
        self._by_type: dict[str, list[Compressor]] | None = None
        self._points: list[DecisionPoint] | None = None
        self._signals: list[Callable[..., Any]] | None = None
        self._adapters: dict[str, Any] | None = None

    def _log(self, where: str, message: str) -> None:
        log_error(self._env, f"extensions.{where}", message)

    def _dropins(self) -> _Provided:
        if self._provided is None:
            merged = _Provided()
            done: set[str] = set()
            for d in self.dropins:
                if d.status != "trusted" or d.sha256 is None or d.sha256 in done:
                    continue
                done.add(d.sha256)
                try:
                    got = _import_dropin(d)
                except (Exception, SystemExit) as exc:  # noqa: BLE001 - third-party code
                    self._log("dropin", f"{d.path.name}: disabled: {type(exc).__name__}: {exc}")
                    continue
                merged.compressors.extend(got.compressors)
                merged.decision_points.extend(got.decision_points)
                merged.signals.extend(got.signals)
            self._provided = merged
        return self._provided

    def _dropin_compressors(self, ctype: str) -> Sequence[Compressor]:
        if not any(d.status == "trusted" for d in self.dropins):
            return ()
        if self._by_type is None:
            by_type: dict[str, list[Compressor]] = {}
            for c in self._dropins().compressors:
                for t in c.handles:
                    by_type.setdefault(t, []).append(c)
            self._by_type = by_type
        return self._by_type.get(ctype, [])

    def compressor_for(self, ctype: str) -> Compressor:
        """Built-in unless a usable extension applies (new type, or ``allow_override``)."""
        try:
            return registry.for_type(
                ctype, allow_override=self.allow_override, extra=self._dropin_compressors
            )
        except Exception as exc:  # noqa: BLE001 - never break the hook
            self._log("compressor", f"lookup {ctype}: {exc}")
            return registry.for_type(ctype)

    def _group(self, group: str, convert: Callable[[Any], Any]) -> list[Any]:
        try:
            eps = list(_entry_points(group))
        except Exception as exc:  # noqa: BLE001 - metadata problems never break a hook
            self._log("entry_points", f"{group}: {exc}")
            return []
        out: list[Any] = []
        for ep in eps:
            name = getattr(ep, "name", "?")
            try:
                out.append(convert(ep.load()))
            except (Exception, SystemExit) as exc:  # noqa: BLE001 - skip broken plugins
                self._log("entry_points", f"{group} {name}: {type(exc).__name__}: {exc}")
        return out

    def decision_points(self) -> list[DecisionPoint]:
        """Extension decision points, registered with the policy registry (built-ins win)."""
        if self._points is None:
            candidates: list[DecisionPoint] = self._group(
                "token_sparen.decision_points", policy.validate_point
            )
            if any(d.status == "trusted" for d in self.dropins):
                candidates.extend(self._dropins().decision_points)
            accepted: list[DecisionPoint] = []
            for point in candidates:
                try:
                    policy.register_point(point)
                except ValueError as exc:
                    self._log("decision_points", f"{point.name}: skipped: {exc}")
                    continue
                accepted.append(point)
            self._points = accepted
        return list(self._points)

    def signals(self) -> list[Callable[..., Any]]:
        if self._signals is None:
            found: list[Callable[..., Any]] = self._group("token_sparen.signals", _as_signal)
            if any(d.status == "trusted" for d in self.dropins):
                found.extend(self._dropins().signals)
            self._signals = found
        return list(self._signals)

    def adapters(self) -> dict[str, Any]:
        if self._adapters is None:
            found: dict[str, Any] = {}
            for adapter in self._group("token_sparen.adapters", _as_adapter):
                found.setdefault(adapter.name, adapter)
            self._adapters = found
        return dict(self._adapters)


def _truthy(value: Any) -> bool:
    """One rule for every source: true, 1 (int) and "1"/"true" (any case) enable."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true"}
    return False


def _user_allows_override(home: Path, env: Mapping[str, str]) -> bool:
    if _truthy(env.get(OVERRIDE_ENV)):
        return True
    try:
        user = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    section = user.get("extensions")
    return isinstance(section, dict) and _truthy(section.get("allow_override"))


def _allow_override(cfg: Config, home: Path, env: Mapping[str, str]) -> bool:
    """True only if the merged config says so *and* the user (not a project) set it."""
    if not _truthy(cfg.get("extensions.allow_override")):
        return False
    if _user_allows_override(home, env):
        return True
    log_error(
        env,
        "extensions",
        "allow_override ignored: set it in ~/.token-sparen/config.toml or "
        f"{OVERRIDE_ENV}, not in a project file",
    )
    return False


_CACHE: dict[tuple[str, str], LoadedExtensions] = {}


def load_extensions(
    project_dir: Path,
    home: Path,
    cfg: Config,
    env: Mapping[str, str] | None = None,
) -> LoadedExtensions:
    """Discover extensions once per process per (project, home). Never raises."""
    env = os.environ if env is None else env
    key = (str(project_dir), str(home))
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    try:
        dropins = discover(project_dir, home, env)
        allow = _allow_override(cfg, home, env)
    except Exception as exc:  # noqa: BLE001 - fail open: built-ins only
        log_error(env, "extensions", f"discovery: {exc}")
        dropins, allow = [], False
    loaded = LoadedExtensions(project_dir, home, dropins, allow, env)
    _CACHE[key] = loaded
    return loaded


def reset() -> None:
    """Forget discovery results (after trust changes, and in tests)."""
    _CACHE.clear()


def entry_point_listing() -> list[tuple[str, str, str]]:
    """``(group, name, value)`` for every installed entry point. Loads nothing."""
    out: list[tuple[str, str, str]] = []
    for group in GROUPS:
        try:
            eps = list(_entry_points(group))
        except Exception:  # noqa: BLE001 - listing is best effort
            continue
        out.extend(
            (group, str(getattr(ep, "name", "?")), str(getattr(ep, "value", "?"))) for ep in eps
        )
    return out


def printable(text: str) -> str:
    """Escape control characters so file content cannot drive the terminal."""
    return "".join(
        ch
        if ch == "\t" or ch.isprintable()
        else f"\\x{ord(ch):02x}"
        if ord(ch) < 0x100
        else f"\\u{ord(ch):04x}"
        for ch in text
    )


def preview(path: Path, lines: int = PREVIEW_LINES) -> tuple[str, list[str]]:
    """sha256 and the first ``lines`` lines (escaped) of a drop-in; raises UnsafeFile."""
    data = _read_checked(path)
    text = data.decode("utf-8", errors="replace")
    return _sha(data), [printable(x) for x in text.splitlines()[:lines]]


def trust(path: Path, home: Path, env: Mapping[str, str], expect_sha: str | None = None) -> str:
    """Record the file's current content hash as trusted; returns it. Raises UnsafeFile."""
    data = _read_checked(path)
    sha = _sha(data)
    if expect_sha is not None and sha != expect_sha:
        raise UnsafeFile("changed-while-trusting")
    real = os.path.realpath(path)

    def apply(doc: dict[str, Any]) -> dict[str, Any]:
        trusted = doc.get("trusted")
        if not isinstance(trusted, dict):
            trusted = {}
        trusted[sha] = {"path": real, "ts": time.time()}
        doc["trusted"] = trusted
        return doc

    jsonstore.update(home / TRUST_FILE, apply, {"trusted": {}}, env)
    reset()
    return sha


def untrust(target: str, home: Path, env: Mapping[str, str]) -> int:
    """Remove trust by sha256, or by path (its current hash and its recorded path)."""
    shas: set[str] = set()
    real: str | None = None
    if _SHA_RE.fullmatch(target.lower()):
        shas.add(target.lower())
    else:
        real = os.path.realpath(target)
        try:
            shas.add(_sha(_read_checked(Path(target))))
        except UnsafeFile:
            pass
    removed = 0

    def apply(doc: dict[str, Any]) -> dict[str, Any]:
        nonlocal removed
        trusted = doc.get("trusted")
        if not isinstance(trusted, dict):
            trusted = {}
        keep = {
            sha: entry
            for sha, entry in trusted.items()
            if sha not in shas and not (isinstance(entry, dict) and entry.get("path") == real)
        }
        removed = len(trusted) - len(keep)
        doc["trusted"] = keep
        return doc

    jsonstore.update(home / TRUST_FILE, apply, {"trusted": {}}, env)
    reset()
    return removed
