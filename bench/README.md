# bench: the live benchmark E4

Dev-time harness for the E4 benchmark (spec §10.4, §10.5; method
[`docs/reports/m5-method.md`](../docs/reports/m5-method.md); owner runbook
[`docs/release/benchmark-runbook.md`](../docs/release/benchmark-runbook.md)). Never imported by
`token_sparen`, never in the wheel.

## Commands

`python -m bench <command> --out DIR` (or `$TOKEN_SPAREN_BENCH_OUT`; no default). The workspace
must lie outside any git work tree; directories are 0700 and files 0600. A workspace is a dry run
or a real campaign (fixed by its first command) and holds one campaign.

| Command | Does |
|---|---|
| `prepare toolchain\|templates\|images\|dataset` | checks the toolchain pins and grading venv, the per-arm config templates; pulls the Mini images; fetches the dataset rows |
| `preflight [--tasks mini\|pool]` | oracle check: gold patch twice, no-op once, per task (`grading/preflight.json`) |
| `freeze --stage S --ref TAG` | read-only plugin export; real stages accept only their plain tag (`v0.5.0-screen1`, `v1.0.0-rc1`) |
| `calibrate` | quota and disk projections for the power step (`calibration.json`) |
| `run --stage S --campaign ID [--arms a,b] [--model ID] [--wait] [--plan-only] [--dry-run]` | runs or resumes a stage |
| `quota set --weekly P --five-hour P [--weekly-resets ISO] [--five-hour-resets ISO]` | the owner's `/usage` readings (account-wide) |
| `invalidate --key ID \| --stage S --since ISO --reason TEXT` | reopens keys (never a graded confirmation run) |
| `status` | final runs per arm and status, next key, last pause, breaker, last quota readings; no secrets |
| `grade --stage S`, `analyse --stage S` | grading of final runs; a private analysis |
| `power` | the confirmation spec and power verdict (`power.json`) |
| `gate [--release-hash H] [--github-repo OWNER/NAME]` | release gate; advisory without server receipts |
| `report --stage S [--publish]` | `e4.json`, `report.md`, figures; `--publish` copies them into the repository |
| `anchors publish` | a heartbeat publication of the confirmation anchors during a pause |

Exit codes of `run`: `0` complete, `3` weekly stop, `4` five-hour wait, `5` incomplete, `6`
breaker, `7` auth, `8` token leak, `9` frozen plugin changed, `10` disk floor; `2` a refusal and
`11` an anchor publication failure for every command.

## Real campaigns

macOS or Linux only (refused on Windows). Credentials: one dedicated OAuth token in a private
file (`--token-file` or `$TOKEN_SPAREN_BENCH_TOKEN_FILE`, mode 0600, owned by the user, not a
link); `CLAUDE_CODE_OAUTH_TOKEN` in the environment is refused. Quota: the owner's readings
(`quota set`) are authoritative until calibrated; the account state lives in
`~/.token-sparen-bench` (or `$TOKEN_SPAREN_BENCH_STATE`) and is shared by every workspace; one
scheduler per account.

Workspace pieces built during preparation: `toolchain/` (Claude Code binary at the pinned
version, a Python, Node, the competitor tools; `toolchain/pins.json` with `claude_code` and
`arms`), `templates/<arm>/` (each arm's documented default install), `grade-venv/` (a venv with
`bench/requirements-grade.txt`) and `dataset.jsonl` (`prepare dataset`).

Confirmation: the plan (from `power.json`, the frozen plugin's version and the live-pricing date)
is recorded in the ledger before any run, and the plan anchor is pushed to the `bench-anchors`
branch of the repository's `origin` (or `--anchors-remote`) through a private clone in the
workspace. Then one commit per push after every graded block, a heartbeat every three hours
while running or waiting, and one at the end. `gate` fetches the branch first.

## Dry run

`python -m bench run --dry-run --campaign ci --out DIR` runs screening, grade, analyse, power,
confirmation, gate (advisory, then with receipts simulated from the temporary remote) and report
with `FakeBackend`, a fake grader, the synthetic `FAKE_TASKS`, a frozen copy of the committed
`HEAD` without tag checks, a scripted quota in `DIR/dry-account`, a virtual clock, a temporary
bare repository as the anchors remote and small bootstrap budgets. No Docker, no `claude`, no
grader, no quota, no credential; CI runs it (job `bench-dry-run`). A dry-run workspace never
publishes.

## Modules

`tasks` pool and fake tasks · `arms` + `arms.toml` arm registry · `isolation` workspace and config
isolation · `freeze` frozen plugin · `backends` fake and Docker runs · `streamjson`, `classify`,
`liveness`, `leak` run metrics, statuses, liveness and the token scan · `patch` patch extraction ·
`ledger`, `quota`, `scheduler` the resumable campaign · `grade` local grading and preflight ·
`stats`, `power`, `stages`, `gate` statistics, sizing, stages and the release gate · `anchors`
external chain anchors · `report`, `e4`, `svg` the published report · `cli` this command line.
