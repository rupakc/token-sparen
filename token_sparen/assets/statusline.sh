#!/bin/sh
# token-sparen statusline launcher (POSIX sh). Always prints one line and exits 0.
#   sh statusline.sh                                 render our statusline
#   sh statusline.sh --compose -- '<original cmd>'   original first line + " | " + ours
# The active copy of the plugin is resolved at every run: $TOKEN_SPAREN_ROOT (only outside the
# current project / git work tree and owned by the user), then
# installed_plugins.json (newest), then the plugin cache (newest), then FALLBACK_ROOT (set by
# `token-sparen statusline install`). Nothing found: an empty line (compose: the original only).
# A python3 older than 3.11 re-launches the statusline on a newer Python found like the hooks do
# (<root>/hooks/_pyfind.py).
FALLBACK_ROOT=''

BOOT='
import json, os, subprocess, sys

MOD = os.path.join("token_sparen", "adapters", "claude", "statusline.py")


def usable(path):
    return bool(path) and os.path.isfile(os.path.join(path, MOD))


def mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


def collect(node, out):
    if isinstance(node, dict):
        path = node.get("installPath")
        if isinstance(path, str) and "token-sparen" in path:
            out.append(path)
        for value in node.values():
            collect(value, out)
    elif isinstance(node, list):
        for value in node:
            collect(value, out)


def newest(paths):
    good = [p for p in paths if usable(p)]
    return max(good, key=mtime) if good else None


def ident(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def chain(path):
    out = set()
    cur = path
    while True:
        i = ident(cur)
        if i is not None:
            out.add(i)
        parent = os.path.dirname(cur)
        if parent == cur:
            return out
        cur = parent


def work_tree(start):
    cur = start
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def trusted(path):
    """A $TOKEN_SPAREN_ROOT outside the current project (its dir, the cwd and their git work
    trees, compared by file identity; a directory containing the home dir is no project) and,
    on POSIX, owned by us: a committed settings env must not make the statusline run code from
    the repository. Same rule as token_sparen.config.env_path."""
    try:
        target = os.path.realpath(os.path.join(os.getcwd(), path))
        home = chain(os.path.realpath(os.path.expanduser("~")))
        inside = chain(target)
        for raw in (os.environ.get("CLAUDE_PROJECT_DIR") or "", os.getcwd()):
            if not raw:
                continue
            base = os.path.realpath(raw)
            for root in (base, work_tree(base)):
                i = None if root is None else ident(root)
                if i is not None and i not in home and i in inside:
                    return False
        if os.name == "posix" and hasattr(os, "getuid"):
            return os.stat(target).st_uid == os.getuid()
        return True
    except Exception:
        return False


def resolve(fallback):
    root = os.environ.get("TOKEN_SPAREN_ROOT")
    if usable(root) and trusted(root):
        return root
    config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    plugins = os.path.join(config, "plugins")
    found = []
    try:
        with open(os.path.join(plugins, "installed_plugins.json"), encoding="utf-8") as fh:
            collect(json.load(fh), found)
    except Exception:
        pass
    got = newest(found)
    if got:
        return got
    cache = os.path.join(plugins, "cache")
    found = []
    try:
        for market in os.listdir(cache):
            base = os.path.join(cache, market, "token-sparen")
            if os.path.isdir(base):
                found.extend(os.path.join(base, v) for v in os.listdir(base))
    except OSError:
        pass
    return newest(found) or (fallback if usable(fallback) else None)


def fallback_line(args):
    if len(args) < 3 or args[:2] != ["--compose", "--"]:
        return ""
    try:
        data = sys.stdin.buffer.read()
        proc = subprocess.run(["sh", "-c", args[2]], input=data, capture_output=True, timeout=1)
        lines = proc.stdout.decode("utf-8", "replace").splitlines()
        return lines[0] if proc.returncode == 0 and lines else ""
    except Exception:
        return ""


SHIM = "\n".join([
    "import os, sys",
    "os.environ.pop(\"TOKEN_SPAREN_REEXEC\", None)",
    "try:",
    "    sys.path.insert(0, sys.argv[1])",
    "    from token_sparen.adapters.claude.statusline import main",
    "except Exception:",
    "    sys.stdout.write(\"\\n\")",
    "    sys.exit(0)",
    "sys.exit(main(sys.argv[2:]))",
])


def hop(root, args):
    """On a python3 older than 3.11 run the statusline on a Python >= 3.11 found like the hooks
    do (root/hooks/_pyfind.py). None: carry on here (only the fallback line if too old)."""
    child = os.environ.pop("TOKEN_SPAREN_REEXEC", None) is not None
    force = os.environ.get("TOKEN_SPAREN_LAUNCHER_FORCE_REEXEC") == "1" and bool(
        os.environ.get("TOKEN_SPAREN_HOME"))
    if child or not root or (sys.version_info >= (3, 11) and not force):
        return None
    hooks = os.path.join(root, "hooks")
    try:
        sys.path.insert(0, hooks)
        try:
            import _pyfind as pyfind
        finally:
            sys.path.remove(hooks)
        if sys.version_info >= (3, 11) and not pyfind.forced(os.environ):
            return None
        found = pyfind.find_python(os.environ, tuple(sys.version_info[:2]), root=root)
        if found is None:
            return None
        return pyfind.relaunch(found[0], ["-I", "-S", "-c", SHIM, root] + list(args))
    except Exception:
        return None


def run():
    args = sys.argv[2:]
    root = resolve(sys.argv[1])
    hopped = hop(root, args)
    if hopped is not None:
        return hopped
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if root and sys.version_info < (3, 11):
        root = None
    if root:
        sys.path.insert(0, root)
        try:
            from token_sparen.adapters.claude.statusline import main
        except Exception:
            main = None
        if main is not None:
            return main(args)
    sys.stdout.write(fallback_line(args) + "\n")
    return 0


try:
    sys.exit(run())
except SystemExit:
    raise
except Exception:
    sys.stdout.write("\n")
    sys.exit(0)
'

for py in python3 python3.15 python3.14 python3.13 python3.12 python3.11; do
  if command -v "$py" >/dev/null 2>&1; then
    exec "$py" -I -c "$BOOT" "$FALLBACK_ROOT" "$@"
  fi
done
if [ "$1" = "--compose" ] && [ "$2" = "--" ] && [ -n "$3" ]; then
  sh -c "$3" 2>/dev/null | head -n 1
  exit 0
fi
echo
exit 0
