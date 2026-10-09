# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]>=3.2", "pyyaml>=6"]
# ///
"""Run and sanity-check every gold query of the DVF text-to-SQL eval set.

    uv run check_gold.py                       # run all checks
    uv run check_gold.py --write-snapshot      # (re)freeze gold_results.json

Checks, each one a flag (exit code 1 if any):
  structure    ids unique, `answer` items have SQL without "-- TODO", others have none
  execution    error, zero rows, or slower than --slow seconds (read-only transaction)
  paraphrase   a paraphrase returns exactly its reference question's result
  plausible    EUR/m2 statistics fall in a plausible range for Gironde
  consistency  results that must agree with each other do (Bordeaux < Gironde...)
  crosscheck   an independent query (cross_checks.yaml) gives the same answer
  snapshot     results equal the frozen gold_results.json of this eval version
"""

import argparse
import datetime as dt
import json
import math
import os
import re
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import yaml

HERE = Path(__file__).parent
DEFAULT_DSN = os.environ.get("DVF_DSN", "postgresql://dvf:dvf@localhost:5433/dvf")

# Column names holding a EUR/m2 statistic (median, mean, quartile) in gold results.
PRICE_COLUMN = re.compile(r"^(prix_m2_(median|moyen|carrez_median)|q[13]|med_\d{4})$")
PRICE_RANGE = (500, 20_000)  # Gironde 2021-2025: communes range ~1 200 to ~13 000 EUR/m2

Rows = list[list[Any]]


class Result:
    def __init__(self, columns: list[str], rows: Rows, seconds: float):
        self.columns, self.rows, self.seconds = columns, rows, seconds

    def col(self, name: str) -> list[Any]:
        i = self.columns.index(name)
        return [r[i] for r in self.rows]

    def scalar(self) -> Any:
        return self.rows[0][0]

    def where(self, key: str, value: Any, col: str) -> Any:
        return next(r[self.columns.index(col)] for r in self.rows if r[self.columns.index(key)] == value)


def normalize(value: Any) -> Any:
    """JSON-friendly and comparison-friendly values."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


def run(conn: psycopg.Connection, sql: str, timeout_s: int) -> Result:
    started = time.monotonic()
    with conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute(f"SET LOCAL statement_timeout = '{int(timeout_s)}s'")
        cur = conn.execute(sql)
        rows = [[normalize(v) for v in row] for row in cur.fetchall()]
        columns = [d.name for d in cur.description]
    return Result(columns, rows, time.monotonic() - started)


def canonical(rows: Rows, order_matters: bool) -> Rows:
    return rows if order_matters else sorted(rows, key=lambda r: json.dumps(r, default=str))


def close(a: Any, b: Any, rel: float) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=rel, abs_tol=1e-9)
    return a == b


def same_rows(a: Rows, b: Rows, rel: float = 0.0) -> bool:
    return len(a) == len(b) and all(
        len(x) == len(y) and all(close(u, v, rel) for u, v in zip(x, y)) for x, y in zip(a, b)
    )


# -----------------------------------------------------------------------------
# Consistency rules: (description, function of the results dict -> bool)
# -----------------------------------------------------------------------------
def consistency_rules(R: dict[str, Result]) -> list[tuple[str, Any]]:
    bdx_2024 = lambda: R["sec-08"].scalar()  # noqa: E731  Bordeaux sales 2024
    gironde_2024 = lambda: R["sim-01"].scalar()  # noqa: E731
    return [
        ("Bordeaux 2024 sales < Gironde 2024 sales", lambda: bdx_2024() < gironde_2024()),
        ("Bordeaux monthly sales 2024 add up to the yearly count", lambda: sum(R["tmp-03"].col("nb_ventes")) == bdx_2024()),
        ("December in the monthly series = the December question", lambda: R["tmp-03"].where("mois", 12, "nb_ventes") == R["tmp-10"].scalar()),
        ("Top commune of rnk-01 is Bordeaux with the Bordeaux count", lambda: R["rnk-01"].rows[0] == ["Bordeaux", bdx_2024()]),
        ("Gironde yearly series contains the 2024 count", lambda: R["tmp-13"].where("annee", 2024, "nb_ventes") == gironde_2024()),
        ("Last-three-years series = tail of the yearly series", lambda: R["tmp-17"].rows == [r[:2] for r in R["tmp-13"].rows[-3:]]),
        ("Bordeaux share = Bordeaux / Gironde", lambda: R["mul-01"].scalar() == round(100 * bdx_2024() / gironde_2024(), 1)),
        ("Bordeaux 2025 sales < Gironde 2025 sales", lambda: R["tmp-15"].scalar() < R["tmp-13"].where("annee", 2025, "nb_ventes")),
        ("Bordeaux VEFA 2024 <= Bordeaux sales 2024", lambda: R["sim-04"].scalar() <= bdx_2024()),
        ("Bordeaux VEFA 2024 <= Gironde VEFA 2024", lambda: R["sim-04"].scalar() <= R["tmp-11"].where("annee", 2024, "nb_vefa")),
        ("Sales without coordinates <= all sales (2024)", lambda: R["spa-07"].scalar() <= gironde_2024()),
        ("Bordeaux apartment median 2024 identical in agg-02, agg-20, tmp-01", lambda: R["agg-02"].scalar() == R["agg-20"].where("nom_commune", "Bordeaux", "prix_m2_median") == R["tmp-01"].where("annee", 2024, "prix_m2_median")),
        ("Last 12 months (data ends 2025-12-31) = calendar 2025", lambda: R["tmp-16"].rows[0] == [R["tmp-01"].where("annee", 2025, "prix_m2_median"), R["tmp-01"].where("annee", 2025, "nb_ventes")]),
        ("mul-05 = the 2024 slice of mul-15", lambda: sorted(R["mul-05"].rows) == sorted(r[1:] for r in R["mul-15"].rows if r[0] == 2024)),
        ("CAGR consistent with the 2021 and 2025 medians", lambda: abs(R["mul-11"].scalar() - 100 * ((R["tmp-01"].where("annee", 2025, "prix_m2_median") / R["tmp-01"].where("annee", 2021, "prix_m2_median")) ** 0.25 - 1)) < 0.05),
        ("Q1 <= Q3", lambda: R["agg-17"].rows[0][0] <= R["agg-17"].rows[0][1]),
        ("Most expensive Bassin commune = most expensive Gironde commune (houses 2024)", lambda: R["rnk-09"].rows[0] == R["rnk-02"].rows[0]),
        ("Apartment sales fell 2022 -> 2023 consistently with the sign", lambda: (R["tmp-05"].rows[0][2] < 0) == (R["tmp-05"].rows[0][1] < R["tmp-05"].rows[0][0])),
        ("Mérignac houses sold 2024 (sim-03) = paraphrase par-05", lambda: R["sim-03"].scalar() == R["par-05"].scalar()),
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--eval", default=HERE / "dvf_eval_questions.yaml", type=Path)
    ap.add_argument("--cross-checks", default=HERE / "cross_checks.yaml", type=Path)
    ap.add_argument("--snapshot", default=HERE / "gold_results.json", type=Path)
    ap.add_argument("--write-snapshot", action="store_true", help="freeze current results")
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--slow", type=float, default=3.0, help="flag queries slower than this (s)")
    ap.add_argument("--timeout", type=int, default=30)
    args = ap.parse_args()

    spec = yaml.safe_load(args.eval.read_text())
    questions = spec["questions"]
    by_id = {q["id"]: q for q in questions}
    flags: list[tuple[str, str, str]] = []

    def flag(check: str, qid: str, msg: str) -> None:
        flags.append((check, qid, msg))

    # ---- structure ------------------------------------------------------------
    if len(by_id) != len(questions):
        flag("structure", "-", "duplicate ids")
    if not spec.get("dvf_release"):
        flag("structure", "-", "dvf_release is not set")
    for q in questions:
        sql = q.get("sql")
        if q["expected_behavior"] == "answer":
            if not sql or "-- TODO" in sql:
                flag("structure", q["id"], "missing SQL or still contains -- TODO")
            if "order_matters" not in q:
                flag("structure", q["id"], "order_matters missing")
        elif sql is not None:
            flag("structure", q["id"], f"{q['expected_behavior']} item must have sql: null")
        if q.get("paraphrase_of") and q["paraphrase_of"] not in by_id:
            flag("structure", q["id"], f"unknown paraphrase_of {q['paraphrase_of']}")

    # ---- execution ------------------------------------------------------------
    R: dict[str, Result] = {}
    with psycopg.connect(args.dsn) as conn:
        for q in questions:
            if q["expected_behavior"] != "answer" or not q.get("sql") or "-- TODO" in q["sql"]:
                continue
            try:
                res = run(conn, q["sql"], args.timeout)
            except psycopg.Error as exc:
                flag("execution", q["id"], f"error: {str(exc).strip().splitlines()[0]}")
                continue
            R[q["id"]] = res
            if not res.rows:
                flag("execution", q["id"], "zero rows")
            if res.seconds > args.slow:
                flag("execution", q["id"], f"slow: {res.seconds:.1f}s")

        # ---- cross-checks (independent queries) --------------------------------
        crosschecks = yaml.safe_load(args.cross_checks.read_text())["checks"] if args.cross_checks.exists() else []
        cross_ok = 0
        for c in crosschecks:
            qid = c["id"]
            if qid not in R:
                flag("crosscheck", qid, "gold result unavailable")
                continue
            try:
                other = run(conn, c["sql"], args.timeout)
            except psycopg.Error as exc:
                flag("crosscheck", qid, f"independent query error: {str(exc).strip().splitlines()[0]}")
                continue
            om = by_id[qid]["order_matters"]
            if same_rows(canonical(R[qid].rows, om), canonical(other.rows, om), c.get("rel_tol", 0.0)):
                cross_ok += 1
            else:
                flag("crosscheck", qid, f"gold {R[qid].rows[:3]} != independent {other.rows[:3]}")

    # ---- paraphrases ------------------------------------------------------------
    for q in questions:
        ref = q.get("paraphrase_of")
        if ref and q["id"] in R and ref in R:
            om = q["order_matters"]
            if canonical(R[q["id"]].rows, om) != canonical(R[ref].rows, om):
                flag("paraphrase", q["id"], f"differs from {ref}")

    # ---- plausibility -------------------------------------------------------------
    lo, hi = PRICE_RANGE
    for qid, res in R.items():
        for i, name in enumerate(res.columns):
            if PRICE_COLUMN.match(name):
                for row in res.rows:
                    v = row[i]
                    if v is not None and not lo <= v <= hi:
                        flag("plausible", qid, f"{name}={v} outside [{lo}, {hi}] EUR/m2")

    # ---- consistency ----------------------------------------------------------------
    rules = consistency_rules(R)
    for desc, rule in rules:
        try:
            ok = rule()
        except (KeyError, IndexError, StopIteration, ValueError, TypeError) as exc:
            flag("consistency", "-", f"{desc}: cannot evaluate ({exc!r})")
            continue
        if not ok:
            flag("consistency", "-", desc)

    # ---- snapshot -----------------------------------------------------------------
    current = {
        "eval_version": spec.get("eval_version"),
        "dvf_release": spec.get("dvf_release"),
        "results": {qid: {"columns": r.columns, "rows": r.rows} for qid, r in sorted(R.items())},
    }
    if args.write_snapshot:
        args.snapshot.write_text(json.dumps(current, ensure_ascii=False, indent=1) + "\n")
        print(f"snapshot written: {args.snapshot.name} ({len(R)} results)")
    elif args.snapshot.exists():
        frozen = json.loads(args.snapshot.read_text())
        for key in ("eval_version", "dvf_release"):
            if frozen.get(key) != current[key]:
                flag("snapshot", "-", f"{key}: snapshot {frozen.get(key)!r} != eval {current[key]!r}")
        for qid in sorted(set(frozen["results"]) | set(current["results"])):
            if frozen["results"].get(qid) != current["results"].get(qid):
                flag("snapshot", qid, "result differs from the frozen snapshot")

    # ---- report -------------------------------------------------------------------
    answer_items = sum(q["expected_behavior"] == "answer" for q in questions)
    slowest = max(R.items(), key=lambda kv: kv[1].seconds) if R else None
    print(f"eval {spec.get('eval_version')} | dvf_release {spec.get('dvf_release')} | {len(questions)} questions, {answer_items} with gold SQL")
    print(f"executed {len(R)}/{answer_items}" + (f" | slowest {slowest[0]} {slowest[1].seconds * 1000:.0f} ms" if slowest else ""))
    print(f"consistency rules {len(rules)} | cross-checks agreeing {cross_ok}/{len(crosschecks)}")
    if not flags:
        print("OK: no flags")
        return 0
    print(f"\n{len(flags)} FLAG(S):")
    for check, qid, msg in flags:
        print(f"  [{check}] {qid}: {msg}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
