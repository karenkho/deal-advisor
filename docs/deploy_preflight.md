# Deployment pre-flight (read-only)

Run on 2026-10-29. Nothing has been created in Azure.

- HOST_SUB: <subscription-name> (`<subscription-id>`), the same subscription as the Azure OpenAI resource
  (which is not modified).
- Resource group: `<resource-group>` · region: `southeastasia` · app: `<app-name>` · budget alerts: <alert-email>
  (USD 5, 50% / 90%).

| # | Check | Result | Detail |
|---|---|---|---|
| 1 | Login and subscription access | PASS | <subscription-name>, Enabled, <alert-email> |
| 2 | Owner or Contributor at subscription scope | PASS | Contributor at `/subscriptions/<subscription-id>` (checked by object id; Graph lookup is not permitted for this account) |
| 3 | Resource providers | PASS | Microsoft.App registered by the user on 2026-10-29 (was NotRegistered); Microsoft.OperationalInsights, Microsoft.ContainerRegistry, Microsoft.Consumption Registered |
| 4 | Resource group name free | PASS | `az group exists` = false |
| 5 | Template validate + what-if | PASS | `az deployment sub validate`: Succeeded. `what-if`: 6 creates, 0 modify, 0 delete (below). Secure parameters passed from a temporary file, deleted after use; not present in output. |
| 6 | Docker image built and tested locally | PASS | `<app-name>:local`, STOP POINT 2 tests (samples from cache with 0 Azure calls; live TOR A run) |


## What-if: resources to be created

| Change | Type | Name | Location |
|---|---|---|---|
| Create | Microsoft.Resources/resourceGroups | <resource-group> | southeastasia |
| Create | Microsoft.OperationalInsights/workspaces | <log-workspace> (PerGB2018, 30-day retention) | southeastasia |
| Create | Microsoft.ContainerRegistry/registries | <registry-name> (Basic, admin user) | southeastasia |
| Create | Microsoft.App/managedEnvironments | <environment> (Consumption) | southeastasia |
| Create | Microsoft.App/containerApps | <app-name> (placeholder image, port 8501, 0.5 vCPU / 1 Gi, 0-1 replicas) | southeastasia |
| Create | Microsoft.Consumption/budgets | <budget-name> (USD 5/month, alerts 50% / 90% to <alert-email>) | resource group scope |

No existing resource is modified or deleted. The Azure OpenAI resource (`<openai-resource>`, `<openai-resource-group>`) is not referenced by the template except as an endpoint URL value.

## Deployment (2026-10-29)

- `az deployment sub create` (single step): **Succeeded** in about 3 minutes (10:20 to 10:23 UTC). Outputs: app FQDN
  `<app-fqdn>`, ACR `<registry-name>.azurecr.io`.
- Image rebuilt for `linux/amd64` (the local image was arm64, Apple Silicon), pushed as `<app-name>:v1`; Container App
  updated to revision `<app-name>--0000001`, state Succeeded.

## Smoke test on the public URL

Driven over Streamlit's websocket protocol (`scripts/remote_smoke.py`), i.e. the same messages a browser sends.

| Check | Result |
|---|---|
| Health endpoint after cold start (min replicas 0) | PASS, HTTP 200 on the 2nd try; plain HTTP redirects to HTTPS (301) |
| Nothing shown before login | PASS |
| Wrong password blocked | PASS ("Wrong password.", no content) |
| Login works | PASS (banner shown) |
| Sample TOR A | PASS, served from the shipped result cache (0 Azure calls): 16 features, bottleneck Dev, 2 export buttons |
| Sample TOR B | PASS, from cache: 20 features, 2 guard findings, 1 injection line removed, 3 PII items masked, bottleneck QA |
| No project pricing in the UI | PASS (TOR-quoted business-rule amounts such as the PHP 50,000 approval threshold are expected) |
| Live upload (TOR A as .md, not cached) | PASS: upload HTTP 204, full run in Azure in 72 s, 18 features, est. LLM cost $0.049 |
| Logs (`az containerapp logs show --tail 100`) | PASS: structured JSON only; no TOR text, PII, API key or app password |


## POC v3 deployment (2026-11-01)

Image `<app-name>:v3-1`, revision `<app-name>--0000015`, from branch `v3-workflow`. Added Container App secrets `database-url` (app role only), `gdrive-refresh-token`, `gdrive-client-id` and `gdrive-client-secret`; the owner role is never in the container. Migrations run locally with the owner role: `alembic upgrade head`. Scale unchanged (0.5 vCPU, 1 Gi, 0-1 replicas). The MSA template is read at runtime from the private `templates/` folder in Drive. Smoke test: `scripts/smoke_v3.py`.
