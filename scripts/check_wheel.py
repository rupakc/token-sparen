"""Build the wheel, install it in a fresh venv outside the checkout, and smoke-test the CLI.

Run from the repository root: ``python scripts/check_wheel.py`` (needs ``pip install build``).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import venv
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIRED_FILES = (
    "token_sparen/params/defaults.toml",
    "token_sparen/assets/statusline.sh",
    "token_sparen/__main__.py",
    "token_sparen/testing/conformance.py",
)


def _run(argv: list[str], cwd: Path, home: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["TOKEN_SPAREN_HOME"] = str(home)
    proc = subprocess.run(  # noqa: S603
        argv,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        sys.exit(f"FAIL {' '.join(argv)}\n{proc.stdout}\n{proc.stderr}")
    return proc


def _subpackages() -> list[str]:
    return sorted(
        p.parent.relative_to(ROOT).as_posix()
        for p in (ROOT / "token_sparen").rglob("__init__.py")
        if "__pycache__" not in p.parts
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="token-sparen-wheel-") as tmp_s:
        tmp = Path(tmp_s)
        dist = tmp / "dist"
        home = tmp / "home"
        home = tmp / "home"
        _run(
            [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist), str(ROOT)], tmp, home
        )
        (wheel,) = dist.glob("*.whl")
        names = set(zipfile.ZipFile(wheel).namelist())
        missing = [f"{d}/__init__.py" for d in _subpackages() if f"{d}/__init__.py" not in names]
        missing += [f for f in REQUIRED_FILES if f not in names]
        if missing:
            sys.exit(f"FAIL wheel is missing: {missing}")
        print(f"ok wheel {wheel.name}: {len(_subpackages())} packages + data present")
        env_dir = tmp / "venv"
        venv.EnvBuilder(with_pip=True).create(env_dir)
        bindir = env_dir / ("Scripts" if sys.platform == "win32" else "bin")
        _run([str(bindir / "python"), "-m", "pip", "install", "-q", str(wheel)], tmp, home)
        work = tmp / "work"  # outside the checkout: the repo must not be on sys.path
        work.mkdir()
        exe = str(bindir / "token-sparen")
        print("ok", _run([exe, "--version"], work, home).stdout.strip())
        _run([exe, "stats", "--json"], work, home)
        print("ok stats --json")
        out = _run([exe, "fold", "--all"], work, home).stdout.strip()
        print(f"ok {out}")
        _run([exe, "extensions", "list"], work, home)
        py = str(bindir / "python")
        probe = (
            "from token_sparen.compress import registry;"
            "from token_sparen.testing.conformance import check_compressor;"
            "assert all(check_compressor(c) == [] for c in registry.builtins())"
        )
        _run([py, "-c", probe], work, home)
        print("ok extensions list + conformance kit importable")
        # plain text is not trimmed by default (quality first); a timestamped log is
        out = _run([exe, "run", "--", "seq", "1", "5000"], work, home).stdout
        if "[token-sparen: kept" in out or (work / ".token-sparen" / "outputs").exists():
            sys.exit("FAIL run trimmed plain text (not an allowed content type by default)")
        log = "for i in range(5000): print(f'2026-11-20T10:00:{i % 60:02d} INFO worker {i} ok')"
        out = _run([exe, "run", "--", py, "-c", log], work, home).stdout
        saved = list((work / ".token-sparen" / "outputs").glob("*.txt"))
        shown = re.search(r"token-sparen show ([0-9a-f]{12})", out)
        if len(saved) != 1 or not shown or saved[0].stem != shown.group(1):
            sys.exit(f"FAIL run did not save a recovery file\n{out[-300:]}")
        print(f"ok run saved {saved[0].name}")
        settings = tmp / "claude" / "settings.json"
        settings.parent.mkdir()
        settings.write_text("{}\n", encoding="utf-8")
        install = _run([exe, "statusline", "install", "--settings", str(settings)], work, home)
        if "statusLine" not in settings.read_text(encoding="utf-8"):
            sys.exit(f"FAIL statusline install did not write settings\n{install.stdout}")
        _run([exe, "statusline", "uninstall", "--settings", str(settings)], work, home)
        if "statusLine" in settings.read_text(encoding="utf-8"):
            sys.exit("FAIL statusline uninstall did not restore settings")
        print("ok statusline install + uninstall from the wheel")
    return 0


if __name__ == "__main__":
    sys.exit(main())
