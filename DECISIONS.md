# Decision log

Dated record of every non-obvious choice: the choice, the alternatives, and why.
Newest entries at the bottom. Phase 0 defaults below were proposed in the Phase 0 plan and
accepted by the owner ("go ahead") on 2026-10-26.

---

## 2026-10-26 — Phase 0 setup

**Tooling.** uv + Python 3.12 (`requires-python = ">=3.12,<3.13"`), src layout, ruff,
mypy --strict on `src/`, pytest + hypothesis, pre-commit, GitHub Actions CI. `make` is not
installed on the owner's Windows machine, so the single rebuild command is
`uv run python -m depmortality.pipeline pilot --cache-only`; the Makefile wraps it for
CI/Linux. Alternative considered: `just`/`nox` (one more tool to install; no benefit yet).

**Pipeline orchestration.** Plain Python entry point with sub-commands, no dagster.
The idea doc allowed either; this is the smaller dependency.

**duckdb deferred.** Phase 0 joins are small and done in polars. duckdb comes in Phase 1 for
the full-scale joins, as CLAUDE.md section 5 intends.

**Source documents moved** to `docs/` (PROMPTS.md, idea and plan).

## 2026-10-26 — Cost guardrails (rule 7)

- Every query is dry-run first; estimate logged and appended to `data/bq_ledger.jsonl`.
- `maximum_bytes_billed = max(10 MB, estimate) × 1.10` on every job, capped at the approved
  ceiling for approved queries.
- A query estimated above 50 GB (decimal GB, the conservative reading) runs only if its exact
  normalised text + parameters (SHA-256) is listed in `config/bq_approvals.yaml`.
- Added beyond the brief: a cumulative per-phase budget (`phase_budget_gb`, default 50 GB)
  so many sub-threshold queries cannot add up silently. Raising it needs an owner edit.

## 2026-10-26 — GH Archive strategy (plan item 1)

BigQuery bills by bytes in the columns scanned, regardless of row filters. GH Archive tables
are not clustered by repository, and nearly every field we need (`action`,
`author_association`, release details) is inside the `payload` JSON column, which is most
of each table's size. So a pilot-only scan costs the same as a full-scale scan, and day tables
cost the same as month tables for the same period.

**Choice:** one extraction pass over the monthly tables, done once, for the *whole candidate
repo set* (every PyPI package with a GitHub repo in deps.dev), written to a slim table in the
owner's dataset. The pilot and Phase 1 then read that small table. One query per year, each
needing explicit approval with its dry-run number.
**Alternative:** GitHub REST/GraphQL for the pilot only — free but slow, and a different
source than Phase 1, which would weaken what Gate 1 tells us.

**Filter on `repo.id`**, not `repo.name`, so renamed repositories are not lost.

## 2026-10-26 — Commits come from git, not PushEvent (plan item 2)

Commit history on the default branch comes from `git clone --bare --filter=tree:0
--single-branch` (commits only, no trees or blobs). Reasons: PushEvent payloads list at most
20 commits per push; matching pushes to the default branch needs the historical branch name
(many repos renamed master → main); git includes history imported from before a repo
moved to GitHub; and git gives author emails for identity merging. To verify during the
pull: whether GitHub dropped commit lists from PushEvent payloads in late 2025 (if so, that
affects the test period and is a further reason).
Activity date = committer date (for PR merges, this is when the maintainer merged).
Caveat: git shows the *current* default-branch history; force-pushed rewrites are invisible.
PushEvent is still extracted, as write-access evidence only (see maintainer replies).

## 2026-10-26 — States: dating vs. knowability (plan item 4)

Quiet is dated at the last activity L but only knowable at L + W. The state machine
produces `state_asof` (knowable at month M; covered by the no-look-ahead property test) and
a transitions table with `event_month` and `observable_month`. Dataset builders must keep
only transitions with `observable_month <= cutoff`; others are censored. Otherwise a 2021
quiet event confirmed in 2022 would leak calibration-period outcomes into training labels.

Further state-machine choices:
- The time origin (first PyPI upload) counts as activity, so every package starts Active.
- "No activity for W months" = W consecutive empty calendar months after L; Quiet is
  confirmed at L + W, a revival needs activity at L + W + 1 or later.
- After a revival the package is **Revived** (an active spell following a quiet one) and can
  go Quiet again. Alternative: return to Active. Keeping Revived preserves history for the
  multi-state model; it can be collapsed later.
- Failed is absorbing. A Quiet spell counts only if confirmed strictly before the failure
  month; otherwise the transition is Active/Revived → Failed. Activity after failure is
  ignored by the state machine (and should be reported as a diagnostic).
- A failure dated before the origin is clamped to the origin.
- Retrospective `state_final` marks Quiet from L + 1; it is for display and hand-checks only.

## 2026-10-26 — Maintainer activity

- Commits: any non-bot commit on the default branch (someone with write access accepted it).
- Releases: every PyPI version's first upload counts (no actor is available).
- Replies: IssueComment / PR review comment / PR review by a non-bot whose
  `author_association` is OWNER, MEMBER or COLLABORATOR. `author_association` is missing
  from older GH Archive payloads, so the fallback is as_of-aware: the replier is the repo
  owner, or pushed to the repo or carried a maintainer association on or before that day.
  Alternative rejected: current PyPI owners or all-time committers (look-ahead).
- Issues/PRs opened, pushes and GitHub releases are recorded but are not activity.

## 2026-10-26 — Bots

Login ends in `[bot]`; or login/name/email matches `config/bots.yaml`; or a conservative
heuristic on **logins only**: a separator-delimited token equal to `bot` alongside another
token (`pyup-bot` yes; `abbott`, `botanist`, a lone `bot` no). No heuristic on display
names, because people have surnames like "Bot".

## 2026-10-26 — Identity merging and hashing

Union-find over GitHub logins, commit emails and PyPI accounts. Merges only on: GitHub
noreply emails; (email, login) pairs reported by the GitHub API for a commit; a PyPI account
equal (case-insensitively) to a GitHub login that acts on the package's own repo. Names are
never used. Generic emails (`noreply@github.com`, `*@example.com`, `root@…`, …) are scoped
to one repository and never merged into a person. Person IDs in `data/processed/` are
HMAC-SHA256 with the secret `DEPMORTALITY_ID_SALT`; raw emails exist only in `data/raw` and
`data/interim`, which are git-ignored and never released.

PyPI maintainer *accounts* are not in the JSON API; they come from the XML-RPC
`package_roles` call, which reports **current** owners only. They are used only as the
fallback for the primary maintainer and flagged as a look-ahead source.

## 2026-10-26 — Failure triggers

- **(a) Archived.** GitHub `archivedAt`; if `isArchived` but the date is missing, the trigger
  is counted as "date unknown" and kept off the timeline. PEP 792 PyPI statuses `archived`
  and `deprecated` count; `quarantined` does not (that's a malware response). The PyPI APIs
  give no status date, so these are also "date unknown". Gate 1 reports coverage.
- **(b) Advisory.** Failure dated at `published + 90 days` (when it becomes knowable) if no
  non-yanked release outside the affected set was uploaded by then. "Outside the affected
  set" = version ≥ the smallest `fixed` version; else > `last_affected`; else > the largest
  explicitly listed affected version. Only ECOSYSTEM ranges are used. Advisories whose
  deadline falls after the freeze are censored. Advisories withdrawn on or before the
  deadline are ignored; a withdrawal after the deadline does not undo the failure (it was not
  knowable at the time).
- **(c) Python support (plan item 5).** Fires only on positive evidence of non-support at
  X.Y.0 + 12 months: the latest release by then has a `requires_python` that rules out X.Y
  (and X.Y+1, X.Y+2, so a raised floor is not mistaken for a cap), or the package ships
  version-specific CPython wheels and none for X.Y (abi3 wheels for ≤ X.Y and pure wheels
  count as support). Pure-Python and sdist-only packages without a cap are treated as
  supported. Requires prior Python 3 evidence (`python_support_requires_py3_evidence`), so
  Python-2-only packages from 2012–14 are not mass-labelled failures. Classifier lists are
  recorded but are not evidence of non-support (most packages never update them).
  Alternative rejected: "classifier list lacks X.Y" — would flood the data with false failures.
- **(d) Statement.** Out of scope for Phase 0: `statement_trigger` and
  `llm_labels.classify_statement` raise `NotImplementedError`.
- `Development Status :: 7 - Inactive` is recorded as a dated candidate signal (first upload
  carrying it), never a failure. `6 - Mature` is recorded for validation only.

**Python release dates** (`config/python_releases.yaml`): taken from
https://www.python.org/downloads/ on 2026-10-26. Note 3.4.0 is listed there as 2014-03-17.

## 2026-10-26 — Pilot selection (plan item 6)

- Universe: PyPI packages in the latest deps.dev snapshot with ≥ 1 direct dependent, a
  resolvable GitHub repo, and a first PyPI upload in 2012–2022 (the idea doc's cohorts).
- 1,000 most depended-upon (by distinct direct-dependent package names, latest snapshot),
  all included; 1,000 from the rest, stratified by direct-dependent bin
  (edges 1, 2, 5, 20, 100+) × first-release year, allocated proportionally (≥ 1 per
  non-empty stratum), seeded. Inverse-probability weights are stored with the pilot, so the
  full-scale projection is a weighted (Horvitz–Thompson) estimate, not top-heavy.
- **Known bias:** selecting on *current* dependents conditions on the future (survivors).
  Acceptable for Gate 1 viability counts; Phase 1 should select from a historical snapshot.
- Monorepos (one repo linked to several packages) are flagged and counted, not excluded.

## 2026-10-26 — Data freeze date

`data_freeze: 2026-10-31` (end of the last complete GH Archive month before Gate 1).
Alternative: 2026-11-30, which would push the pulls past the Gate 1 date.

## 2026-10-26 — Cache key date

The cache "date" key is `pilot.snapshot_date` from config, not the run date, so a re-run
the next day hits the cache. A new snapshot = a new date in config = a fresh pull.
