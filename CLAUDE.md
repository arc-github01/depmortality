# CLAUDE.md — depmortality

This file is the standing brief for every Claude Code session in this repository. Read it fully before doing anything. If an instruction in a prompt conflicts with a **non-negotiable rule** below, stop and ask.

---

## 1. What we are building

`depmortality` is an open-source engine that predicts **when a project's dependencies will stop being maintained**. It outputs calibrated probabilities and guaranteed lower bounds, not heuristic 0–100 scores.

- **Input:** a repository or lockfile (`requirements.txt`, `poetry.lock`, `uv.lock`; npm later).
- **Output, per dependency (direct and transitive):** P(failed within 12/24/36 months), P(finished), and a 90%-guaranteed lower bound on remaining maintained life.
- **Output, per project:** a survival curve, P(≥1 critical failure), the expected number of failures, tail risk P(≥k failures in one year), and maintainer concentration.
- **Front ends:** a Python library, a CLI (`depmortality scan`), a GitHub Action, and a dashboard (stretch).
- **Research output:** a paper. Every result must therefore be reproducible from scripts, never from ad-hoc notebook state.

**Version 1 scope: PyPI only.** Do not add npm support until told to.

### The four statistical contributions (the reason this project exists)

1. **Finished vs. dead.** A mixture cure model separates packages that are quiet because they're *done* from packages that are quiet because they're *abandoned*.
2. **Correlated maintainer failure.** A shared gamma frailty per maintainer means one person burning out can take down many packages at once.
3. **Whole-tree Monte Carlo.** Simulate correlated futures of the full transitive dependency tree.
4. **Conformal guarantees.** Lower bounds on survival time with finite-sample coverage, adapted for ecosystem drift.

---

## 2. Non-negotiable rules

These protect the scientific validity of the project. Breaking any of them silently invalidates the paper.

1. **No look-ahead, ever.** Every feature function takes an explicit `as_of: date` and may only read data timestamped ≤ `as_of`. Each feature has a test proving it ignores later data (use `hypothesis` to inject future rows and assert the output doesn't change).
2. **Time-varying covariates only.** Never attach a status earned later in life (a major release, dependents, sponsors, contributor totals) as a baseline covariate. That creates immortal time bias. Use counting-process (start, stop] rows or landmarking.
3. **Temporal holdout is sacred.**
   - Train on data observable up to **2021-12-31**.
   - Tune and calibrate on outcomes during **2022**.
   - Test on outcomes from **2023-01-01 to the data freeze**.
   - Never tune anything on test-period outcomes. The split dates live in `config/splits.yaml`; don't hardcode them anywhere else.
4. **Bots are not maintainers.** Strip bot activity (Dependabot, Renovate, pre-commit-ci, github-actions, any login ending in `[bot]`, plus the list in `config/bots.yaml`) *before* computing activity or states. Bot-only activity never counts as a revival.
5. **Never fabricate data or results.** If a pull fails, a field is missing, or a number can't be computed, say so. Don't fill in plausible values, and don't write placeholder numbers into reports, figures or docs.
6. **Seeds everywhere.** Every random operation takes an explicit seed from config. Any figure must be exactly reproducible.
7. **Cost guardrails for BigQuery.** Dry-run every query first and log the bytes it would process. Set `maximum_bytes_billed` on every job. **Ask me before running any single query estimated above 50 GB.** Query GH Archive only through date-partitioned tables, filtered to the target repositories.
8. **Ethics.** The tool makes predictions involving real people.
   - Never produce a ranking of individuals by "likelihood to quit".
   - Risk is reported per **package**.
   - Maintainer views appear only inside a user's own dependency tree.
   - Released datasets use **hashed** maintainer IDs.
   - Never store emails in plain text in any artifact meant for release.
9. **Secrets.** Read tokens from environment variables (`GITHUB_TOKEN`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_APPLICATION_CREDENTIALS`). Never print, log or commit them. `.env` is git-ignored.

---

## 3. Domain definitions (implement exactly; thresholds come from config)

Time origin is a package's **first PyPI release**. The time unit is the **calendar month**.

| State | Definition |
|---|---|
| **Active** | ≥1 maintainer activity (non-bot commit to the default branch, PyPI release, or maintainer reply on an issue/PR) in the trailing `quiet_window_months` (default 12) |
| **Quiet** | No maintainer activity for `quiet_window_months`. The transition is dated at the **last** activity, not at the end of the window |
| **Revived** | Maintainer activity resumes after Quiet, by the original maintainer or a new one |
| **Failed** | The **first** of these triggers: (a) repo archived on GitHub, or PyPI status archived/deprecated (PEP 792 markers, where exposed); (b) an OSV advisory with no fixed release within `advisory_window_days` (default 90); (c) no support for the newest CPython release `python_support_lag_months` (default 12) after it ships, judged from `requires_python`, classifiers and wheels; (d) an explicit "no longer maintained / deprecated" statement in the README or a maintainer comment (LLM-extracted, audited) |
| **Finished** (latent) | Never observed directly; estimated by the cure model. **Validation-only** signals: the `Development Status :: 6 - Mature` classifier and LLM-labelled "feature complete" READMEs. These must **not** be used as training labels for π, or the validation is circular |

Also record, but don't treat as failure by itself: `Development Status :: 7 - Inactive`. Log it as a candidate trigger and report how often it agrees with the other triggers.

**Primary maintainer (v1):** the top non-bot committer to the default branch over the trailing 24 months. Fall back to the first PyPI owner account. Merge identities across PyPI accounts, GitHub logins and commit emails, then hash them.

---

## 4. Model specification (for later phases; do not start before told)

- **Active → Quiet:** hazard λ_AQ(t | x_i(t), Z_m) = Z_m · λ0_AQ(t) · exp(βᵀ x_i(t)), with Z_m ~ Gamma(1/θ, 1/θ) shared per maintainer. θ = 0 is the independence model. Report Kendall's τ = θ/(θ+2).
- **Quiet → Failed:** mixture cure model S(u | z, x) = π(z) + (1 − π(z)) · S_u(u | x), with π(z) = logistic(γᵀz) and u = time since going quiet.
- **Quiet → Revived:** a competing cause-specific hazard. **Active → Failed** directly is a rare fourth transition.
- **Estimation:** first a frequentist pass (lifelines/scikit-survival; R `survival`, `frailtyEM`, `flexsurvcure` as cross-checks via `rpy2` or saved CSV). Then the main **NumPyro** model: piecewise-exponential baselines via the Poisson trick on person-month data, the frailty as a random effect, and the cure indicator as a latent Bernoulli.
- **Monte Carlo propagation:** sample each package's future **conditional on its current age and state** (left truncation: T = S⁻¹(U·S(a))). Packages sharing a maintainer share one Z draw per run. Use posterior draws, not plug-in estimates. Default S = 10,000 runs.
- **Golden test (must pass before any Monte Carlo result is trusted):** for one maintainer's packages, P(no failure by h) = (1 + θ Σ Λ_i(h))^(−1/θ). The simulator must match this within Monte Carlo error. As θ → 0 it must match exp(−Σ Λ_i(h)).
- **Conformal layer:** conformalized survival lower bounds (Candès, Lei & Ren, JRSS-B 2023), with two drift fixes to compare: recent-cohort calibration and adaptive conformal inference.

---

## 5. Tech stack and conventions

- **Python 3.12**, managed with **uv**. Lint and format with `ruff`, type-check with `mypy --strict` on `src/`. Tests use `pytest` + `hypothesis`; CI runs on GitHub Actions.
- **Data:** Parquet files (partitioned by month where relevant), `duckdb` for joins, `polars` for transforms. Use pandas only where a library requires it.
- **External access:**
  - `google-cloud-bigquery` for deps.dev (`bigquery-public-data.deps_dev_v1.*`), GH Archive (`githubarchive.month.*` / `githubarchive.day.*`) and PyPI downloads (`bigquery-public-data.pypi.file_downloads`).
  - `httpx` with retry and rate-limit handling for the PyPI JSON API (`https://pypi.org/pypi/<name>/json`), the GitHub GraphQL API, and OSV (`https://api.osv.dev`).
- **Modelling:** `lifelines`, `scikit-survival`, `numpyro` (JAX), `numpy`; `numba` only if profiling demands it.
- **CLI:** `typer` + `rich`. **Config:** YAML files in `config/`, loaded into pydantic models. No magic numbers in code.
- **Caching:** every external call caches raw responses under `data/raw/` keyed by (source, query hash, date). Re-running a pipeline never re-downloads unless `--refresh`.

### Repository layout

```
depmortality/
├── CLAUDE.md
├── DECISIONS.md          # dated log of every non-obvious choice and why
├── config/               # splits.yaml, thresholds.yaml, bots.yaml, pilot.yaml
├── src/depmortality/
│   ├── data/             # pullers: depsdev.py, gharchive.py, pypi.py, github.py, osv.py
│   ├── labels/           # bots.py, identity.py, panel.py, states.py, triggers.py, llm_labels.py
│   ├── features/         # monthly time-varying covariates, all as_of-aware
│   ├── models/           # baselines/, cure/, frailty/, bayes/
│   ├── simulate/         # Monte Carlo over dependency trees
│   ├── conformal/
│   ├── evaluate/         # one module per RQ / figure / table
│   ├── cli/
│   └── utils/            # caching, bq guardrails, seeding, logging
├── action/               # GitHub Action (Docker)
├── dashboard/            # stretch
├── reports/              # gate reports (markdown + figures), generated by scripts
├── notebooks/            # exploration only; nothing in the paper depends on a notebook
├── tests/
└── data/                 # git-ignored: raw/, interim/, processed/
```

---

## 6. How to work with me

- **Plan first.** For any task bigger than a single function, write a short plan (files to touch, tests to add, risks) before coding. For phase kickoffs, show the plan and wait for my OK.
- **Tests before modelling code.** Write the test that defines correct behaviour first, especially for the state machine, `as_of` features and the simulator.
- **Small, reviewable commits** with clear messages. One concern per commit.
- **Log decisions.** Any threshold, exclusion rule or modelling choice goes in `DECISIONS.md` with the date, the choice, the alternatives considered and the reason.
- **Report honestly.** When a step finishes, summarise what worked, what didn't, and the actual numbers. Don't call something done if its tests fail, or if it ran on a subset I didn't approve.
- **Stop at gates.** Each phase ends with a gate report in `reports/`. Don't start the next phase until I approve it.
- **Ask when unsure** about data semantics, cost, or anything touching the non-negotiable rules. A one-line question beats a wrong assumption baked into the dataset.
