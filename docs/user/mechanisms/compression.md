# Output compression

**Hook:** PostToolUse. **Default:** on. **Learned:** yes, how strongly to trim (see [Learning engine](../learning-engine.md)).

When a tool result is at least 2000 estimated tokens and is one of the allowed kinds of output, token-sparen may replace it with a shorter version that keeps the head and tail, every error line and failures, and ends with a recovery marker. The full text is first saved to `<project>/.token-sparen/outputs/<id>.txt`.

## What may be trimmed

- diffs (`git diff`, `git show <commit>`, `git log -p`, `git stash show -p`) and search results (Grep/Glob, `grep`, `rg`);
- test reports (pytest, jest, vitest, go test, cargo test, ...), compiler output (tsc, rustc, gcc, javac, ...), build and install logs (npm, pip, gradle, docker build, ...);
- JSON (API responses, MCP tools, `curl`), `pplx` search results and timestamped logs.

## What is shown in full by default

File contents: the Read tool; any tool (MCP included) whose name says it reads a file (`read_file`, `ReadFile`, `get_file_contents`, `file_read`, `read_text`, `cat_file`, `view_file`, `open_file`); and any shell or PowerShell command with a part that prints a file, wherever it sits in a chain, pipe or loop (`cat`, `head`, `tail`, `sed -n`, `less`, `bat`, `nl`, `awk`, `jq <file>`, `xxd`, `git show <rev>:<path>`, `Get-Content`, and similar, also inside `$(...)`, `eval`, `ssh` or `docker exec` wrappers, and any command whose stdin is redirected from a file). Plain text, tables and stack traces from any tool (web results included) are also shown in full and never reach the learner. A subagent's report (Agent/Task) is never trimmed at all, whatever it looks like (a log, a test run, call sites, JSON), and whatever `allow_ctypes` says: it is the main agent's only view of the subagent's work.

File-content detection is best effort: an unusual binary or wrapper may slip through, and scripts you run (`./x.sh`, `bash x.sh`) are not inspected.

To widen this, in your own `~/.token-sparen/config.toml` or with `token-sparen config set` for one session (a project file may only narrow it; see [Configuration](../configuration.md)):

```toml
[compression]
trim_file_contents = true   # also trim Read results and cat/head/tail/... output
allow_ctypes = ["diff", "search", "test_report", "compiler", "build_log", "json", "pplx", "log", "text"]
```

Quality warning: the agent usually goes on to use a file's contents (to edit or quote it), so trimming them often costs a recovery read and can cost answer quality. Check this on your own sessions with `token-sparen replay --policy static` against `--policy default` (see [Benchmarks and methodology](../benchmarks-methodology.md#measuring-it-on-your-own-sessions-offline-private)).

## Guarantees

- **Never larger.** The replacement is used only if it is no longer than what the model would have seen anyway and saves at least 15 % of it.
- **Floor.** Results below 2000 estimated tokens are never modified; a lower `compression.min_tokens` is ignored and `doctor` flags it, a higher one is honoured.
- **Failing tests kept.** In a test report, every failing test keeps its whole assertion detail (pytest `E` lines and the failing source line, expected/received diffs, cargo `left:`/`right:`); only its other lines are capped by `test_report.block_lines`, and passing tests become a count.
- **Errors kept.** Error lines, failures and stack-trace edges are never elided. Besides `error`, `exception`, `fail`, `fatal`, `panic` and `assert`, this covers `npm ERR!`, `Segmentation fault`/`segfault`, `Killed`, `Permission denied`, `EACCES`, `Access is denied`/`access denied`, `undefined reference` and `command not found`.
- **Never touched:** edit tools (`Edit`, `Write`, `MultiEdit`, `NotebookEdit`), `TodoWrite`, `AskUserQuestion`, `ExitPlanMode`, images and PDFs, and failing tool calls (Claude Code cannot replace the result of a failed call).
- **Recoverable.** The marker names the id and path: [Recovering trimmed output](../host-guides/claude-code.md#recovering-trimmed-output).
- **Fail open.** A hook slower than 400 ms, or any error, leaves the output as is; if the recovery file cannot be saved in time (`compression.save_deadline_ms`) the output is left as is.

## Switching it off

`[learn.hold] compression = true` (stops every trim, with learning on or off) or `[compression] allow_ctypes = []`. See [Turning it off](../host-guides/claude-code.md#turning-it-off).
