---
name: egm-cycle
description: Use when starting any non-trivial feature, milestone or spec change, to run the Elephant-Goldfish draft, review, implement, drift cycle.
---
# Elephant-Goldfish cycle
1. **Draft (elephant)**: write or amend the spec/plan. No code. Include acceptance criteria and failure modes.
2. **Review (goldfish)**: dispatch `goldfish-reviewer` with only the document path. A fresh,
   zero-context reviewer reads it cold and returns ranked gaps. Use a new reviewer each round.
3. **Resolve**: fix every blocker and major gap in the document; record rejected ones with a reason.
   Repeat 2-3 until a cold reviewer finds no blockers.
4. **Implement**: TDD. Failing test first, minimal code, run the full check set from `CLAUDE.md`.
5. **Drift report**: run `drift-checker` against the spec sections; fix code or amend spec, noting
   decisions in `MEMORY.md`.
6. **Self-review** the diff, run `python scripts/check_denylist.py`, then commit.
