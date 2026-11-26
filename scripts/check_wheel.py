"""Build the wheel, install it in a fresh venv outside the checkout, and smoke-test the CLI.

Run from the repository root: ``python scripts/check_wheel.py`` (needs ``pip install build``).
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import venv
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIRED_FILES = ("token_sparen/params/defaults.toml", "token_sparen/__main__.py")


def _run(argv: list[str], cwd: Path, **kw: object) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(  # noqa: S603
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        **kw,  # type: ignore[call-overload]
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
        _run([sys.executable, "-m", "build", "--wheel", "--outdir", str(dist), str(ROOT)], tmp)
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
        _run([str(bindir / "python"), "-m", "pip", "install", "-q", str(wheel)], tmp)
        work = tmp / "work"  # outside the checkout: the repo must not be on sys.path
        work.mkdir()
        exe = str(bindir / "token-sparen")
        print("ok", _run([exe, "--version"], work).stdout.strip())
        _run([exe, "stats", "--json"], work)
        print("ok stats --json")
        out = _run([exe, "run", "--", "seq", "1", "5000"], work).stdout
        saved = list((work / ".token-sparen" / "outputs").glob("*.txt"))
        shown = re.search(r"token-sparen show ([0-9a-f]{12})", out)
        if len(saved) != 1 or not shown or saved[0].stem != shown.group(1):
            sys.exit(f"FAIL run did not save a recovery file\n{out[-300:]}")
        print(f"ok run saved {saved[0].name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
