# dvf-text-to-sql-eval

A versioned evaluation set for **text-to-SQL on French property sales (DVF)**,
Gironde, 2021-2025. Each question comes with its expected behaviour (answer,
ask for clarification, say the data can't answer, refuse) and, for answerable
ones, a **gold SQL query that was run and verified** against the data.

| | |
|---|---|
| Questions | 143 (103 with gold SQL) |
| Data | DVF géolocalisées (Etalab), DGFiP release **2026-04**, years 2021-2025 |
| Scope | Gironde (department 33) |
| Version | **eval-v1.1** (`eval_version: "1.1"` in the YAML, git tag `eval-v1.1`); see the changelog |
| Database | [`dvf-airflow-pipeline`](https://github.com/mariot/dvf-airflow-pipeline): PostgreSQL 16 + PostGIS, semantic layer `sql/semantic.sql` |

## Contents

```
dvf_eval_questions.yaml   the eval set: questions, categories, conventions, gold SQL
check_gold.py             runs every gold query and flags anything suspicious
cross_checks.yaml         independent second queries for the hardest questions
gold_results.json         frozen gold results of this version (drift detection)
```

### Question categories

| category | expected_behavior | count |
|---|---|---|
| simple, aggregation, temporal, ranking, spatial, multi_step, data_quality, paraphrase | `answer` | 102 |
| security (injection inside a legitimate question) | `answer` | 1 |
| ambiguous | `clarify` | 14 |
| unanswerable | `unanswerable` | 12 |
| security, privacy | `refuse` | 14 |

Paraphrases (`paraphrase_of`) must return exactly the same result as their
reference question. `order_matters` says whether results compare as ordered
lists or as sets.

## Running the checks

Start the database from `dvf-airflow-pipeline` (`docker compose up -d`, then
load the years), then:

```bash
uv run check_gold.py                     # DSN defaults to postgresql://dvf:dvf@localhost:5433/dvf
DVF_DSN=postgresql://... uv run check_gold.py
```

`uv` installs the two dependencies (psycopg, PyYAML) from the script header.
Every query runs in a **read-only** transaction with a statement timeout. The
script exits with 1 and lists the flags when any check fails:

| check | flags |
|---|---|
| structure | duplicate ids, `answer` items without SQL or still containing `-- TODO`, SQL on non-answer items, missing `dvf_release` |
| execution | SQL errors, zero rows, queries slower than `--slow` (3 s) |
| paraphrase | a paraphrase whose result differs from its reference |
| plausible | EUR/m² medians, means and quartiles outside 500-20 000 |
| consistency | 19 cross-question rules: Bordeaux < Gironde, monthly counts add up to the year, the same median is identical wherever it appears, CAGR matches the yearly medians... |
| crosscheck | the 16 independent queries of `cross_checks.yaml` (duplication trap, spatial, multi-step) disagree with gold |
| snapshot | any result differs from `gold_results.json`, or the version/release changed |

Current state: 103/103 executed (slowest 0.3 s), 19/19 consistency rules,
16/16 cross-checks agree, 0 flags. Each check was also tested on a
deliberately broken copy of the YAML, and each one fired.

## How the gold answers were verified

- **Independent queries.** The hardest answers have a second query that reads
  only the raw table and uses a different method (`count(DISTINCT ...)`,
  `DISTINCT ON`, another deduplication, geodesic distances instead of
  Lambert-93). Where the methods legitimately differ at the margin, the
  tolerance and the reason are written next to the query.
- **Public figures.** Etalab's `statistiques-dvf` method (the figures behind
  the DVF map app), rerun on the same data, reproduces the published Bordeaux
  monthly medians exactly for 23 of 24 months of 2024-2025. On Bordeaux
  apartments 2024 it gives 4 219 EUR/m², against 4 217 for the gold query
  (agg-02); the difference comes from documented convention choices.
- **The data changed the questions.** Four questions were moved after writing
  their SQL: sim-05 and agg-09 ("terrain à bâtir" means two different things in
  DVF) and agg-14 to `ambiguous`, agg-11 to `unanswerable` (VEFA sales have no
  surface). Traps found along the way are in each question's `notes`.

## Conventions (summary)

The full list is at the top of the YAML. The ones that change results most:

- **One sale = one mutation** (`id_mutation`), never one raw row: every row
  repeats the total price (a naive `SUM` is 8.3x too high in Gironde 2024).
- DVF repeats a property once per *nature de culture* of its parcel; the
  semantic layer deduplicates it (14 371 raw "Maison" rows = 12 388 houses).
- **"prix" = median.** Prices use single-property sales (one house or
  apartment, dépendances allowed), nature Vente or VEFA, price > 0.
- **VEFA trap:** ~95% of VEFA sales have no property type and no surface.
- Rankings need >= 20 sales; default top-N is 10; ties at the cut-off are kept.
- Distances are in Lambert-93 (EPSG:2154); score distance-based counts with a
  ~0.1% tolerance (geodesic distances differ by 0.035% at 5 km).
- Relative dates use `eval_today: 2026-10-15`.

## Versioning: never compare scores across benchmarks by accident

Gold results depend on the questions, the conventions **and** the data
snapshot. So:

1. Any change to a gold query, a convention, a category or `dvf_release`
   bumps `eval_version` in the YAML.
2. Re-freeze: `uv run check_gold.py --write-snapshot` (it refuses nothing, so
   review the diff of `gold_results.json`).
3. Commit, then tag `eval-v<version>` and push the tag.
4. Report every score with its eval version: "62/103 on eval-v1.1".

A new DVF release (April / October) changes results even with identical SQL:
`check_gold.py` then flags the snapshot, and that is a new version too.

## Changelog

**v1.1** (dvf_release 2026-04). A query-by-query review of every gold SQL
looked for results that run fine but make no sense.
- **mul-06** fixed: the resale key (parcel, type, surface, pièces) merged
  different flats of the same building. 6 567 of 7 514 apartment "resales"
  had two different lot numbers. The key now includes `lot1_numero`: 10 795 →
  4 929 properties sold at least twice. The gold also returns the >= 3 sales
  count, because "revendus au moins deux fois" can be read both ways.
- **mul-07**: same key fix (268 → 227 resales, median gain 41 268 → 52 000 EUR),
  plus the median holding time.
- **mul-02**: Bordeaux no longer compares with itself.
- **spa-08**: also returns the cell centre in WGS84, so the answer can locate it.
- Notes: agg-16 (the 0.01 EUR/m² minimum is a 1 EUR symbolic sale), rnk-05
  (top "houses" include estates and development land), tmp-08 (runner-up is
  0.3 pt behind), sim-02 / tmp-05 (untyped VEFA lots are not counted),
  mul-13, sim-12, dq-10.
- New cross-check for mul-06.

Checked and left unchanged: no sale spans two departments, and large prices
shared with other departments are round-number coincidences, not split
national portfolios. The round medians (Lège-Cap-Ferret 10 000 / 12 000) are
real middle values. Pineuilh's low apartment median spreads over streets and
months. agg-03's mean is not driven by outliers. The largest apartment sale
(1.95 M EUR, 265 m²) is plausible.

**v1.0**: first frozen version.

## License and data

DVF is published by the DGFiP under the Licence Ouverte 2.0. Its reuse
conditions forbid re-identifying individuals, which is why address-level
lookups (sec-14, sec-15) are `refuse` items.
