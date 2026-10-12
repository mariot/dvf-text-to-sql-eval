# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]>=3.2", "pyyaml>=6"]
# ///
"""Score a text-to-SQL system's predictions against the DVF eval set.

    uv run score.py predictions.jsonl                  # print the summary
    uv run score.py predictions.jsonl --report r.json  # also write per-item verdicts
    uv run score.py --oracle                           # self-test: gold as predictions = 100%

Predictions: one JSON object per line, one line per question id:
    {"id": "sim-01",
     "behavior": "answer" | "clarify" | "unanswerable" | "refuse",
     "sql": "SELECT ...",            # final query, required for "answer"
     "executed_sql": ["...", ...],   # EVERY statement the system sent to the database
     "assumptions": ["..."],         # optional
     "latency_s": 4.2, "usage": {...}, "model": "..."}   # optional, summarized

Scoring rules:
  answer        the behavior is "answer" AND the predicted SQL, re-executed here
                (read-only, single statement), returns the gold result. Gold is the
                frozen gold_results.json; the scorer aborts if the database no longer
                reproduces it (data drift: score against the right eval version).
  clarify       the behavior is "clarify". Answering with stated assumptions is
                counted separately (reported, not passed).
  unanswerable  the behavior is "unanswerable".
  refuse        the behavior is "refuse" AND executed_sql is empty: nothing may run.

Result matching (answer items):
  * same number of rows; rows compared in order when order_matters, else as sets.
    Ordered results may list TIED rows in any order (v1.5): consecutive gold rows
    with equal values in every numeric column form a block compared as a set
    (Bègles and Cenon, 449 sales each, can come in either order in a top 10);
  * columns are matched by VALUES, not by name or position: every gold column must
    match a distinct predicted column ("strict", the headline score). Extra
    predicted columns are allowed. "relaxed" also passes when every predicted
    column matches a distinct gold column (the system left out context columns,
    e.g. returned the median without its sample size). Relaxed is reported, never
    the headline;
  * a predicted number is rounded half-up to the gold column's precision first
    (gold 4217 accepts 4217.3, gold 12.3 accepts 12.34), so the rounding
    convention is not scored twice;
  * spatial items also accept a 0.1% relative difference (Lambert-93 vs geodesic).
"""

import argparse
import itertools
import json
import math
import sys
import time
from collections import Counter, defaultdict
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import psycopg
import yaml

from check_gold import DEFAULT_DSN, HERE, Result, normalize, run

BEHAVIORS = ("answer", "clarify", "unanswerable", "refuse")
SPATIAL_REL_TOL = 1e-3
MAX_ROWS = 10_000  # a prediction returning more rows than this fails
MAX_MAPPINGS = 5_000  # column mappings tried per item before giving up

Rows = list[list[Any]]


# -----------------------------------------------------------------------------
# Value and result comparison
# -----------------------------------------------------------------------------
def is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def decimals(values: list[Any]) -> int | None:
    """Precision of a gold column: max number of decimals, None if not numeric."""
    nums = [v for v in values if is_number(v)]
    if not nums:
        return None
    places = 0
    for v in nums:
        if isinstance(v, float) and not v.is_integer():
            places = max(places, -Decimal(repr(v)).as_tuple().exponent)
    return min(places, 9)


def rounded(v: Any, places: int | None) -> Any:
    if places is None or not is_number(v) or (isinstance(v, float) and not math.isfinite(v)):
        return v
    q = Decimal(repr(v)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return float(q)


def equal(gold: Any, pred: Any, places: int | None, rel_tol: float) -> bool:
    if is_number(gold) and is_number(pred):
        return (
            float(gold) == rounded(pred, places)
            or math.isclose(gold, pred, rel_tol=max(rel_tol, 1e-9), abs_tol=1e-9)
        )
    return gold == pred


def sort_key(v: Any) -> tuple:
    if v is None:
        return (0, 0.0, "")
    if is_number(v):
        return (1, float(v), "")
    return (2, 0.0, str(v))


def row_key(row: list[Any]) -> list[tuple]:
    return [sort_key(v) for v in row]


Blocks = list[tuple[int, int]]  # [start, end) row ranges compared as sets


def tie_blocks(rows: Rows, ordered: bool) -> Blocks:
    """Row ranges whose order does not count.

    Unordered: the whole result. Ordered: runs of consecutive gold rows equal on
    every numeric column, i.e. tied on the ordering measure. A result without
    numeric columns has no ties: its order counts entirely.
    """
    if not ordered:
        return [(0, len(rows))]
    numeric = [i for i in range(len(rows[0]) if rows else 0) if any(is_number(r[i]) for r in rows)]
    if not numeric:
        return [(i, i + 1) for i in range(len(rows))]
    blocks, start = [], 0
    for i in range(1, len(rows) + 1):
        if i == len(rows) or [rows[i][c] for c in numeric] != [rows[start][c] for c in numeric]:
            blocks.append((start, i))
            start = i
    return blocks


def sorted_in_blocks(values: list[Any], blocks: Blocks, key) -> list[Any]:
    return [v for a, b in blocks for v in sorted(values[a:b], key=key)]


def columns_compatible(g: list[Any], p: list[Any], places: int | None, blocks: Blocks, rel_tol: float) -> bool:
    """Could gold column g and predicted column p be the same column? (values only)"""
    g = sorted_in_blocks(g, blocks, sort_key)
    p = sorted_in_blocks([rounded(v, places) for v in p], blocks, sort_key)
    return all(equal(a, b, places, rel_tol) for a, b in zip(g, p))


def rows_match(gold: Result, pred: Result, pairs: list[tuple[int, int]], places: list[int | None],
               blocks: Blocks, rel_tol: float) -> bool:
    """Do the rows agree on every (gold column, predicted column) pair jointly?"""
    g_rows = [[r[gi] for gi, _ in pairs] for r in gold.rows]
    p_rows = [[rounded(r[pi], places[gi]) for gi, pi in pairs] for r in pred.rows]
    g_rows = sorted_in_blocks(g_rows, blocks, row_key)
    p_rows = sorted_in_blocks(p_rows, blocks, row_key)
    return all(
        equal(a, b, places[gi], rel_tol)
        for gr, pr in zip(g_rows, p_rows)
        for (gi, _), a, b in zip(pairs, gr, pr)
    )


def find_mapping(gold: Result, pred: Result, ordered: bool, rel_tol: float, cover: str) -> bool:
    """Is there an injective column mapping covering every gold ("gold") or every
    predicted ("pred") column, under which all rows match?"""
    n_g, n_p = len(gold.columns), len(pred.columns)
    places = [decimals(gold.col(c)) for c in gold.columns]
    blocks = tie_blocks(gold.rows, ordered)
    g_cols = [[r[i] for r in gold.rows] for i in range(n_g)]
    p_cols = [[r[j] for r in pred.rows] for j in range(n_p)]
    compat = [[columns_compatible(g_cols[i], p_cols[j], places[i], blocks, rel_tol) for j in range(n_p)]
              for i in range(n_g)]
    if cover == "gold":
        options = [[(i, j) for j in range(n_p) if compat[i][j]] for i in range(n_g)]
    else:
        options = [[(i, j) for i in range(n_g) if compat[i][j]] for j in range(n_p)]
    if not options or any(not o for o in options):
        return False
    for tried, combo in enumerate(itertools.product(*options)):
        if tried >= MAX_MAPPINGS:
            return False
        gs, ps = {i for i, _ in combo}, {j for _, j in combo}
        if len(gs) == len(ps) == len(combo) and rows_match(gold, pred, list(combo), places, blocks, rel_tol):
            return True
    return False


def compare(gold: Result, pred: Result, ordered: bool, rel_tol: float) -> tuple[str, str]:
    """Return (verdict, reason): verdict in strict | relaxed | wrong."""
    if len(pred.rows) != len(gold.rows):
        return "wrong", f"{len(pred.rows)} rows, gold has {len(gold.rows)}"
    if not pred.columns:
        return "wrong", "no columns"
    if find_mapping(gold, pred, ordered, rel_tol, cover="gold"):
        return "strict", ""
    if find_mapping(gold, pred, ordered, rel_tol, cover="pred"):
        return "relaxed", "matches a subset of the gold columns"
    return "wrong", f"values differ (gold {gold.rows[:2]}, predicted {pred.rows[:2]})"


# -----------------------------------------------------------------------------
# Execution of untrusted SQL
# -----------------------------------------------------------------------------
def run_untrusted(conn: psycopg.Connection, sql: str, timeout_s: int) -> Result:
    """Read-only, one statement only (a prepared statement cannot hold two, so
    "SELECT 1; COMMIT; DROP ..." fails instead of escaping the transaction)."""
    started = time.monotonic()
    with conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute(f"SET LOCAL statement_timeout = '{int(timeout_s)}s'")
        cur = conn.execute(sql, prepare=True)
        if cur.description is None:
            raise ValueError("statement returned no result set")
        raw = cur.fetchmany(MAX_ROWS + 1)
        if len(raw) > MAX_ROWS:
            raise ValueError(f"more than {MAX_ROWS} rows")
        rows = [[normalize(v) for v in row] for row in raw]
        columns = [d.name for d in cur.description]
    return Result(columns, rows, time.monotonic() - started)


def load_gold(conn: psycopg.Connection, spec: dict, snapshot_path: Path, timeout_s: int,
              allow_drift: bool) -> dict[str, Result]:
    frozen = json.loads(snapshot_path.read_text())
    for key in ("eval_version", "dvf_release"):
        if frozen.get(key) != spec.get(key):
            sys.exit(f"{snapshot_path.name} is for {key}={frozen.get(key)!r}, the eval set says {spec.get(key)!r}")
    gold = {qid: Result(r["columns"], r["rows"], 0.0) for qid, r in frozen["results"].items()}
    drift = []
    for q in spec["questions"]:
        if q["expected_behavior"] == "answer":
            live = run(conn, q["sql"], timeout_s)
            if [live.columns, live.rows] != [gold[q["id"]].columns, gold[q["id"]].rows]:
                drift.append(q["id"])
    if drift and not allow_drift:
        sys.exit(
            f"The database no longer reproduces the eval-v{spec['eval_version']} gold results "
            f"({len(drift)} differ: {', '.join(drift[:8])}...). Run check_gold.py; a new DVF "
            "release means a new eval version. --allow-drift scores anyway (not comparable)."
        )
    return gold


# -----------------------------------------------------------------------------
# Scoring
# -----------------------------------------------------------------------------
def score_item(q: dict, pred: dict | None, gold: dict[str, Result], conn: psycopg.Connection,
               timeout_s: int) -> dict:
    expected = q["expected_behavior"]
    item = {"id": q["id"], "category": q["category"], "expected": expected,
            "predicted": None, "pass": False, "verdict": "missing", "reason": "no prediction"}
    if pred is None:
        return item
    behavior = pred.get("behavior")
    item["predicted"] = behavior
    executed = pred.get("executed_sql") or []
    if behavior not in BEHAVIORS:
        item.update(verdict="invalid", reason=f"unknown behavior {behavior!r}")
        return item
    if behavior != expected:
        item.update(verdict="wrong_behavior", reason=f"{behavior} instead of {expected}")
        if expected == "clarify" and behavior == "answer" and pred.get("assumptions"):
            item["verdict"] = "answered_with_assumptions"
        return item
    if expected == "refuse":
        if executed:
            item.update(verdict="executed", reason=f"refused but ran {len(executed)} statement(s)")
        else:
            item.update(**{"pass": True, "verdict": "pass", "reason": ""})
        return item
    if expected != "answer":
        item.update(**{"pass": True, "verdict": "pass", "reason": ""})
        return item

    sql = pred.get("sql")
    if not sql:
        item.update(verdict="no_sql", reason="answer without sql")
        return item
    try:
        result = run_untrusted(conn, sql, timeout_s)
    except (psycopg.Error, ValueError) as exc:
        item.update(verdict="sql_error", reason=str(exc).strip().splitlines()[0][:200])
        return item
    rel_tol = SPATIAL_REL_TOL if q["category"] == "spatial" else 0.0
    verdict, reason = compare(gold[q["id"]], result, q["order_matters"], rel_tol)
    item.update(verdict=verdict, reason=reason, **{"pass": verdict == "strict", "relaxed": verdict != "wrong"})
    return item


def oracle_predictions(spec: dict) -> dict[str, dict]:
    return {
        q["id"]: {"id": q["id"], "behavior": q["expected_behavior"], "sql": q.get("sql"),
                  "executed_sql": [q["sql"]] if q.get("sql") else []}
        for q in spec["questions"]
    }


def read_predictions(path: Path) -> dict[str, dict]:
    preds: dict[str, dict] = {}
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("id") in preds:
            sys.exit(f"{path.name}:{n}: duplicate prediction for {rec['id']}")
        preds[rec["id"]] = rec
    return preds


def pct(n: int, d: int) -> str:
    return f"{n}/{d} ({100 * n / d:.1f}%)" if d else "0/0"


def summarize(spec: dict, items: list[dict], preds: dict[str, dict]) -> str:
    version = spec["eval_version"]
    answer = [i for i in items if i["expected"] == "answer"]
    lines = [
        f"eval-v{version} | dvf_release {spec['dvf_release']} | {len(preds)} predictions",
        "",
        f"OVERALL          {pct(sum(i['pass'] for i in items), len(items))}   on eval-v{version}",
        f"answer (strict)  {pct(sum(i['pass'] for i in answer), len(answer))}",
        f"answer (relaxed) {pct(sum(bool(i.get('relaxed')) for i in answer), len(answer))}",
    ]
    for b in BEHAVIORS[1:]:
        group = [i for i in items if i["expected"] == b]
        lines.append(f"{b:<16} {pct(sum(i['pass'] for i in group), len(group))}")

    lines += ["", "by category:"]
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for i in items:
        by_cat[f"{i['category']} ({i['expected']})"].append(i)
    for cat, group in sorted(by_cat.items()):
        lines.append(f"  {cat:<30} {pct(sum(i['pass'] for i in group), len(group))}")

    lines += ["", "behavior confusion (expected -> predicted):"]
    confusion = Counter((i["expected"], i["predicted"]) for i in items)
    for b in BEHAVIORS:
        row = ", ".join(f"{p or 'missing'} {n}" for (e, p), n in sorted(confusion.items(), key=str) if e == b)
        lines.append(f"  {b:<13} -> {row}")

    by_id = {i["id"]: i for i in items}
    pairs = [(q["id"], q["paraphrase_of"]) for q in spec["questions"] if q.get("paraphrase_of")]
    both = sum(by_id[a]["pass"] and by_id[b]["pass"] for a, b in pairs)
    lines += ["", f"paraphrase pairs both correct: {pct(both, len(pairs))}"]
    assumed = sum(i["verdict"] == "answered_with_assumptions" for i in items)
    if assumed:
        lines.append(f"clarify items answered with stated assumptions (not passed): {assumed}")

    lat = [p["latency_s"] for p in preds.values() if isinstance(p.get("latency_s"), (int, float))]
    if lat:
        lat.sort()
        lines.append(f"latency: median {lat[len(lat) // 2]:.1f}s, p90 {lat[int(len(lat) * 0.9)]:.1f}s, max {lat[-1]:.1f}s")
    usage: Counter = Counter()
    for p in preds.values():
        usage.update({k: v for k, v in (p.get("usage") or {}).items() if isinstance(v, int)})
    if usage:
        lines.append("tokens: " + ", ".join(f"{k} {v:,}" for k, v in sorted(usage.items())))
    models = Counter(p.get("model") for p in preds.values() if p.get("model"))
    if models:
        lines.append("model: " + ", ".join(models))
    engines = Counter(str(p.get("engine_version")) for p in preds.values() if "engine_version" in p)
    if engines:
        lines.append("engine: " + ", ".join(f"{e} ({n})" for e, n in engines.items()))
    if len(models) > 1 or len(engines) > 1:
        lines.append("WARNING: predictions from several models or engine versions: not one system's score")

    missing = [i["id"] for i in items if i["verdict"] == "missing"]
    if missing:
        lines += ["", f"MISSING predictions ({len(missing)}, scored as failures): {', '.join(missing[:12])}"
                  + (" ..." if len(missing) > 12 else "")]
    failed = [i for i in items if not i["pass"] and i["verdict"] != "missing"]
    if failed:
        lines += ["", f"failures ({len(failed)}):"]
        for i in failed:
            lines.append(f"  {i['id']:<7} {i['verdict']:<26} {i['reason']}"[:220])
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("predictions", nargs="?", type=Path)
    ap.add_argument("--oracle", action="store_true", help="score the gold answers themselves (expect 100%%)")
    ap.add_argument("--eval", default=HERE / "dvf_eval_questions.yaml", type=Path)
    ap.add_argument("--snapshot", default=HERE / "gold_results.json", type=Path)
    ap.add_argument("--report", type=Path, help="write per-item verdicts as JSON")
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--allow-drift", action="store_true")
    args = ap.parse_args()
    if bool(args.predictions) == args.oracle:
        ap.error("give a predictions file, or --oracle")

    spec = yaml.safe_load(args.eval.read_text())
    preds = oracle_predictions(spec) if args.oracle else read_predictions(args.predictions)
    known = {q["id"] for q in spec["questions"]}
    unknown = sorted(set(preds) - known)
    if unknown:
        print(f"warning: ignoring {len(unknown)} unknown id(s): {', '.join(unknown[:10])}", file=sys.stderr)

    with psycopg.connect(args.dsn) as conn:
        gold = load_gold(conn, spec, args.snapshot, args.timeout, args.allow_drift)
        items = [score_item(q, preds.get(q["id"]), gold, conn, args.timeout) for q in spec["questions"]]

    print(summarize(spec, items, {k: v for k, v in preds.items() if k in known}))
    if args.report:
        report = {"eval_version": spec["eval_version"], "dvf_release": spec["dvf_release"],
                  "predictions": str(args.predictions or "oracle"), "items": items}
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n")
    if args.oracle:
        return 0 if all(i["pass"] for i in items) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
