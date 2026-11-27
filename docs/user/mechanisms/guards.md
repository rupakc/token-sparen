# Guards

**Hook:** PreToolUse. **Default:** on (`guards.enabled = true`). **Learned:** no.

Guards block a call before it runs when it is clearly unbounded, with a one-sentence reason that names the bounded alternative (for example "add `-maxdepth 3`"). Exactly these are blocked:

1. `find` starting at `/`, `~` or `~/` without `-maxdepth`;
2. recursive `grep`/`rg` into an existing `node_modules`, `.git`, `dist`, `build` or `.venv` directory without an include filter;
3. `cat` of a file larger than 5 MB whose output goes to the model (not when piped or redirected), and the Read tool on a text file larger than 5 MB without an offset/limit range (images, PDFs and notebooks are not blocked);
4. `ls -R` on `/`, `~` or `~/`.

A command that cannot be parsed is never blocked. token-sparen never rewrites a command: a guard either lets the call through or blocks it with a reason.

## Settings

| Key | Default | Meaning |
|---|---|---|
| `guards.enabled` | true | `false` turns all blocks off (a project file may only turn it off) |
| `guards.max_read_bytes` | 5000000 | size limit for `cat`/Read without a range (a project may only raise it) |

See [Configuration](../configuration.md).
