# Benchmark runbook (E4, owner)

How the owner runs the real E4 campaign (the controller-run steps of M5) with `python -m bench`. The method is
[`docs/reports/m5-method.md`](../reports/m5-method.md); the harness is described in
[`bench/README.md`](../../bench/README.md). Rehearse every step on a dry run first
(`python -m bench run --dry-run --campaign rehearsal --out <scratch dir>`); a dry run needs no
token, no Docker and no quota, and never pushes anywhere.

Real campaigns run on macOS or Linux only. Keep the workspace in a private folder outside any
git work tree and point the CLI at it once per shell:

```
export TOKEN_SPAREN_BENCH_OUT="<private folder>/bench-m5"
```

## Pinned harness

The bench never runs from your live checkout: each stage ledger pins the harness commit, so a
pull or an evidence commit there would stop the campaign. Create a separate clean clone of
the harness commit in the workspace once, from the main checkout, and run every later
`python -m bench` command from inside it:

```
cd "<main checkout>"
python -m bench prepare harness --ref <harness commit>
cd "$TOKEN_SPAREN_BENCH_OUT/harness"
```

The real `run`, `grade`, `preflight` and `gate` commands refuse to run anywhere else, refuse
a harness at another commit, and refuse uncommitted changes in it. Never edit or commit in the
harness. Commits the runbook asks for (`docs/evidence/plan.sha256`, the published report) go to
the main checkout. Replace the harness (`prepare harness --ref <commit> --replace`) only between
stages; a started stage keeps the commit its ledger recorded.

## Dedicated OAuth token

Use a token made for the benchmark only, never the daily one.

1. Run `claude setup-token` and complete the browser sign-in.
2. Store the printed token in a file only you can read, without putting it in shell history:

   ```
   umask 077
   mkdir -p "<private folder>/bench-secret"
   install -m 600 /dev/null "<private folder>/bench-secret/token"
   "$EDITOR" "<private folder>/bench-secret/token"   # paste the token, one line, save
   export TOKEN_SPAREN_BENCH_TOKEN_FILE="<private folder>/bench-secret/token"
   ```

   Use an editor that does not write swap, backup or undo files next to the token (or
   turn them off for this file), and check afterwards that nothing else appeared in the
   folder.
3. Never export `CLAUDE_CODE_OAUTH_TOKEN` in the bench shell: the CLI refuses to run when it is
   set, and it refuses a token file that is a link, not owned by you, or readable by group or
   others. The CLI never prints the token; `python -m bench status` shows progress only.

## Quota readings

Until the quota calibration, the owner's readings are the authoritative quota state, shared by
every workspace (account-wide, under `~/.token-sparen-bench` or `$TOKEN_SPAREN_BENCH_STATE`).

1. In an interactive Claude Code session, open `/usage` and note the weekly and five-hour
   percentages and their reset times.
2. Record them (the command prints the timestamp it stored):

   ```
   python -m bench quota set --weekly <P> --five-hour <P> \
     --weekly-resets <ISO time> --five-hour-resets <ISO time>
   ```

3. Repeat at least every five hours while a stage runs: a weekly reading older than five hours
   stops the scheduler (exit `3`), as does any reading at or above the weekly stop (60 %). The
   scheduler never sleeps through a weekly reset; it waits for a five-hour reset only with
   `--wait` (otherwise exit `4`).

## The `bench-anchors` branch

The confirmation pushes its chain anchors to a branch of the public repository. Pushing there is
authorised only during real campaigns; dry runs use a temporary local repository.

1. Create the branch from `main` (it must carry `.github/workflows/anchor-receipt.yml`, which
   dates every push server-side):

   ```
   git fetch origin main
   git push origin origin/main:refs/heads/bench-anchors
   ```

2. Protect it before the confirmation: in the repository settings add a branch rule (or ruleset)
   for `bench-anchors` that blocks force pushes and blocks deletion, with no pull-request
   requirement (the CLI pushes one commit per publication). Check after the first push that the
   "Anchor receipt" workflow ran for it.

## Preparation

```
python -m bench prepare dataset
python -m bench prepare toolchain      # checks toolchain/pins.json and grade-venv
python -m bench prepare templates      # checks one config template per arm
python -m bench preflight --tasks pool # gold twice, no-op once, per task
python -m bench prepare images         # pulls the Mini images (no token needed)
python -m bench calibrate --gb-per-image <G> --gb-kept <G> --gb-per-run <G>
```

The toolchain and the per-arm templates are built as described in `bench/README.md`; a missing
piece makes the check commands exit `2` with what is missing. This first calibration only
serves the screening; it is redone after screening.

## Screening

1. Tag the screening runtime with a signed annotated tag and push it:
   `git tag -s v0.5.0-screen1 -m "E4 screening freeze"` then `git push origin v0.5.0-screen1`.
2. Freeze and run (resumable; run it again after any pause):

   ```
   python -m bench freeze --stage screening --ref v0.5.0-screen1
   python -m bench run --stage screening --campaign m5 --model <resolved sonnet id> --wait
   ```

   Exit codes: `0` complete, `3` weekly stop (give a new reading, then resume), `4` five-hour
   wait without `--wait`, `5` incomplete, `6` breaker (look at the ledger, then resume), `7`
   auth (renew the token file), `8` token leak (stop, rotate the token), `9` frozen plugin
   changed, `10` disk floor, `2` a refusal, `11` anchor publication failed.
3. `python -m bench grade --stage screening`, then `analyse --stage screening` and
   `report --stage screening` (private, in the workspace).
4. Calibrate again, so that the observed per-run weekly use (from your readings and the
   screening's run starts) and the screening's retry rate feed the sizing:
   `python -m bench calibrate --gb-per-image <G> --gb-kept <G> --gb-per-run <G>`. `power`
   refuses a calibration older than the screening's last run.
5. `python -m bench power` writes the confirmation spec and verdict and prints the projected
   weeks; decide before going on (an `underpowered` verdict means the owner decides).

## Live pricing check

Before the confirmation, compare the price table of `bench/streamjson.py` with the live pricing
page for the confirmation model (every token class, cache reads and writes included). Update the
table first if anything differs. The date of this check is passed as `--price-confirmed-on`; a
confirmation report without it is never published.

## Confirmation

1. Wait for the M6 pre-freeze tasks, then tag the frozen commit `v1.0.0-rc1` (signed, pushed).
   That commit must already carry version `1.0.0` (the *Freeze* step of
   [runbook.md](runbook.md#cutting-a-release-owner)): the plugin hash includes the version, so
   a bump after the benchmark would no longer match the evidence.
2. Freeze, record the plan, anchor its hash, then run:

   ```
   python -m bench freeze --stage confirmation --ref v1.0.0-rc1
   python -m bench run --stage confirmation --campaign m5 --model <resolved default model id> \
     --price-confirmed-on <YYYY-MM-DD> --plan-only
   # in the MAIN checkout (never the harness): write the printed hash to
   # docs/evidence/plan.sha256, commit it signed, push; then, back in the harness:
   python -m bench run --stage confirmation --campaign m5 --model <resolved default model id> \
     --price-confirmed-on <YYYY-MM-DD> --evidence-repo "<main checkout>" --wait
   ```

   `--plan-only` writes the plan into the ledger, publishes the plan anchor and prints the plan
   hash without starting a run; running it again retries a failed anchor push. The run refuses
   to start until the committed `plan.sha256` equals the recorded plan. It then publishes after
   every graded block, every three hours while it runs or waits, and at the end; a failed push
   (exit `11`) is retried by the next invocation, one commit per push. Give exactly the same
   `--model` and `--price-confirmed-on` on every confirmation invocation: both are pinned in the
   ledger header, and a different value is refused.
3. Pauses need no owner action for the anchors. While the scheduler is alive (running or
   waiting with `--wait`) it publishes a heartbeat at least every four hours. When it has
   stopped (a weekly stop, the breaker, a reboot), it publishes nothing. Resuming later is
   fine: the gate accepts a gap of more than five hours between publications only when the
   publication that ends the gap anchors no additional graded run, and that is always the case
   when nothing ran in between. Runs anchored across such a gap make the gate refuse the
   campaign. `python -m bench anchors publish` remains available for a manual publication.

## Gate

```
python -m bench grade --stage confirmation
python -m bench gate --github-repo <owner>/<name> --evidence-repo "<main checkout>"
```

Without `--release-hash` the gate uses the hash of the frozen confirmation plugin (printed by
`freeze`); give the full hash explicitly only to check another tree, for example
`python scripts/plugin_hash.py` run on the release tree. The gate checks that the anchors
remote is `<owner>/<name>` and that the committed `plan.sha256` equals the plan, fetches
`bench-anchors` first, runs E2 and reads the push receipts from the GitHub API
(set `GH_TOKEN` to raise the API rate limit). Without `--github-repo` the result is advisory and
cannot be published. A `failed` gate means no `v1.0` release; `underpowered` means the owner
decides in `docs/release/gate-decision.toml`.

## Publish

```
export TOKEN_SPAREN_DENYLIST="$(cat "<private folder>/denylist")"   # a 0600 file, never typed
python -m bench report --stage confirmation --publish --repo "<main checkout>"
```

This writes `docs/evidence/e4.json`, the report and its figures into the main checkout (never
the harness). Run the
full battery, commit, push; the release job re-verifies the anchors with server receipts.
Then continue with the release runbook's *Evidence committed* step
([runbook.md](runbook.md#cutting-a-release-owner)): render the README Evidence, record a gate
decision if the verdict is `underpowered`, and run the release-prep step before the signed tag.

## Token revocation

1. Revoke the benchmark token in the claude.ai settings.
2. Delete the token file: `rm "<private folder>/bench-secret/token"`.
3. Record the revocation date in `MEMORY.md` (never the value). Stop pushing to
   `bench-anchors`; keep the branch and its protection, since the published evidence refers to it.
