"""Run the offline LinTS challenger (``lints``) through the replay's simulation lane.

The candidate learner of the ``sim`` lane is replaced by LinTS (one model per project group,
in memory, updated once per trimming decision when its proxy regret settles, together with the
savings the hook would have rewarded); the reference stays the shipped engine with current
defaults, on the same outputs. Not like-for-like: LinTS has no circuit breaker, exploration
floor or safe-exploration ladder, and logs propensity 1. Informational only (M4 Task 11
compares LinTS and the engine on dev); promotion is out of scope and LinTS is never wired to
hooks.

    python evals/challengers/lints_sim.py <transcripts...> --out PRIVATE.json [--split dev]

Real-data runs follow the M4 rules: the report goes through ``replay.report.write_private``
(outside any git work tree) and stdout is only ``report: <path> id: <12 hex>``.
"""

from __future__ import annotations

import argparse
import random
import sys
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # `python evals/challengers/lints_sim.py` from a checkout
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evals.challengers.lints import DIM, LinTS, encode
from token_sparen.config import Config, packaged_defaults
from token_sparen.events import SessionCtx
from token_sparen.learn.breaker import tau_for
from token_sparen.learn.policy import COMPRESSION, Decision, DecisionPoint
from token_sparen.replay import report, runner, sim_lane

FACTORY = "evals.challengers.lints_sim:make_chooser"
DEV_CUTOFF = date(2026, 11, 8)  # the M4 split cutoff
BUCKET = "lints"


class LinTSChooser:
    """``sim_lane.Chooser`` backed by ``LinTS`` (compression point only; any other point gets
    its safe arm)."""

    def __init__(self, cfg: Config, seed: str) -> None:
        self.model = LinTS(
            COMPRESSION.arms, COMPRESSION.safe_arm, tau_for(COMPRESSION, cfg.data), DIM
        )
        self.rng = random.Random(f"lints|{seed}")
        self._n = 0
        self._pending: dict[str, list[Any]] = {}  # decision id -> [arm, x, savings, regret]

    def start_session(self, session_id: str, project_key: str, now: float) -> None:
        return None

    def decide(
        self, point: DecisionPoint, features: Mapping[str, str], session: SessionCtx
    ) -> Decision:
        self._n += 1
        did = f"lints{self._n}"
        if point.name != COMPRESSION.name:
            return Decision(did, point.name, point.safe_arm, 1.0, BUCKET)
        x = encode(features)
        arm = self.model.choose(x, self.rng)
        if arm != COMPRESSION.safe_arm:
            self._pending[did] = [arm, x, None, None]
        return Decision(did, point.name, arm, 1.0, BUCKET)

    def reward(self, decision: Decision, savings: float | None, regret: float | None) -> None:
        """Savings and regret arrive separately, in either order (the lane delivers savings
        once the re-sends are known); the model is updated once both are in."""
        entry = self._pending.get(decision.decision_id)
        if entry is None:
            return
        if savings is not None and entry[2] is None:
            entry[2] = savings
        if regret is not None and entry[3] is None:
            entry[3] = regret
        if entry[2] is not None and entry[3] is not None:
            del self._pending[decision.decision_id]
            self.model.update(entry[0], entry[1], entry[2], entry[3])

    def outcome(self, decision: Decision, value: float, signal: str) -> None:
        return None  # no circuit breaker around the challenger

    def end_session(self) -> list[str]:
        return []


def make_chooser(cfg: Config, env: Mapping[str, str], home: Path, seed: str) -> LinTSChooser:
    """``sim_lane`` chooser factory (``FACTORY``)."""
    return LinTSChooser(cfg, seed)


def run(
    paths: Sequence[Path],
    *,
    seed: int = 0,
    cutoff: date = DEV_CUTOFF,
    split: str = "all",
    cfg: Config | None = None,
    jobs: int = 1,
    overlay: Mapping[str, Any] | None = None,
) -> runner.RunResult:
    """The ``sim`` lane with LinTS as the candidate (and the shipped engine as the reference)."""
    params: dict[str, Any] = {"seed": seed, "chooser_factory": FACTORY}
    if overlay:
        params["overlay"] = dict(overlay)
    return runner.run_parallel(
        list(paths),
        [("sim", params)],
        jobs=jobs,
        cutoff=cutoff,
        split=split,
        cfg=cfg or Config(data=packaged_defaults()),  # ignored by the lane (packaged only)
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="+", type=Path, help="transcript files or directories")
    ap.add_argument("--out", type=Path, required=True, help="private report (outside any repo)")
    ap.add_argument("--split", choices=["dev", "test", "all"], default="dev")
    ap.add_argument("--cutoff", default=DEV_CUTOFF.isoformat(), help="YYYY-MM-DD")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--quiet", action="store_true", help="the only mode: path and id")
    args = ap.parse_args(argv)
    out = report.check_out_path(args.out)
    res = run(
        args.paths,
        seed=args.seed,
        cutoff=date.fromisoformat(args.cutoff),
        split=args.split,
        jobs=max(1, args.jobs),
    )
    body = {
        "version": 1,
        "kind": "e1-sim-lints",
        "split": args.split,
        "cutoff": args.cutoff,
        "summary": {p: sim_lane.summary(rows.get("sim", {})) for p, rows in res.by_split.items()},
        "pairs": {p: sim_lane.pairs(rows.get("sim", {})) for p, rows in res.by_split.items()},
        "run": res.counters(),
    }
    report.write_private(out, body)
    print(report.quiet_line(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
