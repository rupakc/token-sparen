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
python -m bench prepare images         # pulls the Mini images (needs the token file)
python -m bench preflight --tasks pool # gold twice, no-op once, per task
python -m bench calibrate --gb-per-image <G> --gb-kept <G> --gb-per-run <G>
```

The toolchain and the per-arm templates are built as described in `bench/README.md`; a missing
piece makes the check commands exit `2` with what is missing.

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
4. `python -m bench power` writes the confirmation spec and verdict and prints the projected
   weeks; decide before going on (an `underpowered` verdict means the owner decides).

## Live pricing check

Before the confirmation, compare the price table of `bench/streamjson.py` with the live pricing
page for the confirmation model (every token class, cache reads and writes included). Update the
table first if anything differs. The date of this check is passed as `--price-confirmed-on`; a
confirmation report without it is never published.

## Confirmation

1. Wait for the M6 pre-freeze tasks, then tag the frozen commit `v1.0.0-rc1` (signed, pushed).
2. Freeze, record the plan, anchor its hash, then run:

   ```
   python -m bench freeze --stage confirmation --ref v1.0.0-rc1
   python -m bench run --stage confirmation --campaign m5 --model <resolved default model id> \
     --price-confirmed-on <YYYY-MM-DD> --plan-only
   # commit the printed hash as docs/evidence/plan.sha256 in a signed commit, push, then:
   python -m bench run --stage confirmation --campaign m5 --model <resolved default model id> \
     --price-confirmed-on <YYYY-MM-DD> --wait
   ```

   `--plan-only` writes the plan into the ledger, publishes the plan anchor and prints the plan
   hash without starting a run. The run then publishes after every graded block, every three
   hours while it runs or waits, and at the end.
3. While the campaign is paused (a weekly stop, the breaker, a reboot), publish a heartbeat at
   least every four hours, for example from a scheduled job:
   `python -m bench anchors publish`. A gap of more than five hours between publications makes
   the gate refuse the campaign.

## Gate

```
python -m bench grade --stage confirmation
python -m bench gate --release-hash <plugin hash of the RC> --github-repo <owner>/<name>
```

The gate fetches `bench-anchors` first, runs E2 and reads the push receipts from the GitHub API
(set `GH_TOKEN` to raise the API rate limit). Without `--github-repo` the result is advisory and
cannot be published. A `failed` gate means no `v1.0` release; `underpowered` means the owner
decides in `docs/release/gate-decision.toml`.

## Publish

```
TOKEN_SPAREN_DENYLIST="<private words>" python -m bench report --stage confirmation --publish
```

This writes `docs/evidence/e4.json`, the report and its figures into the repository. Run the
full battery, commit, push; the release job re-verifies the anchors with server receipts.

## Token revocation

1. Revoke the benchmark token in the claude.ai settings.
2. Delete the token file: `rm "<private folder>/bench-secret/token"`.
3. Record the revocation date in `MEMORY.md` (never the value). Stop pushing to
   `bench-anchors`; keep the branch and its protection, since the published evidence refers to it.
