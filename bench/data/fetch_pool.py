"""Regenerate `bench/data/pool.jsonl` and fetch the prepare-time dataset (dev-time, network).

Never run by the tests or by CI. Two commands:

* ``python -m bench.data.fetch_pool pool`` rewrites `pool.jsonl`: one row per SWE-bench Verified
  task whose prebuilt arm64 evaluation image exists on Docker Hub
  (``swebench/sweb.eval.arm64.<id with "__" → "_1776_">`` with a ``latest`` tag), flagged
  ``mini`` when the task is in SWE-bench Verified Mini. The header records the Hugging Face
  revisions of both datasets, the fetch date and the Mini tasks left out for want of an image.
  No problem statement, hint or patch is committed.
* ``python -m bench.data.fetch_pool dataset --workspace W`` writes the rows of the pooled tasks
  (problem statements, gold and test patches) to ``W/dataset.jsonl`` and their revision and
  sha256 to ``W/dataset.meta.json``; `bench.tasks.load_dataset_rows` re-checks both. The
  revision must equal the pool header's (a moved dataset is refused, not silently mixed).

Rows come from the Hugging Face datasets-server rows API, which serves the current revision
only, so the hub revision is read before and after the download and a change in between is
refused.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # `python bench/data/fetch_pool.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bench.tasks import (
    DATASET_FILE,
    DATASET_META_FILE,
    POOL_FILE,
    arm64_image,
    read_pool_header,
    write_dataset_rows,
)

VERIFIED = "SWE-bench/SWE-bench_Verified"
MINI = "MariusHobbhahn/swe-bench-verified-mini"
HUB_API = "https://huggingface.co/api/datasets/"
ROWS_API = "https://datasets-server.huggingface.co/rows"
DOCKER_REPOS = "https://hub.docker.com/v2/namespaces/swebench/repositories"
PAGE = 100
POOL_KEYS = ("instance_id", "repo", "base_commit", "created_at")
USER_AGENT = "token-sparen-bench"

Fetch = Callable[[str], Any]


def http_json(url: str, *, attempts: int = 6) -> Any:
    """GET ``url`` as JSON; throttling (403/429) and 5xx are retried with backoff."""
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 (fixed hosts)
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code not in (403, 429) and exc.code < 500 or attempt == attempts - 1:
                raise
            time.sleep(min(60.0, 2.0 ** (attempt + 1)))
    raise AssertionError("unreachable")


def hub_revision(dataset: str, fetch: Fetch = http_json) -> str:
    sha = fetch(HUB_API + dataset)["sha"]
    if not isinstance(sha, str) or len(sha) != 40:
        raise RuntimeError(f"unexpected revision for {dataset}")
    return sha


def dataset_rows(dataset: str, fetch: Fetch = http_json) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset, total = 0, None
    while total is None or offset < total:
        query = urllib.parse.urlencode(
            {"dataset": dataset, "config": "default", "split": "test", "offset": offset}
        )
        page = fetch(f"{ROWS_API}?{query}&length={PAGE}")
        total = int(page["num_rows_total"])
        got = [r["row"] for r in page["rows"]]
        if not got:
            raise RuntimeError(f"{dataset}: empty page at offset {offset} of {total}")
        rows += got
        offset += len(got)
    return rows


def pinned_rows(dataset: str, fetch: Fetch = http_json) -> tuple[str, list[dict[str, Any]]]:
    """(revision, rows); refused when the dataset moved during the download."""
    before = hub_revision(dataset, fetch)
    rows = dataset_rows(dataset, fetch)
    if hub_revision(dataset, fetch) != before:
        raise RuntimeError(f"{dataset} changed during the download; retry")
    return before, rows


def arm64_repositories(instance_ids: Iterable[str], fetch: Fetch = http_json) -> set[str]:
    """Names of the arm64 evaluation images (of these tasks) that carry a ``latest`` tag.

    One tag lookup per task: Docker Hub refuses to page a repository listing past the first
    page for anonymous clients. Only a 404 means "no image"; any other error stops the fetch.
    """
    names = (arm64_image(i).split("/", 1)[1].removesuffix(":latest") for i in instance_ids)
    return {n for n in sorted(names) if _has_latest(n, fetch)}


def _has_latest(name: str, fetch: Fetch) -> bool:
    try:
        return bool(fetch(f"{DOCKER_REPOS}/{name}/tags/latest").get("name") == "latest")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise


def build_pool(
    verified: Iterable[Mapping[str, Any]],
    mini_ids: Iterable[str],
    images: set[str],
    *,
    header: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Header plus one row per Verified task with an arm64 image (pure; tested offline)."""
    mini = set(mini_ids)
    rows: list[dict[str, Any]] = []
    for task in sorted(verified, key=lambda t: str(t["instance_id"])):
        iid = str(task["instance_id"])
        image = arm64_image(iid)
        if image.split("/", 1)[1].removesuffix(":latest") not in images:
            continue
        row = {k: str(task[k]) for k in POOL_KEYS}
        rows.append({**row, "image": image, "mini": iid in mini})
    pooled = {r["instance_id"] for r in rows}
    head = dict(header)
    head["mini_without_arm64"] = sorted(mini - pooled)
    head["verified_without_arm64"] = sum(1 for t in verified_ids(verified) if t not in pooled)
    return [head, *rows]


def verified_ids(verified: Iterable[Mapping[str, Any]]) -> list[str]:
    return [str(t["instance_id"]) for t in verified]


def dump_jsonl(rows: Sequence[Mapping[str, Any]]) -> str:
    return "".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in rows)


def cmd_pool(out: Path, fetch: Fetch = http_json) -> int:
    v_rev, verified = pinned_rows(VERIFIED, fetch)
    m_rev, mini = pinned_rows(MINI, fetch)
    unknown = {r["instance_id"] for r in mini} - {r["instance_id"] for r in verified}
    if unknown:
        raise RuntimeError(f"Mini tasks missing from Verified: {sorted(unknown)}")
    header = {
        "verified_revision": v_rev,
        "mini_revision": m_rev,
        "fetched": dt.date.today().isoformat(),
    }
    rows = build_pool(
        verified,
        (r["instance_id"] for r in mini),
        arm64_repositories(verified_ids(verified), fetch),
        header=header,
    )
    out.write_text(dump_jsonl(rows), encoding="utf-8", newline="\n")
    print(f"{len(rows) - 1} tasks written to {out.name}", file=sys.stderr)
    return 0


def cmd_dataset(workspace: Path, pool: Path = POOL_FILE, fetch: Fetch = http_json) -> int:
    from bench.isolation import check_workspace
    from bench.tasks import load_pool

    workspace = check_workspace(workspace)
    want = read_pool_header(pool)["verified_revision"]
    revision, rows = pinned_rows(VERIFIED, fetch)
    if revision != want:
        raise RuntimeError(
            f"{VERIFIED} moved ({revision[:12]} != pool {want[:12]}); regenerate the pool first"
        )
    ids = {t.instance_id for t in load_pool(pool)}
    write_dataset_rows(workspace, [r for r in rows if r["instance_id"] in ids], revision)
    print(f"{DATASET_FILE} and {DATASET_META_FILE} written", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fetch_pool", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_pool = sub.add_parser("pool", help="regenerate bench/data/pool.jsonl")
    p_pool.add_argument("--out", type=Path, default=POOL_FILE)
    p_data = sub.add_parser("dataset", help="fetch dataset rows into the bench workspace")
    p_data.add_argument(
        "--workspace", type=Path, default=os.environ.get("TOKEN_SPAREN_BENCH_OUT"), required=False
    )
    args = parser.parse_args(argv)
    if args.cmd == "pool":
        return cmd_pool(args.out)
    if args.workspace is None:
        parser.error("--workspace (or TOKEN_SPAREN_BENCH_OUT) is required")
    return cmd_dataset(Path(args.workspace))


if __name__ == "__main__":
    raise SystemExit(main())
