# BigQuery dry-run report — Phase 0

Generated 2026-10-25 20:45 UTC by `pipeline dryrun`. deps.dev snapshot: 2026-10-19 21:00. Dry-runs are free.
Per-query cap: 50.0 GB. Phase budget: 50 GB; already billed this phase: 0.031 GB.

## Estimates

| Query | Estimated GB | Needs approval | sha256 |
|---|---:|---|---|
| Q2 repo mapping | 1.032 | no | `9462bd8fd9ab5cdf59be22f510bfb6d27f5a935bd5b1d1cdcce5eddd11f49ba3` |
| Q3 direct dependents | 10.767 | no | `a325e010304bac0b4baa50fd7ee435e4e4effde7d68901992c681428264e527b` |
| Q4a first upload | 0.590 | no | `2290406823c50cebcb8fabad19cdc894b3665b54df0e9d99064cac90412d2133` |
| Q5 GH Archive 2012 (cost preview, placeholder repos) | 28.860 | no | `c2a35ef50be51879f6cc9bae7f005a9466c580abb388478bb3c1f53ba7be00d4` |
| Q5 GH Archive 2013 (cost preview, placeholder repos) | 78.034 | yes | `981f0c0b6dfd22a3733244249d9a480d10f582fb7708b19d96008a55be9709df` |
| Q5 GH Archive 2014 (cost preview, placeholder repos) | 162.103 | yes | `2b00ae1f9c66b630210fae672c85195e81cd7420fd94e55d9d93b9fedcff7d40` |
| Q5 GH Archive 2015 (cost preview, placeholder repos) | 499.600 | yes | `3de8f3724fa2e20119272dd1ba9dd5a08c2797fd3ef5d932222c1dd22e127057` |
| Q5 GH Archive 2016 (cost preview, placeholder repos) | 769.452 | yes | `761ea423d983572c5f397eaa5af37ff05441b5b35e35c8afb629818f21c84929` |
| Q5 GH Archive 2017 (cost preview, placeholder repos) | 972.075 | yes | `c8505de5a11893665c9deeaebdff557d4ae4ac451619288017498cc908b5c0f9` |
| Q5 GH Archive 2018 (cost preview, placeholder repos) | 1,158.370 | yes | `a6ee23c0804e41d07916771c0de4ba7bfcf189b2b55067376c54eccf222d6fa0` |
| Q5 GH Archive 2019 (cost preview, placeholder repos) | 1,750.734 | yes | `9788f9b685dbe9503f085c686cbc38c47568ea02727974c506d67b50719b836d` |
| Q5 GH Archive 2020 (cost preview, placeholder repos) | 3,107.531 | yes | `37cfcd460cf9b72df16d199c9f01ffee2e9f76159d38f9d32efc1331273f8b07` |
| Q5 GH Archive 2021 (cost preview, placeholder repos) | 3,649.695 | yes | `52419397e898ead3255b4d8403848ce814faf75a47632613e17c3e39b9cc8117` |
| Q5 GH Archive 2022 (cost preview, placeholder repos) | 5,102.851 | yes | `e2927871ef8089dd5120b7b9f501cbef2eb518871acd3c58c391f4ad686c0d3f` |
| Q5 GH Archive 2023 (cost preview, placeholder repos) | 4,787.690 | yes | `6e381c14f0e294dab858007292602e2a5b27cb9b9c296e1260ff2889e04998f7` |
| Q5 GH Archive 2024 (cost preview, placeholder repos) | 5,321.782 | yes | `96ddbac1b6f85381bba4f79823bb7c53e894e41c3c65a19c27730bcf867600ec` |
| Q5 GH Archive 2025 (cost preview, placeholder repos) | 4,337.074 | yes | `6d392c2943c3b6eed36d740e915599aae9dbb12301518083ca45631c96b505a7` |
| Q5 GH Archive 2026 (cost preview, placeholder repos) | 691.077 | yes | `dd6e2c9b1093ab833ece93e994d7c1a26209f276a84240ea3907d7ba9a5c3833` |

Total if all run: 32,429.319 GB.

## Not yet estimable

- Q4b release classifiers — needs the pilot list (stage `select`)
- Q5 GH Archive extraction 2012 — needs resolved candidate repos (stage `resolve`)
- Q5 GH Archive extraction 2013 — needs resolved candidate repos (stage `resolve`)
- Q5 GH Archive extraction 2014 — needs resolved candidate repos (stage `resolve`)
- Q5 GH Archive extraction 2015 — needs resolved candidate repos (stage `resolve`)
- Q5 GH Archive extraction 2016 — needs resolved candidate repos (stage `resolve`)
- Q5 GH Archive extraction 2017 — needs resolved candidate repos (stage `resolve`)
- Q5 GH Archive extraction 2018 — needs resolved candidate repos (stage `resolve`)
- Q5 GH Archive extraction 2019 — needs resolved candidate repos (stage `resolve`)
- Q5 GH Archive extraction 2020 — needs resolved candidate repos (stage `resolve`)
- Q5 GH Archive extraction 2021 — needs resolved candidate repos (stage `resolve`)
- Q5 GH Archive extraction 2022 — needs resolved candidate repos (stage `resolve`)
- Q5 GH Archive extraction 2023 — needs resolved candidate repos (stage `resolve`)
- Q5 GH Archive extraction 2024 — needs resolved candidate repos (stage `resolve`)
- Q5 GH Archive extraction 2025 — needs resolved candidate repos (stage `resolve`)
- Q5 GH Archive extraction 2026 — needs resolved candidate repos (stage `resolve`)

## To approve

Add an entry per query to `config/bq_approvals.yaml` (sha256 from the table, max_gb a little above the estimate), and raise `phase_budget_override_gb` to cover the total. Cost previews with placeholder repos are not approvable: their hash changes once the real repo list exists.
