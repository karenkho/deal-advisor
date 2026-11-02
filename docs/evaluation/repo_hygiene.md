# Repository hygiene (Step 5.1a)

Scanned at commit `8a02570`, all branches and history. Findings are reported by label and count only.

Commands: `docker run --rm -v "$PWD:/repo:ro" zricethezav/gitleaks:v8.18.4 detect -s /repo --redact --no-banner`, plus a local scan of the tree and of `git log --all -p`.

## Secrets

| Check | Result |
|---|---|
| gitleaks, whole history | 30 hits, all the `generic-api-key` rule in 5 cached result files |
| Those 30 hits, reviewed | False positives: a field named `key` holding a document key such as a bid bulletin's name |
| Every secret value from `.env`, tree and history | The Azure OpenAI endpoint URL was found in the tree and history; no key, password, token or connection string was |

## Identifying values

| Item | Current tree | History |
|---|---|---|
| App URL, subscription ID and name, registry, resource group, OpenAI resource, Postgres server, alert email | Replaced in `README.md`, `docs/deploy_preflight.md`, `infra/*`; remain in the plan files | Present |
| Drive root folder ID | Still in `config/storage.yaml`: the deployed image needs it | Present |
| Client names derived from private file names | Not found outside `POC_V3_WORKFLOW_PLAN.md`, which names the 2 client repositories | Same |
| Company name | In `app/proposal.py` as the vendor name, `app/brd_docx.py`, plan files and cached proposals | Present |
| Rate file name, MSA and BRD template file names, private folder name | In plan files; the private folder also in `scripts/drive_auth.py`; the MSA file name in a test | Present |

Real values are in `docs/private/`, which is git-ignored.

## Publishing proposal (nothing created or pushed)

A new `capstone` branch, one commit, cut from `poc-v3` or later, so no old commit is ever pushed.

Left out of that commit: `POC_PLAN.md`, `POC_V2_PLAN.md`, `POC_V2B_PLAN.md`, `POC_V3_WORKFLOW_PLAN.md`, `PHASE5_CAPSTONE_PLAN.md`, `PHASE5_V3_ADDENDUM.md`.

Changed in that commit only: the Drive folder ID in `config/storage.yaml` becomes a placeholder, and `scripts/drive_auth.py` no longer names the private folder.

Before pushing: rerun both scans on the branch. It is pushed only when a remote is given. Also left out: the reviewer's working note `eval/REVIEW_PACK.md`.

## Configuration

`.env.example` now lists every variable the code reads. `.gitignore` covers `.env`, `.claude/settings.local.json`, `data/`, `docs/private/` and `.cache/`.

The branch as finally prepared is described in the push-readiness checklist at stop point 5E.
