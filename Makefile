.PHONY: install lint typecheck test check dryrun pilot

PIPE = uv run python -m depmortality.pipeline

install:
	uv sync --all-groups --python 3.12

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy

test:
	uv run pytest -m "not network"

check: lint typecheck test

# Free: dry-runs every Phase 0 BigQuery query and writes reports/bq_dryrun_phase0.md
dryrun:
	$(PIPE) dryrun

# Rebuilds everything from data/raw (cache-only; a cache miss is an error)
pilot:
	$(PIPE) pilot --cache-only
