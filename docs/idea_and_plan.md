# Dependency Mortality Engine: Idea and Execution Plan

Oct 26, 2026 · @Aryan kumar

## Summary

We will build an open-source engine that predicts *when* a project's dependencies will stop being maintained, with calibrated probabilities and guarantees. Unlike existing tools, it tells finished packages apart from dead ones and models maintainers burning out across many packages at once.

- **Working name:** `depmortality` (rename freely)
- **One-line pitch:** actuarial life tables for your software supply chain.
- **Input:** a repository or lockfile (`requirements.txt`, `package-lock.json`).
- **Output:** for every direct and transitive dependency, P(abandoned within 12/24 months), a guaranteed lower bound on its remaining life, and a project-level "supply-chain life expectancy" with tail risk.
- **Four statistical contributions:**
  1. **Cure model:** separates *finished* (stable, no work needed) from *dead*.
  2. **Maintainer frailty:** models correlated deaths when one person maintains many packages.
  3. **Monte Carlo propagation** through the full transitive dependency tree.
  4. **Conformal lower bounds** with coverage guarantees under ecosystem drift.
- **Deliverables:** a Python library + CLI, a GitHub Action, a web dashboard, a public dataset, and a research paper.
- **Scope for version 1:** PyPI only. npm comes second, once the method works.
- **Timeline:** about 8 months at 10–12 hours a week (Nov 2026 – Jun 2027), with three go/no-go gates.
- **Target venue:** an empirical software engineering conference research track, plus a separate tool-demo paper. Deadlines are in the Timeline section.

Paper 1 (the bias audit) is now optional. Its lesson is built in: every model here uses time-varying covariates, so the engine doesn't commit the bias that Paper 1 would audit.

## The gap

Predicting abandonment is well studied. What no one has done is model *finished vs. dead*, *correlated maintainer failure*, and *calibrated whole-tree risk* together, with guarantees.

| Prior work | What it does | What it leaves open |
| --- | --- | --- |
| [Miller et al., ICSE 2025](https://conf.researchr.org/details/icse-2025/icse-2025-research-track/16/Understanding-the-Response-to-Open-Source-Dependency-Abandonment-in-the-npm-Ecosystem) | Measures npm abandonment and how dependents react | Direct dependencies only; [reviewers flagged](https://plg.uwaterloo.ca/~migod/846/current/summaries/21-Kevin-npmAbandonment-slides.pdf) transitive exposure as ignored |
| [Xu et al. 2025](https://arxiv.org/abs/2507.21678v2) | AFT survival prediction, C-index 0.846 | Single-package risk; no tree propagation, no finished/dead split, no guarantees |
| [Tsakpinis et al. 2026](https://arxiv.org/pdf/2605.06164) | Propagates maintenance through the full PyPI closure | Deterministic and linear; one snapshot; no probabilities or correlation |
| [Ossuary](https://pypi.org/project/ossuary-risk/), [Infield](https://onceamaintainer.substack.com/p/identifying-unmaintained-open-source) | Risk scores for single packages | Heuristic 0–100 scores; no time horizon, no calibration |
| [Security ecosystem study](https://arxiv.org/pdf/1903.02613) | Counts abandoned packages | Explicitly lumps finished packages in with abandoned ones |

**The four things nobody does, which this engine does:**

1. **Finished vs. dead.** A package with no commits may simply be done. A cure model estimates that probability instead of guessing with a fixed inactivity threshold.
2. **Correlated failure.** The average npm package transitively relies on [about 40 maintainers](https://blog.acolyer.org/2019/09/30/small-world-with-high-risks/), and some maintain over 100 packages. One person leaving can kill dozens of packages at once; independence models miss this.
3. **Probabilistic whole-tree risk.** "P(at least one critical dependency dies within 24 months)" with tail probabilities, not a weighted sum.
4. **Guarantees.** Conformal lower bounds that hold at a stated rate, checked on future data.

## The product

There are three front ends on one engine: a CLI for developers, a GitHub Action for CI, and a dashboard for exploring risk. All three show probabilities with intervals, never a bare 0–100 score.

### 1. CLI

`pip install depmortality`, then `depmortality scan requirements.txt`. Illustrative mock output (invented package names and numbers):

```
Supply-chain life report   requirements.txt   42 deps (9 direct, 33 transitive)

Project-level (24-month horizon, 10,000 Monte Carlo runs)
  P(>=1 critical dependency abandoned)    0.31   [90% interval 0.24-0.38]
  Expected abandoned dependencies          1.4
  P(>=3 abandoned in the same year)        0.06   (independence model says 0.01)

Highest-risk dependencies
  package        depth  P(dead,24m)  P(finished)  life >= (90% guaranteed)  why
  tinyretry      2      0.47         0.08         7 months                  1 maintainer, activity falling
  yamlish        1      0.22         0.15         14 months                 maintainer also owns 61 other pkgs
  colorfmt       3      0.05         0.81         -                         looks finished: stable API, issues answered

Maintainer concentration
  @dev_k  owns 6 of your 42 deps   shared-frailty risk: HIGH
```

The two columns no other tool has are `P(finished)` and the correlated-tail line ("independence model says 0.01").

### 2. GitHub Action

- Runs on every pull request that changes a lockfile.
- Comments when a newly added dependency raises the project's 24-month risk by more than a set threshold.
- Fails the check only if the user opts in (for example, any new dependency with P(dead, 12 months) > 0.5).

### 3. Dashboard

- **Survival curve** of the whole project, with a 90% band, compared against the independence model.
- **Dependency tree** coloured by risk; click a node to see its own survival curve and cure probability.
- **Maintainer view:** which people your project leans on, and how many of your dependencies each one holds.
- **What-if:** "replace `tinyretry` with X", showing how the project curve moves.

### Who uses it

- Developers choosing between two libraries.
- Security and platform teams auditing supply-chain risk.
- Funders (Sovereign Tech Agency, Tidelift, GitHub Sponsors donors) deciding which maintainers to support. The maintainer view answers exactly that.

## Architecture

All the heavy statistics happen offline once a month. Scanning a repository only resolves its dependency tree and runs the simulator, so a scan takes seconds on a laptop.

&#91;embedded content: depmortality architecture · offline training, online scoring\]

- **Offline:** pull data → build labels and monthly features → fit the multi-state cure–frailty model → store posterior draws per package, versioned by data snapshot.
- **Online:** read the lockfile → resolve the full transitive tree and who maintains each node → simulate 10,000 correlated futures → attach conformal lower bounds.
- **Front ends** all call the same scoring function, so the CLI, the Action and the dashboard can never disagree.

## Statistical engine

The engine is a multi-state survival model. A shared maintainer frailty drives the "goes quiet" step and a cure fraction sits on the "quiet to failed" step. Its posterior feeds a Monte Carlo simulator and a conformal layer that adds guarantees.

### 1. States and what counts as failure

The key design choice: **going quiet is not failure.** Failure is when a dependency actually becomes a problem for the people using it.

| State | Meaning | Observed by |
| --- | --- | --- |
| Active | Commits, releases or maintainer replies in the last 12 months | Activity history |
| Quiet | No maintainer activity for 12 months (dated at the last activity) | Activity history |
| Revived | Activity resumes after Quiet, by the original maintainer or a new one | Activity history |
| Failed | The package becomes a problem for users | First of: archived / deprecated status marker; an advisory unpatched for 90+ days; no support for the newest Python release 12 months after it ships; an explicit "unmaintained" statement |
| Finished (latent) | Quiet but will never fail: a stable, complete package | Never directly observed; estimated by the cure model and *validated* against the self-declared PyPI "Development Status :: 6 - Mature" classifier and LLM-labelled "feature complete" READMEs |

Time origin is the first PyPI release. The clock is in months, and every covariate is measured monthly using only data available up to that month.

### 2. Model: multi-state hazards with maintainer frailty and a cure fraction

**Active → Quiet** (the burnout step), with a shared gamma frailty *Z* per maintainer *m*:

```latex
\lambda_{AQ}\big(t \mid x_i(t), Z_m\big) = Z_m \, \lambda_{0,AQ}(t) \, \exp\{\beta^\top x_i(t)\}, \qquad Z_m \sim \text{Gamma}(1/\theta,\ 1/\theta)
```

*Z* has mean 1 and variance θ. A maintainer with high *Z* tends to let all their packages go quiet together. θ = 0 recovers the independence model, which is the baseline every competitor implicitly uses. Report the dependence as Kendall's τ = θ / (θ + 2).

**Quiet → Failed**, with a cure fraction π (the probability the package is finished). *u* is time since going quiet:

```latex
S_{QF}(u \mid z, x) = \pi(z) + \big(1 - \pi(z)\big)\, S_{u}(u \mid x), \qquad \pi(z) = \frac{1}{1 + e^{-\gamma^\top z}}
```

**Quiet → Revived** is a competing transition with its own hazard. Direct Active → Failed (for example, deprecated while still active) is a rare fourth transition.

**Estimation, in two passes:**

1. **Fast frequentist pass** to get going. Cause-specific Cox models (R `survival`) and EM for the cure model (R `smcure` / `flexsurvcure`), with frailty from R `frailtyEM` or `coxme`.
2. **Main Bayesian model.** Use piecewise-exponential baselines via the Poisson trick on person-month data, written in NumPyro. The frailty is a random effect and the cure indicator a latent Bernoulli. Posterior draws carry parameter uncertainty straight into the Monte Carlo step.

**Identifiability checks:** a Kaplan-Meier plateau on Quiet → Failed, and the Maller–Zhou test that cures exist with long enough follow-up. Proportional hazards matter here because [violating them biases cure-probability estimates](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC13562180/), so test with Schoenfeld residuals and fall back to an AFT latency model if needed.

**Multiple maintainers.** In version 1, assign each package its primary maintainer: the top committer over the last 24 months. Version 2 can use multiple-membership frailty, a weighted sum over all maintainers.

### 3. Covariates (time-varying, monthly)

- **Activity:** commits, releases, median issue first-response time, PR merge latency (rolling 3/6/12-month windows), trend slope.
- **People:** active maintainers, truck factor, organization vs. individual owner.
- **Maintainer portfolio** (new, and what powers the frailty story): how many packages the maintainer owns, and their activity across *all* their repositories (a burnout signal).
- **Demand:** dependents over time from deps.dev snapshots, downloads from PyPI's public BigQuery logs, open issues from non-maintainers.
- **Ecosystem:** lag in supporting new Python versions, dependence on already-failed packages.
- **Self-declared:** development-status classifier, funding links.

### 4. Monte Carlo propagation through the dependency tree

Each run simulates the future of every package in the project's resolved tree at once. Correlation enters because packages sharing a maintainer share one frailty draw.

**One simulation run** (repeat S = 10,000 times):

1. Draw one parameter set from the posterior.
2. For each maintainer in the tree, draw Z\_m \~ Gamma(1/θ, 1/θ).
3. For each package, sample its future *conditional on its current age and state*. A 7-year-old active package has already survived 7 years, so sample by inverse transform from the left-truncated distribution:

```latex
T = S^{-1}\big(U \cdot S(a)\big), \qquad U \sim \text{Uniform}(0,1),\ a = \text{current age}
```

4. If it goes quiet, draw "finished" \~ Bernoulli(π). If not finished, sample failure time and revival time and keep whichever comes first.
5. Record which packages failed before each horizon (12, 24, 36 months).

**Outputs:** the project survival curve P(no critical failure by *t*); P(at least one failure); expected number of failures; tail probabilities P(≥ k failures in one year); per-package risk; and per-maintainer attribution (the share of simulated failures traced to each person).

**Accuracy.** With 10,000 runs, the Monte Carlo standard error on a probability near 0.3 is about 0.005, well below the model's own uncertainty.

**A closed form to test the simulator.** For one maintainer's packages under gamma frailty, the probability that none fail by horizon *h* has an exact Laplace-transform formula. The unit tests must reproduce it:

```latex
P(\text{no failure among } m\text{'s packages by } h) = \Big(1 + \theta \sum_{i \in m} \Lambda_i(h)\Big)^{-1/\theta}
```

where Λ\_i is each package's cumulative hazard. As θ → 0 this becomes the independence product, exp(−ΣΛ\_i). The gap between the two *is* the paper's correlated-risk finding.

### 5. Conformal guarantees

[Conformalized survival analysis](https://www.ukbiobank.ac.uk/publications/conformalized-survival-analysis/) wraps any survival model to give a lower bound L(x) on survival time with P(T ≥ L(x)) ≥ 1 − α in finite samples. It handles censoring by restricting to units with long enough censoring times and reweighting.

**The drift problem.** Ecosystems change over time, which breaks the exchangeability that conformal guarantees rely on. Two fixes, compared in the evaluation:

1. **Recent-cohort calibration:** calibrate on the most recent cohort only.
2. **Adaptive conformal inference:** update α online as each new month's outcomes arrive, keeping long-run coverage near the target.

The CLI's "life ≥ 7 months (90% guaranteed)" column comes from this layer.

## Data

Everything needed is public. deps.dev supplies dated dependency graphs, GH Archive the activity history, and PyPI the self-declared status signals that validate the cure model.

### Sources

| Need | Source | Notes |
| --- | --- | --- |
| Dependency graphs and dependents over time | [deps.dev BigQuery dataset](https://docs.deps.dev/bigquery/v1/) | Historical snapshots; transitive closure per version; [dependents with minimum depth](https://blog.deps.dev/enumerating-dependents/); advisories table |
| Activity (commits, issues, PRs, replies) | [GH Archive](https://www.gharchive.org/) via BigQuery | Public GitHub event stream; filter to target repos to keep query costs low |
| Repo state, archived flag, owners | GitHub GraphQL API | Rate-limited; use only for the final package set |
| Release history, maintainers, classifiers | PyPI JSON API | Release dates give the time origin; [classifiers](https://pypi.org/classifiers) include "6 - Mature" and "7 - Inactive" |
| Project status markers | [PEP 792](https://peps.python.org/pep-0792/) | Defines active, archived, quarantined and deprecated statuses; the PEP notes these are not yet exposed in all APIs, so check coverage |
| Unpatched vulnerabilities | OSV / deps.dev advisories | Failure trigger: advisory with no fixed release after 90 days |
| Downloads | PyPI download logs on BigQuery | Demand covariate |
| Python release dates | python.org | Failure trigger: no support for the newest Python after 12 months |

### Scope and cohorts

- **Population:** PyPI packages with at least one dependent in deps.dev and a resolvable GitHub repository. Start with a 2,000-package pilot, then scale to all qualifying packages (expect tens of thousands).
- **Cohorts:** first release from 2012 to 2022, followed up to the data freeze (Nov 2026).
- **Temporal split (no look-ahead):**
  - Train on everything observable up to 31 Dec 2021.
  - Tune and calibrate on outcomes during 2022.
  - Test on outcomes from Jan 2023 to the freeze, scoring models frozen at the end of 2022.

### Labelling pipeline

1. **Monthly activity panel** per package: maintainer commits, releases, issue and PR replies. **Strip bot activity** (Dependabot, Renovate, CI bots) first; otherwise bots fake "revivals", a pattern a [2026 revival study](https://arxiv.org/pdf/2606.20966) calls bot-only pseudo-recovery.
2. **Maintainer identity:** merge PyPI maintainer accounts, GitHub logins and commit emails into one person ID. Store hashed IDs only.
3. **State transitions:** Quiet after 12 months without maintainer activity, dated at the last activity; Revived on resumed activity; Failed on the first failure trigger.
4. **Finished validation set:** packages with the "6 - Mature" classifier, plus READMEs an LLM flags as "feature complete". Hand-label 300 of them with a second annotator and report Cohen's kappa.
5. **Death-statement extraction:** an LLM reads READMEs and the last 20 maintainer comments for "no longer maintained" statements. Check precision on 200 hand-labelled cases.

### Storage and cost

- Raw pulls are Parquet, partitioned by month; joins and features in DuckDB.
- BigQuery has a free monthly query allowance. Pre-filter GH Archive to target repositories and query daily partitions to stay inside it.
- Freeze a versioned dataset at the end of data collection and publish it (with a DOI) as a separate artifact.

## Evaluation

Every claim is tested on the future: models frozen at the end of 2022, scored against what actually happened from 2023 to 2026. That temporal holdout is what makes the results credible.

### Research questions

| RQ | Question | How it is tested | Success looks like |
| --- | --- | --- | --- |
| RQ1 Finished vs. dead | What share of quiet PyPI packages are finished, and does π identify them? | AUC and calibration of π against the Mature-classifier and hand-labelled sets; does π predict *no* failure after going quiet? | π clearly separates finished from death-statement packages; a visible Kaplan-Meier plateau |
| RQ2 Correlated failure | How strong is maintainer frailty, and does ignoring it understate tail risk? | Estimate θ and Kendall's τ; likelihood-ratio test of θ = 0 (boundary case: 50:50 mixture of χ²₀ and χ²₁); compare observed vs. predicted counts of "≥ k failures in one maintainer-year" under θ̂ and under θ = 0 | θ̂ > 0, and the independence model under-predicts clustered failures |
| RQ3 Package-level prediction | How accurate and calibrated are 12- and 24-month failure probabilities? | Time-dependent AUC, Uno's C-index, integrated Brier score, calibration slope and intercept | Beats all baselines on calibration, not just ranking |
| RQ4 Project-level prediction | Do whole-tree probabilities match reality? | Take \~500 real GitHub projects' 2022 lockfiles; predict P(≥ 1 failure in 24 months); compare to observed with a reliability diagram and Brier score | Predicted and observed rates line up within bins |
| RQ5 Guarantees under drift | Do the 90% conformal bounds really cover 90% on future data? | Empirical coverage and bound width for each drift fix (recent-cohort vs. adaptive) | Coverage within a few points of 90% |

### Baselines

1. **Inactivity rule:** "no commit for 12 months = dead" (the field's default)
2. **OpenSSF Scorecard "Maintained" score** (as used by [Tsakpinis et al.](https://arxiv.org/pdf/2605.06164))
3. **Plain Cox model**, no cure and no frailty
4. **Gradient-boosted survival and random survival forest** (scikit-survival), standing in for the [Xu et al.](https://arxiv.org/abs/2507.21678v2) style of model
5. **Independence Monte Carlo** (θ = 0) for the project-level questions

### Ablations

Remove one piece at a time to show each one earns its place:

- No cure fraction
- No frailty
- **Snapshot covariates instead of time-varying ones.** This demonstrates immortal time bias on your own data and gives the Paper 1 idea a home inside this paper.
- No maintainer-portfolio covariates
- No posterior uncertainty (plug-in point estimates)

### Statistical hygiene

- Use paired bootstrap confidence intervals (resampling packages) for every metric difference, since there's no single correct p-value for comparing C-indices.
- Run sensitivity analyses for each threshold: quiet after 6/12/24 months; advisory window 60/90/180 days.
- Run a leakage check: rebuild every test-time feature from a dataset frozen at 31 Dec 2022 and confirm the scores don't change. If they move, future information leaked in.

## Tech stack and repository

Python end to end, with R used only to cross-check the survival estimates. Everything runs on a laptop except the BigQuery pulls.

| Layer | Choice | Why |
| --- | --- | --- |
| Data pulls | BigQuery Python client, `httpx` for PyPI and GitHub GraphQL | One language for the pipeline |
| Storage and features | Parquet + DuckDB, `polars` | Fast joins on a laptop |
| Pipeline orchestration | `dagster` or plain `make` + scripts | Re-run any step from scratch |
| Frequentist models | `lifelines`, `scikit-survival`; R `survival`, `frailtyEM`, `flexsurvcure` as a check | Quick baselines and estimate cross-checks |
| Bayesian engine | `numpyro` (JAX) | Fast enough for millions of person-months; posterior draws feed the simulator |
| Monte Carlo | `numpy` vectorised, then `numba` if needed | 10,000 runs × a few hundred nodes in seconds |
| Conformal layer | Own implementation, about 200 lines, tested against the paper's examples | Small and auditable |
| CLI | `typer` + `rich` | The coloured report shown above |
| GitHub Action | Docker action wrapping the CLI | Runs on pull requests |
| Dashboard | `FastAPI` backend + a React or Svelte front end with `d3` survival curves (or `streamlit` for v1) | Interactive tree and what-if views |
| Model serving | Precomputed per-package posteriors stored as compact files; scoring is pure simulation | No GPU or server needed to scan a repo |
| Quality | `pytest` + `hypothesis`, coverage in CI, the closed-form frailty test as a golden test | Statistical code that is actually tested |

**Repository layout:**

```
depmortality/
├── data/            # pull scripts: deps.dev, gharchive, pypi, github, osv
├── labels/          # state machine, bot filter, LLM label prompts + audit sets
├── features/        # monthly time-varying covariates, no look-ahead
├── models/
│   ├── baselines/   # inactivity rule, Cox, RSF, GBSA
│   ├── cure/        # EM mixture cure (frequentist)
│   ├── frailty/     # shared gamma frailty
│   └── bayes/       # numpyro multi-state model
├── simulate/        # Monte Carlo over dependency trees
├── conformal/       # lower bounds + drift fixes
├── evaluate/        # RQ1–RQ5 scripts, one per figure/table
├── cli/             # depmortality scan
├── action/          # GitHub Action
├── dashboard/
├── paper/           # LaTeX
└── tests/
```

**Engineering rules that keep the statistics honest:**

- Every feature function takes an `as_of` date and is tested to never read past it.
- Every random step takes an explicit seed, so any figure can be reproduced exactly.
- Models are versioned with the data snapshot they were trained on.

## Execution timeline

About 8 months at 10–12 hours a week: a working engine by February, results by mid-April, and a paper ready by 30 June 2027.

&#91;embedded content: depmortality build plan · 6 phases, 3 gates\]

### Gates

| Gate | Date | Pass if | If it fails |
| --- | --- | --- | --- |
| 1 | 29 Nov 2026 | The pilot shows enough failure events (on track for 1,000+ at full scale) and the bot filter and state machine pass a 100-package hand check | Widen the failure triggers, or use "goes quiet" as the main event |
| 2 | 10 Jan 2027 | Frequentist fits find a Quiet → Failed plateau (cure is plausible) and θ̂ is estimable; the plain model beats the inactivity rule | Drop whichever of cure or frailty fails; ship the other |
| 3 | 11 Apr 2027 | On 2023–26 outcomes, calibration beats the baselines and 90% bounds cover close to 90% | Report honestly; lead with the parts that held |

### Phase by phase

1. **Setup and pilot (Nov).** Repo skeleton, BigQuery and GitHub tokens, pull a 2,000-package pilot, write the bot filter and state machine, hand-check 100 packages.
2. **Data and baselines (Dec – early Jan).** Full PyPI pull, monthly features with `as_of` tests, the inactivity and Cox baselines, then frequentist cure (EM) and frailty fits. The winter break is the best time for the heavy data work.
3. **Engine (Jan – Feb).** The NumPyro multi-state model; the Monte Carlo simulator, which must pass the closed-form frailty test first; the conformal layer.
4. **Evaluation (Mar – mid-Apr).** RQ1–RQ5 on the frozen 2022 models, all ablations, and the 500-project test.
5. **Ship the tool (mid-Mar – early May, overlapping).** CLI first, then the GitHub Action; the dashboard last and optional. Release v0.1 on PyPI.
6. **Paper (mid-Apr – Jun).** Draft, get a mentor's review, post an arXiv preprint, then submit.

### Where to submit

- **Main paper:** ICSE moved to [a single summer submission cycle from 2027 on](https://icse-conferences.org/news.html), so the ICSE 2028 research deadline is likely mid-2027. Its call isn't out yet; ICSE 2027's mandatory abstract was due 23 June 2026. The 30 June target is set to be ready either way. Backups with autumn 2027 deadlines: the FSE 2028 and MSR 2028 research tracks. [FSE 2027's research deadline was 2 Oct 2026](https://conf.researchr.org/dates/fse-2027) and MSR 2027's was 23 Oct 2026, so expect similar timing.
- **Tool demo paper:** a short separate paper on the CLI and Action for a tool-demonstration track at the same venues.
- **Dataset:** the frozen labelled dataset can go to a data showcase track as its own small paper.

## Risks and fallbacks

Most risks have a fallback that still produces a publishable result. The one that needs active care is ethics: the tool makes predictions about real people.

| Risk | Early warning | Fallback |
| --- | --- | --- |
| Failure events are too rare or noisy to model | Fewer than \~1,000 failures in the pilot | Widen the trigger set; use "goes quiet" as the main event in v1 and add the cure model as a second stage |
| Cure fraction not identifiable (no plateau) | Kaplan-Meier on Quiet → Failed keeps falling; the Maller–Zhou test fails | Report it honestly; use the Mature classifier as a covariate instead of a latent class |
| Frailty turns out small (θ near 0) | Estimate near zero with a tight interval | That is a finding ("risk is mostly independent"); the tool still ships; lead with finished-vs-dead and the guarantees |
| Maintainer identity merging is wrong | Hand audit of 200 merges shows errors | Fall back to PyPI owner accounts; report a sensitivity analysis |
| BigQuery costs or rate limits | Pilot query costs above the free allowance | Shrink to the most depended-on packages; cache aggressively |
| Bayesian model is slow or won't converge | Divergences, poor mixing on the pilot | Ship the frequentist engine with bootstrap uncertainty; keep Bayesian as v2 |
| Scope creep (npm, dashboard polish) | Gate 2 slips | v1 = PyPI + CLI + GitHub Action; dashboard and npm are stretch goals |
| Scooped (e.g. a longitudinal follow-up of the PyPI propagation work) | New related arXiv papers | Post an arXiv preprint right after Gate 2 to timestamp the idea |
| Not enough hours alongside classes and placements | Two consecutive weeks under plan | Drop RQ5 or the dashboard first; never drop the temporal holdout |
| **Ethics: labelling real people as burnout risks** | Any view that ranks individuals | Show risk per *package*, not per person; maintainer views only inside a user's own dependency tree; hash IDs in the released dataset; no "likely to quit" labels on anyone; include an ethics statement in the paper |

**Get a statistics mentor.** The multi-state cure-frailty model is graduate-level statistics. A faculty co-author or advisor from statistics, or a professor whose work uses survival analysis, will catch mistakes reviewers would otherwise find.

## Sources

**Closest prior work**

- [Miller et al., ICSE 2025: dependency abandonment in npm](https://conf.researchr.org/details/icse-2025/icse-2025-research-track/16/Understanding-the-Response-to-Open-Source-Dependency-Abandonment-in-the-npm-Ecosystem) and [review slides noting the direct-only scope](https://plg.uwaterloo.ca/~migod/846/current/summaries/21-Kevin-npmAbandonment-slides.pdf)
- [Xu et al. 2025: predicting OSS abandonment](https://arxiv.org/abs/2507.21678v2)
- [Tsakpinis et al. 2026: dependency-propagated ecosystem impact in PyPI](https://arxiv.org/pdf/2605.06164)
- [Security issues in language-based ecosystems](https://arxiv.org/pdf/1903.02613)
- [Zimmermann et al., npm maintainer concentration (summary)](https://blog.acolyer.org/2019/09/30/small-world-with-high-risks/)
- [Beyond the Grave: dormancy and revival in scientific OSS](https://arxiv.org/pdf/2606.20966)
- Tools: [Ossuary](https://pypi.org/project/ossuary-risk/), [Infield](https://onceamaintainer.substack.com/p/identifying-unmaintained-open-source)

**Statistics**

- [Conformalized survival analysis (Candès, Lei, Ren)](https://www.ukbiobank.ac.uk/publications/conformalized-survival-analysis/)
- [No cure for cure models: identifiability under competing risks](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC13562180/)
- Classics to find on Google Scholar (not opened here): Peng & Yu, *Cure Models* (2021); Hougaard, *Analysis of Multivariate Survival Data* (frailty); Putter et al. 2007, multi-state models tutorial; Gibbs & Candès 2021, adaptive conformal inference

**Data**

- [deps.dev BigQuery dataset](https://docs.deps.dev/bigquery/v1/) and [enumerating dependents](https://blog.deps.dev/enumerating-dependents/)
- [PyPI trove classifiers](https://pypi.org/classifiers)
- [PEP 792: project status markers](https://peps.python.org/pep-0792/)
- [GH Archive](https://www.gharchive.org/)

**Venues**

- [ICSE single-cycle announcement](https://icse-conferences.org/news.html)
- [FSE 2027 important dates](https://conf.researchr.org/dates/fse-2027)
- [MSR 2027 important dates](https://conf.researchr.org/dates/msr-2027)
