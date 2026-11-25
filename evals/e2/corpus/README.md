# E2 corpus provenance

Every file here is synthetic: generated deterministically (seeded) from templates that imitate the
shape of real tool output (pytest, jest, go test, cargo, gcc/clang/rustc/tsc/javac, npm/pip/docker/
gradle, grep/rg, `pplx` JSON, logs, CSV/TSV tables, diffs, stack traces, prose). No real project
output, user data, or third-party text is included, so the corpus carries the repository MIT licence.

Cases are declared in `../needles.toml`:

- `file`: corpus file. `command` (optional): shell command used as the detection hint.
- `types`: expected `detect()` result (asserted, so routing regressions are caught).
- `needles`: facts that must survive: present in the compressed text or, unless also listed in
  `must_inline`, in the recovery file the marker points at.
- `must_inline`: needles that must appear in the compressed text itself (failing test ids, error
  lines, compiler diagnostics, URLs in `pplx`, file names in search results).

Adversarial cases: error line buried mid-output, needle inside a folded repetitive run, javac
snippet at column 0, project frame at `/srv/app/java.py`, `pplx` error result beyond top-k, grep
match in the 50th file, JSON value matching the error patterns deep in an array.
