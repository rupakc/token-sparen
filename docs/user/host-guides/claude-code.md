# Claude Code guide

All of this is on by default unless marked opt-in. Nothing is sent anywhere. Requirements are in the [support matrix](../support.md).

## What changes in your session

- **Compression (quality first).** A tool result of at least 2000 estimated tokens that is a diff, search result, test report, compiler output, build or install log, JSON, `pplx` result or timestamped log may be replaced by a shorter version that keeps the head and tail and every error line, ending with a recovery marker. File contents, plain text, tables and stack traces are shown in full by default. Details: [Output compression](../mechanisms/compression.md).
- **Never larger.** A replacement is used only if it is no longer than what the model would have seen anyway and saves at least 15 % of it.
- **Never touched.** Results under 2000 tokens, edit tools, `TodoWrite`, `AskUserQuestion`, `ExitPlanMode`, images and PDFs, and failing tool calls (Claude Code cannot replace the result of a failed call).
- **Guards** block a few unbounded commands: [Guards](../mechanisms/guards.md).
- **Read de-duplication** returns a short pointer for an unchanged file that is still in context: [Read de-duplication](../mechanisms/read-dedupe.md).
- **Notices** in your terminal (no model tokens) about budget and context use, and a hint to `/compact` at a task boundary: [Governor](../mechanisms/governor.md), [Boundary](../mechanisms/boundary.md).
- **Failure-loop guard** adds one short line when the same error repeats within a turn: [Failure-loop guard](../mechanisms/loop-guard.md).
- **Learned repository facts** at session start: [Facts](../mechanisms/facts.md).
- **Opt-in:** [tool pruning](../mechanisms/tool-pruning.md), the [statusline](../mechanisms/statusline.md), the output-style directive and the hard stop.
- token-sparen never modifies a command you or the model typed.

## Recovering trimmed output

A trimmed result ends with a marker:

```
[token-sparen: kept 48 of 1930 lines · full output: <path> · `token-sparen show a1b2c3d4e5f6`]
```

- `token-sparen show <id>` prints the full text (the id is 12 hex characters); or read the `<path>` file. Add `--project <dir>` if you are not in that project. `show` serves only outputs this install saved itself; an output saved before an upgrade stays readable at its marker path until the 14-day cleanup. In Claude Code without the CLI installed, ask for `/token-sparen` and the skill runs the bundled CLI.
- Full outputs live in `<project>/.token-sparen/outputs/<id>.txt`. Old files are pruned whenever a new one is written: those older than 14 days, then the oldest beyond 200 files or 100 MB.
- Error lines, failures and stack-trace edges are never elided. Ask the model to read the file when it needs more; usually the visible lines suffice.

## The /token-sparen skill

| Command | What it does |
|---|---|
| `/token-sparen` or `/token-sparen stats` | Net tokens (removed by trims, minus recovery reads, minus tokens token-sparen injected) for this session and the project; net can be zero or negative |
| `/token-sparen doctor` | Checks Python, state directory, hook files and a synthetic round trip; reports plugin and CLI versions |
| `/token-sparen statusline` | Offers the opt-in [statusline](../mechanisms/statusline.md) |
| `/token-sparen disable` / `enable` | Switches trimming off or on for this session |

`stats`, `show`, `doctor`, `session disable` and `prune restore` are pre-approved for the skill; every other subcommand goes through Claude Code's normal permission prompt.

## Turning it off

| To stop | How |
|---|---|
| Everything | `TOKEN_SPAREN_DISABLE=1` (every hook becomes a no-op; any of `1`, `true`, `yes`, `on`). It must be in the environment Claude Code is **launched** from; setting it in a running session's shell is too late. Or `claude plugin disable token-sparen` (`claude plugin enable token-sparen` undoes it) |
| Everything, this session only | `/token-sparen disable` (and `enable`), or `token-sparen session disable` (without `--session` it acts on this project's most recently active session, if active in the last 6 hours) |
| All trimming of tool output | `[learn.hold] compression = true` in `~/.token-sparen/config.toml`; for one session `token-sparen config set learn.hold.compression true`. A project's `token-sparen.toml` may set it too. Config-only alternative: `[compression] allow_ctypes = []`. Guards and read de-duplication stay on |
| Read pointers only | `[learn.hold] read_dedupe = true` or `read_dedupe.enabled = false` |
| Learned repository facts | `token-sparen facts disable` (this project), `[facts] enabled = false`, or `token-sparen facts forget <id>` |
| Failure-loop guard | `[loopguard] enabled = false` (`[learn.hold] loopguard = true` only keeps the most patient setting) |
| Output-style directive | Off unless you opted in; `[output_style] enabled = false` |
| Tool pruning | Off unless you opted in; `token-sparen prune disable` removes every rule token-sparen wrote |
| Blocking, notices, other mechanisms | `guards.enabled`, `governor.enabled`, `boundary.enabled` or `subagent.enabled` set to `false` |
| Uninstall | See [Uninstall](../uninstall.md) |

There is no `compression.enabled` key; use the hold or `allow_ctypes` row. Setting `[learn] enabled = false` does **not** stop trimming: it falls back to fixed defaults (standard compression), which trim more than a cold learner does.
