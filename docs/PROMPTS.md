# depmortality: Claude Code prompts, phase by phase

**How to use this file**

1. Create an empty folder, `git init`, and put `CLAUDE.md` at its root. Claude Code reads it automatically every session.
2. Start Claude Code in that folder. Paste the **Phase 0** prompt below.
3. Claude Code will show a plan first. Read it, correct anything, then say "go".
4. Each phase ends with a gate report in `reports/`. Read it, decide, then paste the next phase's prompt.
5. Before Phase 0, set these in your shell or a git-ignored `.env`: `GITHUB_TOKEN`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_APPLICATION_CREDENTIALS`. You need a Google Cloud project with the BigQuery API enabled.

---

## Phase 0 — Setup and pilot (target: by 29 Nov 2026)

```
Read CLAUDE.md fully. We are starting Phase 0: repository setup and a
2,000-package pilot. Do NOT write any modelling code in this phase.

Before coding, show me a plan (files, tests, order of work, risks, and the
BigQuery queries you intend to run with their dry-run byte estimates). Wait
for my OK.

GOAL
Build the data and labelling pipeline end to end on a pilot set, and produce
a Gate 1 report that tells us whether the full project is viable.

TASKS

1. Scaffold the repository exactly as laid out in CLAUDE.md section 5:
   - uv project, Python 3.12, src/ layout, package name `depmortality`
   - ruff, mypy --strict on src/, pytest + hypothesis, pre-commit
   - GitHub Actions CI: lint, type-check, tests
   - .gitignore covering data/, .env, caches
   - config/ with splits.yaml, thresholds.yaml, bots.yaml, pilot.yaml,
     loaded through pydantic models in src/depmortality/config.py
   - DECISIONS.md with a first entry recording the Phase 0 setup choices

2. Utilities (src/depmortality/utils/):
   - cache.py: on-disk cache of raw responses keyed by (source, query hash,
     date), with a --refresh override
   - bq.py: a BigQuery wrapper that ALWAYS dry-runs first, logs estimated
     bytes, sets maximum_bytes_billed, and raises unless I've approved any
     query above the threshold in config (default 50 GB)
   - http.py: httpx client with retries, backoff, and GitHub rate-limit
     awareness (read the rate-limit headers, sleep until reset)
   - seeding.py and logging setup; tokens must never appear in logs

3. Pilot selection (data/depsdev.py):
   - From deps.dev BigQuery (bigquery-public-data.deps_dev_v1), pick 2,000
     PyPI packages: the most depended-upon packages with a resolvable GitHub
     repository, plus a stratified random sample from the long tail so the
     pilot isn't only famous packages. Put the exact split in pilot.yaml and
     log the rule in DECISIONS.md.
   - Save the pilot list with the selection rule and snapshot date.

4. Pullers, each cached and idempotent:
   - pypi.py: release history (dates, versions, yanked flags), classifiers
     per release (Development Status included), requires_python, maintainer
     accounts, project URLs
   - gharchive.py: monthly aggregates per pilot repo from
     githubarchive.month.* (or day tables if cheaper), restricted to pilot
     repos: pushes with commit authors, releases, issue/PR opened, issue/PR
     comments with actor login and author_association, PR reviews.
     Dry-run first; show me the byte estimates before running.
   - github.py: GraphQL for isArchived (and archive date if available),
     default branch, owner type (user/org), README text
   - osv.py: advisories per package with affected/fixed versions and
     published dates
   - Python release dates: a small static table in config, with its source
     cited in DECISIONS.md

5. Labelling (labels/):
   - bots.py: bot detection (logins ending in [bot], config/bots.yaml list,
     plus a conservative heuristic). Unit tests with real-looking examples.
   - identity.py: merge PyPI accounts, GitHub logins and commit emails into
     one person ID; store only salted hashes in anything under processed/.
   - panel.py: one row per (package, month) with maintainer activity counts
     AFTER bot removal. Months start at the first PyPI release.
   - states.py: the Active / Quiet / Revived state machine from CLAUDE.md
     section 3, thresholds from config. Quiet is dated at the LAST activity.
   - triggers.py: Failed triggers (a)–(c) from CLAUDE.md. Trigger (d), the
     LLM statement extractor, is OUT OF SCOPE for Phase 0: leave a stub that
     raises NotImplementedError and note it in DECISIONS.md.
   - Record "Development Status :: 7 - Inactive" as a candidate signal, not
     a failure.

6. Tests that must exist and pass:
   - State machine on hand-built synthetic timelines: never active, active
     then quiet, quiet then revived, revived by a bot only (must NOT count),
     archived while active, multiple quiet spells.
   - Property test (hypothesis): appending activity rows dated after a
     month M never changes any state or panel value for months <= M.
   - Bot filter tests; identity-merge tests; the BigQuery guard refuses an
     over-threshold query.

7. Hand-check tool:
   - A script that samples 100 pilot packages (seeded) and writes
     reports/gate1_handcheck.csv with each package's monthly timeline,
     detected states and triggers, plus empty columns for my verdict.
     I will fill it in by hand.

8. Gate 1 report (script-generated, reports/gate1.md + figures):
   - Counts: packages pulled successfully vs. failed (with reasons), quiet
     transitions, revivals, failures by trigger type, Inactive-classifier
     packages and their overlap with failures, Mature-classifier packages
   - Kaplan-Meier curve of Quiet -> Failed time (does it look like it
     plateaus?), and of time to first Quiet
   - A projection of the failure count at full PyPI scale, stating the
     assumption it rests on
   - Total BigQuery bytes processed, and API calls made
   - A plain-language verdict: viable / risky / not viable, with reasons
   Every number must come from the pipeline. If something couldn't be
   computed, say so in the report rather than estimating it.

DEFINITION OF DONE
- CI green; all tests above pass
- `make pilot` (or an equivalent single command) rebuilds everything from
  the cache
- reports/gate1.md and reports/gate1_handcheck.csv exist
- DECISIONS.md is up to date
Then STOP and summarise: what worked, what didn't, the key numbers, and
what you'd change before scaling up. Do not start Phase 1.
```

---

## Phase 1 — Full data and baselines (target: by 10 Jan 2027)

```
Read CLAUDE.md and reports/gate1.md (plus my hand-check verdicts in
reports/gate1_handcheck.csv, and the changes I asked for after Gate 1).
We are starting Phase 1. Show me a plan with BigQuery cost estimates first.

GOAL
Scale the pipeline to all qualifying PyPI packages, build time-varying
features, fit baselines and first frequentist versions of the cure and
frailty models, and produce the Gate 2 report.

TASKS
1. Fix every issue found at Gate 1. Log each fix in DECISIONS.md.
2. Scale to all qualifying PyPI packages (at least one dependent in deps.dev
   and a resolvable GitHub repo). Run in resumable batches.
3. Implement trigger (d): an LLM classifier that reads the README and the
   last 20 maintainer comments and labels "explicitly unmaintained /
   deprecated", "feature complete / finished", or "neither". Keep the prompt
   in a versioned file. Produce a 200-item stratified audit sheet for me to
   label by hand, and report precision/recall once I've labelled it. The
   "finished" label is VALIDATION ONLY (CLAUDE.md section 3).
4. features/: monthly time-varying covariates, every function taking as_of:
   activity in rolling 3/6/12-month windows and trend slope; issue
   first-response time; PR merge latency; active maintainers; truck factor;
   org vs. user owner; maintainer portfolio size and cross-repo activity;
   dependents over time (deps.dev snapshots); downloads; Python-support
   lag; dependence on already-failed packages; development-status
   classifier. Each feature gets an as_of property test.
5. Build counting-process datasets (start, stop, event) for each
   transition: Active->Quiet, Quiet->Revived, Quiet->Failed, Active->Failed.
   Apply the splits from config/splits.yaml.
6. Baselines on the training period:
   - Inactivity rule ("no commit for 12 months = dead")
   - Cox models per transition (lifelines), proportional hazards checked
     with Schoenfeld residuals
   - Random survival forest and gradient-boosted survival (scikit-survival)
7. First frequentist versions of the new models:
   - Mixture cure model for Quiet->Failed via EM (own implementation),
     cross-checked against R flexsurvcure or smcure on a sample
   - Shared gamma frailty for Active->Quiet (cross-check with R frailtyEM
     or coxme). Report theta, its CI, and Kendall's tau.
8. Gate 2 report (reports/gate2.md): dataset size and event counts per
   transition; KM plateau evidence and a test for the presence of cures
   (e.g. Maller-Zhou); theta-hat with a CI and a likelihood-ratio test of
   theta = 0 (boundary-corrected); baselines evaluated on the 2022
   validation period only. Verdict per contribution: keep / drop / revise.

Do not touch test-period (2023+) outcomes in this phase except to confirm
they exist. Stop after the Gate 2 report.
```

---

## Phase 2 — The engine (target: by 28 Feb 2027)

```
Read CLAUDE.md and reports/gate2.md. Phase 2: the Bayesian model, the Monte
Carlo simulator, and the conformal layer. Plan first.

ORDER OF WORK (tests first, always)
1. Golden test FIRST: write tests/test_simulator_closed_form.py checking that
   for one maintainer's packages P(no failure by h) =
   (1 + theta * sum Lambda_i(h))^(-1/theta), and exp(-sum Lambda_i(h)) as
   theta -> 0, within Monte Carlo error at S = 100,000. It must fail before
   the simulator exists.
2. simulate/: vectorised Monte Carlo over a dependency tree.
   - Inputs: resolved tree (deps.dev transitive closure), node -> maintainer
     map, per-node posterior draws, current age and state per node
   - Per run: draw parameters from the posterior; one Gamma frailty per
     maintainer; sample each node's future CONDITIONAL on current age via
     T = S^-1(U * S(a)); for quiet nodes draw finished ~ Bernoulli(pi);
     compete failure vs. revival
   - Outputs: project survival curve with bands, P(>=1 failure) at
     12/24/36 months, expected failures, P(>=k in one year), per-node risk,
     per-maintainer attribution (internal only; see ethics rules)
   - Make the golden test pass. Benchmark: 10,000 runs on a 300-node tree.
3. models/bayes/: NumPyro multi-state model.
   - Piecewise-exponential baselines via the Poisson trick on person-month
     data (aggregate identical covariate cells where possible)
   - Gamma frailty per maintainer on Active->Quiet; latent cure on
     Quiet->Failed; cause-specific hazards for Revived and Active->Failed
   - Simulation-based calibration on synthetic data with KNOWN theta and pi
     BEFORE touching real data: show the model recovers them. This is
     mandatory; report the results.
   - Then fit on the training period; report convergence (R-hat, ESS,
     divergences). Fall back to SVI only if NUTS is infeasible, and log it.
   - Export compact per-package posterior draws, versioned by data snapshot.
4. conformal/: conformalized survival lower bounds (Candès, Lei & Ren 2023)
   wrapping the fitted model, plus two drift fixes: recent-cohort
   calibration and adaptive conformal inference. Unit-test coverage on
   synthetic exchangeable data first.
5. A library API: `depmortality.score(lockfile_or_repo) -> Report` that ties
   it together. No CLI yet.

Stop and report: golden test results, SBC results, convergence diagnostics,
and the simulator benchmark. Do not evaluate on the test period yet.
```

---

## Phase 3 — Evaluation (target: by 11 Apr 2027)

```
Read CLAUDE.md. Phase 3: evaluation on the held-out future (2023 to the data
freeze). Plan first, and list every number you will report BEFORE computing
any of them; I will approve the list. After that, nothing may be retuned on
test outcomes.

Freeze all models as trained on data up to 2021-12-31 and calibrated on
2022. Then, one script per item in src/depmortality/evaluate/:

RQ1 Finished vs dead: AUC and calibration of pi against the Mature
    classifier and the hand-labelled finished set; post-quiet failure rates
    by pi decile.
RQ2 Correlated failure: theta and tau with credible intervals; observed vs.
    predicted distribution of failures per maintainer-year under theta-hat
    and under theta = 0.
RQ3 Package-level: time-dependent AUC, Uno's C, integrated Brier score,
    calibration slope/intercept at 12 and 24 months, against every baseline.
RQ4 Project-level: about 500 real GitHub projects' lockfiles as of
    2022-12-31 (seeded sample; selection rule logged). Predicted vs.
    observed P(>=1 failure in 24 months): reliability diagram and Brier.
RQ5 Conformal: empirical coverage and bound width of the 90% lower bounds
    for each drift fix.
Ablations: no cure; no frailty; snapshot covariates instead of time-varying
    (this demonstrates immortal time bias on our own data); no portfolio
    covariates; plug-in vs. posterior.
Hygiene: paired bootstrap CIs for every metric difference; threshold
    sensitivity (quiet 6/12/24 months, advisory window 60/90/180 days);
    leakage check (rebuild test features from a dataset frozen at
    2022-12-31; scores must not change).

Every figure and table is generated by a script into reports/figures and
reports/tables. Write reports/gate3.md with the results, including the ones
that didn't go our way. Stop there.
```

---

## Phase 4 — Ship the tool (target: v0.1 by 9 May 2027)

```
Read CLAUDE.md. Phase 4: user-facing tools on top of depmortality.score().
Plan first.

1. CLI (typer + rich): `depmortality scan <path-or-repo>`
   - Parses requirements.txt, poetry.lock and uv.lock
   - Report: project-level block (P(>=1 critical failure) with interval,
     expected failures, correlated-tail line vs. the independence model),
     highest-risk dependencies table (depth, P(dead,24m), P(finished),
     guaranteed lower bound, short "why"), maintainer concentration within
     the user's own tree only
   - --json output; --horizon; --runs; --seed
   - Model files downloaded on first run, version-pinned and checksummed
2. GitHub Action (Docker): runs on PRs that change a lockfile; comments
   when a new dependency raises 24-month risk past a configurable
   threshold; fails only when the user opts in.
3. Docs: README with install, an example report, what the numbers mean,
   their limitations, and the ethics statement.
4. Packaging: build and publish v0.1 to TestPyPI first; I approve before
   the real PyPI upload.
5. (Stretch, only if I say so) Dashboard: FastAPI + a small front end with
   a project survival curve, a risk-coloured tree, and what-if replacement.

Ethics check before release: no screen or output ranks individuals by
likelihood to quit. Show me the output samples you used to verify this.
```

---

## Phase 5 — Paper support (target: draft by 20 Jun 2027)

```
Read CLAUDE.md and reports/gate3.md. Phase 5: help me produce the paper.
I write the arguments; you make the artifacts exact and reproducible.

1. paper/: an IEEE/ACM LaTeX skeleton (I'll tell you the venue template)
   with sections: introduction, background, data, method, evaluation
   (RQ1-RQ5), tool, threats to validity, ethics, data availability.
2. Every number in the paper comes from a generated macro file
   (paper/numbers.tex) written by a script. No hand-typed results anywhere.
3. A one-command reproduction: `make paper` rebuilds data (from cache),
   models, figures, tables, numbers.tex, and the PDF.
4. A replication package: code, frozen dataset with hashed IDs, model
   files, and a README; ready to archive on Zenodo.
5. A threats-to-validity checklist pre-filled from DECISIONS.md, for me to
   turn into prose.
Flag any claim in my drafts that the results don't support.
```

---

## Prompts for when things go wrong

- **Drifting from the rules:** "Stop. Re-read CLAUDE.md section 2 and check whether what you just did violates any rule. Report, then fix."
- **A number looks too good:** "Before we trust this result, hunt for leakage: list every feature's as_of handling and any join that could pull in future data. Run the leakage check."
- **Stuck on a model:** "Go back to synthetic data with known parameters. Show the model recovers them before we touch real data again."
- **Scope creep:** "Is this needed for the current gate? If not, write it in BACKLOG.md and move on."
