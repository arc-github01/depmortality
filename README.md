# depmortality

**Predict when a project's dependencies will stop being maintained** — with calibrated
probabilities and guaranteed lower bounds, not heuristic 0–100 health scores.

> **Status: Phase 0 complete (data and labelling pilot), awaiting Gate 1 sign-off.**
> There is no user-facing tool yet. What exists today is the data pipeline, the state
> machine that labels package histories, and the [Gate 1 report](reports/gate1.md).
> Version 1 targets **PyPI only**.

---

## What it will do

Given a repository or lockfile (`requirements.txt`, `poetry.lock`, `uv.lock`), depmortality
will report, for every direct and transitive dependency:

- P(failed within 12 / 24 / 36 months)
- P(finished) — quiet because it's *done*, not because it's abandoned
- a 90%-guaranteed lower bound on remaining maintained life

and, for the project as a whole: a survival curve, P(≥1 critical failure), the expected
number of failures, tail risk P(≥k failures in one year), and maintainer concentration.

Planned front ends: a Python library, a CLI (`depmortality scan`), a GitHub Action, and
(stretch) a dashboard.

## The statistical ideas

1. **Finished vs. dead.** A mixture cure model separates packages that are quiet because
   they're complete from packages that are quiet because they've been abandoned.
2. **Correlated maintainer failure.** A shared gamma frailty per maintainer captures the
   fact that one person burning out can take down many packages at once.
3. **Whole-tree Monte Carlo.** Simulate correlated futures of the full transitive
   dependency tree, conditional on each package's current age and state.
4. **Conformal guarantees.** Lower bounds on survival time with finite-sample coverage,
   adapted for drift in the ecosystem.

The full specification is in [CLAUDE.md](CLAUDE.md) §4 and
[docs/idea_and_plan.md](docs/idea_and_plan.md).

## How a package's life is labelled

Time starts at a package's first PyPI release and is measured in calendar months.

| State | Meaning |
|---|---|
| **Active** | Non-bot maintainer activity (commit to default branch, PyPI release, or maintainer reply on an issue/PR) in the trailing 12 months |
| **Quiet** | No maintainer activity for 12 months; dated at the *last* activity |
| **Revived** | Maintainer activity resumes after Quiet |
| **Failed** | First of: (a) repo archived / PyPI archived or deprecated; (b) an OSV advisory unfixed after 90 days; (c) no support for the newest CPython 12 months after release; (d) an explicit "no longer maintained" statement *(not yet implemented)* |
| **Finished** | Latent — never observed, estimated by the cure model |

Bot activity (Dependabot, Renovate, pre-commit-ci, `*[bot]`, and `config/bots.yaml`) is
stripped before any state is computed. All thresholds live in `config/thresholds.yaml`.

## Phase 0 pilot results

From [reports/gate1.md](reports/gate1.md) (data freeze 2025-09-30):

- **Universe:** 46,267 PyPI packages with ≥1 direct dependent, a GitHub repo, and a first
  release in 2012–2022; 42,046 eligible after repo resolution.
- **Pilot:** 2,000 packages (1,000 most depended-upon + 1,000 stratified tail);
  1,996 pulled and analysed.
- **Transitions observed:** 1,395 Quiet transitions, 791 revivals, 184 failures.
- **Projected failures in the eligible universe:** 4,426 (95% interval 3,650–5,201).
- **Median time to first Quiet:** 94 months from first release.
- **Cost:** 22.5 GB billed on BigQuery.

The verdict is **viable, provisional** — the 100-package hand check
(`reports/gate1_handcheck.csv`) has not been scored yet.

## Scientific guardrails

These are enforced in code and tests; see [CLAUDE.md](CLAUDE.md) §2 for the full list.

- **No look-ahead.** Every feature takes an explicit `as_of` date; property-based tests
  (`hypothesis`) inject future rows and check outputs don't change.
- **Time-varying covariates only**, to avoid immortal-time bias.
- **Fixed temporal holdout** (`config/splits.yaml`): train ≤ 2021-12-31, calibrate on 2022,
  test 2023-01-01 → data freeze.
- **Seeded randomness** everywhere, so every figure is reproducible.
- **BigQuery cost guards:** every query is dry-run first and capped with
  `maximum_bytes_billed`.
- **Ethics:** risk is reported per *package*, never as a ranking of people. Released data
  uses hashed maintainer IDs and no plain-text emails.

## Getting started

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-groups --python 3.12   # or: make install
make check                           # ruff + mypy --strict + offline tests
```

### Credentials

Copy `.env.example` to `.env` (git-ignored) and fill in:

| Variable | Used for |
|---|---|
| `GITHUB_TOKEN` | GitHub API (repo metadata, archival status) |
| `GOOGLE_CLOUD_PROJECT`, `GOOGLE_APPLICATION_CREDENTIALS` | BigQuery (deps.dev, GH Archive) |
| `DEPMORTALITY_ID_SALT` | Secret salt for hashing maintainer identities — keep stable, never publish |
| `DEPMORTALITY_BQ_DATASET` | Your BigQuery dataset for the slim GH Archive extraction |

### Running the pipeline

```bash
uv run python -m depmortality.pipeline <stage> [--refresh | --cache-only]
```

Stages, in order: `dryrun`, `universe`, `resolve`, `select`, `extract`, `pull`, `build`,
`report`. `pilot` runs `universe` through `report`.

```bash
make dryrun   # free: dry-runs every BigQuery query, writes reports/bq_dryrun_phase0.md
make pilot    # rebuilds everything from data/raw with no network (cache misses are errors)
```

Every external response is cached under `data/raw/`, keyed by source, query hash and
snapshot date. Re-runs never re-download unless you pass `--refresh`.

## Repository layout

```
config/            splits, thresholds, bot list, pilot design, Python release dates
src/depmortality/
  data/            pullers: deps.dev, GH Archive (BigQuery + mirror), PyPI, GitHub, OSV, git log
  labels/          bots, identity merging, monthly panel, state machine, failure triggers
  reports/         Gate 1 report and hand-check generators
  utils/           caching, BigQuery guardrails, HTTP retry, seeding, logging
  pipeline.py      stage runner
tests/             pytest + hypothesis (network tests marked and skipped in CI)
reports/           generated gate reports and figures
docs/              project plan and prompt log
DECISIONS.md       dated log of every non-obvious choice and why
```

Modelling (`models/`), simulation (`simulate/`), conformal bounds (`conformal/`) and the
CLI arrive in later phases.

## Roadmap

- [x] **Phase 0** — data pulls, bot filtering, identity merging, state machine, failure triggers, Gate 1 report
- [ ] Gate 1 hand check and approval
- [ ] **Phase 1** — frequentist baselines (Cox, cure, frailty) and formal tests
- [ ] Bayesian model in NumPyro (piecewise-exponential baselines, frailty, latent cure)
- [ ] Whole-tree Monte Carlo, with the closed-form frailty golden test
- [ ] Conformal lower bounds with drift corrections
- [ ] `depmortality scan` CLI, GitHub Action, paper

## Contributing

Read [CLAUDE.md](CLAUDE.md) first — it's the standing brief. Log any new threshold,
exclusion or modelling choice in [DECISIONS.md](DECISIONS.md). Keep commits small, and
write tests before modelling code.

## License

Apache-2.0
