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

## 2026-10-26 — Implementation choices made while building Phase 0

- **No git history → not analysed.** A package whose clone failed would look falsely Quiet,
  so it is excluded from the panel and counted as a pull failure with its reason. Clone
  failures are cached (so a cache-only rebuild reproduces them); `--refresh` retries them.
- **GH Archive rows.** Deduplicated by event id; comment edits/deletions dropped (only
  `created`/`submitted`/`opened`/`published` actions count). Rows match on `repo.id` or on the
  lower-cased repo name, because `repo.id` may be missing in early years. Renamed repos map
  back to the deps.dev slug.
- **Quiet → Failed KM is measured from Quiet confirmation (L + W)**, not from L. Under the
  state-machine rule, a failure inside the W-month window is Active → Failed, so measuring
  from L would put a structural zero in the first W months. **Open question for Phase 1:**
  the multi-state model has the same tension: the Quiet state is entered at L, but
  failures in (L, L + W) are counted as Active → Failed.
- **Repo slugs in processed tables.** `owner/repo` is public package metadata and is kept for
  joins, but for user-owned repos the owner is a person's handle. The released dataset
  (Phase 5) must hash or drop it.
- **PEP 792 exposure checked live (2026-10-26):** the Simple API JSON (api-version 1.4)
  returns `project-status` (e.g. six → `active`). Gate 1 reports how many pilot packages
  are archived/deprecated; the API gives no status date.
- **Full-scale projection** = stratified Horvitz–Thompson total of failed packages using the
  pilot's selection weights, nonresponse handled as missing-at-random within strata, SRSWOR
  variance with finite-population correction. Stated in the report with its assumptions.
- **Gate 1 verdict rule** (thresholds in `config/thresholds.yaml`, `gate1:`): viable if the
  projection ≥ 1,000; risky if 500–1,000, if the 95% interval's lower end is below 1,000, or if
  more than 20% of pilot packages failed to pull; not viable below 500. Always provisional
  until the owner scores the hand check.
- **Test fixtures** are trimmed real API responses (six, numpy, one Pillow advisory) with
  author names and emails replaced by placeholders, so no real emails are committed.

## 2026-10-26 — GH Archive source: free ClickHouse mirror instead of BigQuery (provisional)

The BigQuery dry-run priced the one-pass extraction at **32,429 GB** (2012–2026; 2022 alone
5.1 TB), i.e. roughly US$185 on-demand. Alternative found: the public ClickHouse playground
(`play.clickhouse.com`, user `play`, table `github_events`) mirrors GH Archive with
`author_association`, `action` and all needed event types, indexed by repo name.
**Verification against BigQuery (2026-10-26):** per-type event counts on 2020-01-15 match
(IssueCommentEvent identical; PullRequestEvent 181,042 vs 181,091); total and comment counts
on 2026-01-15, 2026-07-15 and 2026-10-15 are identical. Cost of the checks: < 0.2 GB.
**Choice (provisional, owner to confirm):** `pilot.gha_source: clickhouse_mirror`.
BigQuery remains selectable (`bigquery`).
Known differences: no `repo.id`, only the name at event time, so events under a repo's
earlier names are missed (e.g. `mitsuhiko/flask` until 2016-03, `kennethreitz/requests`);
`author_association` `NONE` also encodes "not recorded" (mapped to null outside the
recording window); no issue-vs-PR flag on comments (both count as replies). Playground limits:
1M result rows and 60 s per query; the client splits batches on limit errors.
**Threat to validity:** a third-party copy. Keep the sampled BigQuery cross-checks.
**Rename gap fix, needs approval:** a BigQuery query reading only `repo.id` + `repo.name`
(no payload) recovers every historical name per repo; dry-run 418 GB (~US$2.4).

## 2026-10-26 — Data freeze moved to 2025-09-30 (provisional)

Measured on the mirror and confirmed on BigQuery: GH Archive payloads stop carrying
`author_association` after 2025-10 (share of IssueCommentEvents with an association:
70% in 2025-09, 27% in 2025-10, 0% from 2025-11), consistent with GitHub's late-2025 Events
API payload change; and comment events nearly vanish in mid-2026 (2026-07-15: 4,974
IssueCommentEvents vs 118,803 on 2026-01-15), with total volume also collapsing by
2026-10-15. Both are upstream GH Archive issues, not mirror issues. Git and PyPI data are
unaffected. To avoid falsely "Quiet" packages in the late test period, `data_freeze` moves
from 2026-10-31 to **2025-09-30**. The test period becomes 2023-01 → 2025-09 (33 months).
That still covers 24-month horizons from end-2022 lockfiles, with 9 months to spare.
`author_association` is recorded from **2017-09**; before that the as_of fallback applies.

## 2026-10-26 — No paid queries; rename recovery done for free

Owner instruction: spend no money, accept a somewhat compromised project. All BigQuery work
stays inside the 50 GB phase budget (well within BigQuery's free monthly query allowance),
and the 418 GB rename-history query is dropped. Instead, earlier GitHub names of pilot repos
come from GitHub URLs in each package's historical PyPI metadata (`home_page`,
`download_url`, `project_urls`; added to the pilot classifier query Q4b). A candidate counts
as an alias only if GitHub resolves it to the same repository id (GitHub redirects renamed
repos). If the old name now belongs to a different, newer repository, the alias holds only
for events before that repository's creation date. Renames never written into PyPI metadata
are still missed: a known, unquantified undercount of early replies.

## 2026-10-26 — Commit histories held columnar (memory)

The pull stage was stopped twice by the host for low memory. Cause: 3.86 M commits across
1,930 repos were held as Python objects and copied once per package sharing a repo
(e.g. aws/aws-cdk × 13). Commits are now parsed straight into one polars frame per repo
(~2 MB for aws-cdk's 18k commits) and joined to packages once. Parallel clones cut from 4 to 2.
The columnar parser is checked against the object parser (identical dates on aws/aws-cdk).

## 2026-10-26 — GH Archive mirror rows streamed to disk (memory)

The third pull run was stopped for low memory near the end of the mirror pull (pytorch alone
is ~2 M kept events). The mirror queries now keep only the actions that `to_events` uses
(`opened`, `created`, `submitted`, `published`, and none for pushes). This is checked on
benjaminp/six: identical event counts by kind before and after. Event ids are hashed, and
each query's rows are mapped to events and written to disk as they arrive, instead of being
held in memory together. Commit rows are freed before the mirror pull.

## 2026-10-26 — OSV from the bulk export; stable commit-login batches

`api.osv.dev` resets every connection from the pilot machine's network (IPv4 and IPv6, curl
and httpx alike). The earlier pull therefore wrote an empty advisories table. This would have
silently meant "no advisories" for trigger (b). Fix: read OSV's free bulk export for PyPI
(`osv-vulnerabilities.storage.googleapis.com/PyPI/all.zip`, 37 MB, exported 2026-10-26
03:48 UTC), cached under the snapshot date and parsed with the same `parse_vuln`. Pilot result:
197 packages with at least one advisory, 4,710 advisory entries. If the export cannot be read,
the pull now fails instead of writing an empty table.
Also fixed: the per-repo commit lists sent to GitHub were in a different order on each run
(unordered group_by), so cached responses were never reused. Batches are now sorted by sha.
