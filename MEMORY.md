# MEMORY: decision log

Append-only. One entry per decision: date, decision, reason. Newest at the bottom.

## 2026-11-25 Owner decisions (spec section 17)
- **Approach A**: one learning engine drives all mechanisms (not two bolted-together optimizers).
- **Benchmarks are Claude-Code-only**, run under a Max 5x plan, calendar time unconstrained;
  scheduler respects 5-hour windows and the weekly cap.
- **Dogfooding**: about 2 weeks of real use to collect decision logs with propensities, needed for
  off-policy evaluation (E3).
- **Quality gate**: non-inferiority on resolved rate with a -5 percentage point margin; exact CI is
  always published; release is blocked if the gate fails.
- **Scope**: v1.0 = Claude Code complete; v1.1 = other hosts (Codex, Gemini CLI, Qwen, OpenCode).
  Clients without hooks are deferred.
- **Live statusline is required** (zero model tokens).

## 2026-11-25 Amendment
- **Python floor is 3.11** (spec said 3.10): 3.10 reached end-of-life in Oct 2026 and `tomllib`
  is only in 3.11+. Runtime remains stdlib only.

## 2026-11-25 Designer calls (spec section 17; owner to check)
- Python stdlib core plus a thin TypeScript shim where a host needs one.
- Gemini CLI supported; Antigravity deferred.
- Output-style directives off by default.
- LLM side-calls off by default.
- Tool pruning needs opt-in on Claude Code because it writes settings.
- Coverage gate >= 90 %.
- CLI name `token-sparen`.

## 2026-11-25 Process
- Development follows Elephant-Goldfish (spec section 13); see `.claude/skills/egm-cycle`.
- Private inputs (owner's local sessions, E1 results) stay outside the repo and are only referred to generically.

## 2026-11-25 Text compressor (Task 7)
- Error lines (ERROR_PATTERNS) never take part in a fold run, so folding can never hide one.
- Digit-run normalisation for folding uses a marker byte that cannot occur in UTF-8, so a literal
  `#` is not treated as a number.
- The case-insensitive error regex folds ASCII case only; whole-text scanning uses literal byte
  anchors plus per-line regex verification (exactly equivalent, several times faster).
- `CompressResult.text` excludes the recovery marker; elided gaps show as `… [N lines elided]`.
