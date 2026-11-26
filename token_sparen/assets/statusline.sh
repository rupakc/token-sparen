#!/bin/sh
# token-sparen statusline launcher (POSIX sh). Always prints one line and exits 0.
#   sh statusline.sh                                 render our statusline
#   sh statusline.sh --compose -- '<original cmd>'   original first line + " | " + ours
# The active copy of the plugin is resolved at every run: $TOKEN_SPAREN_ROOT, then
# installed_plugins.json (newest), then the plugin cache (newest), then FALLBACK_ROOT (set by
# `token-sparen statusline install`). Nothing found: an empty line (compose: the original only).
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


def resolve(fallback):
    root = os.environ.get("TOKEN_SPAREN_ROOT")
    if usable(root):
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


def run():
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    args = sys.argv[2:]
    root = resolve(sys.argv[1])
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

if command -v python3 >/dev/null 2>&1; then
  exec python3 -I -c "$BOOT" "$FALLBACK_ROOT" "$@"
fi
if [ "$1" = "--compose" ] && [ "$2" = "--" ] && [ -n "$3" ]; then
  sh -c "$3" 2>/dev/null | head -n 1
  exit 0
fi
echo
exit 0
