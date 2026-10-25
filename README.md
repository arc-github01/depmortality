# depmortality

Predicts when a project's dependencies will stop being maintained, with calibrated
probabilities and guaranteed lower bounds. PyPI only for v1.

Status: **Phase 0** (data and labelling pilot). Not usable as a tool yet.

- Standing brief: [CLAUDE.md](CLAUDE.md)
- Decision log: [DECISIONS.md](DECISIONS.md)
- Project plan: [docs/idea_and_plan.md](docs/idea_and_plan.md)

## Development

```
uv sync --all-groups --python 3.12
uv run pytest
uv run python -m depmortality.pipeline --help
```

Credentials are read from environment variables or a git-ignored `.env`
(see `.env.example`).
