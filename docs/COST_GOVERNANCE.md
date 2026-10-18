# Cost governance: idle-runtime clearance and email alerts

FinOps Dashboard can warn the owner of an idle runtime, give them a grace period to keep it, and then clear it. It can also email a FinOps digest to executives. Three Cloud Scheduler jobs drive this. The jobs only trigger the service: the service decides what to do and sends the email itself.

## 1. What runs on a schedule

| Cloud Scheduler job | Schedule (default `America/Los_Angeles`) | Calls | What it does |
| :--- | :--- | :--- | :--- |
| `finops-warn-idle-runtimes` | Daily 08:00 | `POST /api/cron/warn_idle_runtimes` | Flags idle Agent Engine (Reasoning Engine) and Cloud Run runtimes and over-budget workloads, finds each creator in the Cloud Audit Logs, and emails them a warning with a Keep-Alive link. Clearance is scheduled 48 hours later (`grace_period_hours`). Each runtime is warned once per idle period. |
| `finops-execute-scheduled-clearance` | Every 6 hours | `POST /api/cron/execute_scheduled_clearance` | Clears runtimes whose grace period ended without a snooze. `dry_run` by default (see section 5). |
| `finops-send-executive-digest` | Daily 08:30, or Mondays with `DIGEST_CADENCE=weekly` | `POST /api/cron/send_executive_digest` | Emails the digest: top agents and users by estimated token spend, week-over-week change, prompt cache-hit alerts, runtimes pending clearance. |

The queue is stored in BigQuery (`finops_analytics.zombie_clearance_queue`), which every Cloud Run instance shares. Each scheduled run, sent digest and snooze first re-reads that table, so a snooze recorded on one instance is honored by the instance that runs the clearance job.

## 2. Create the jobs

```bash
PROJECT_ID=my-project REGION=us-central1 \
DIGEST_RECIPIENTS="cfo@example.com,finops@example.com" \
./deploy/setup_cloud_scheduler.sh
```

The script is idempotent: run it again to change settings. It grants the job identity (`SCHEDULER_SA_EMAIL`, default the runtime service account) `roles/run.invoker` on the private service and attaches an OIDC token to every call. Options: `CLEARANCE_MODE` (`dry_run` or `enforced`), `DIGEST_CADENCE` (`daily` or `weekly`), `DIGEST_RECIPIENTS`, `TIME_ZONE`, `SERVICE_URL`. Whoever runs it needs `roles/iam.serviceAccountUser` on the job identity.

Check it end to end without waiting for the schedule:

```bash
gcloud scheduler jobs run finops-send-executive-digest --project=my-project --location=us-central1
```

Then look at `recent_warnings` in `GET /api/governance/clearance_queue` (or the Cost Governance panel). Every email attempt is recorded there with its `delivery_channel`, `delivered` flag and `error`.

## 3. Connect email delivery

The service uses the first channel that is configured:

1. **Gmail API**: any of `FINOPS_GMAIL_REFRESH_TOKEN`, `FINOPS_GMAIL_DWD_SERVICE_ACCOUNT` or `FINOPS_GMAIL_ACCESS_TOKEN` is set.
2. **SMTP**: `FINOPS_SMTP_HOST` (plus `FINOPS_SMTP_PORT`, `FINOPS_SMTP_USER`, `FINOPS_SMTP_PASSWORD`, `FINOPS_SMTP_STARTTLS`).
3. **Webhook**: `FINOPS_ALERT_WEBHOOK_URL` (`https://` only) receives the message as JSON, for a chat or ticketing integration.
4. **Nothing configured**: the run records a `DRY_RUN_PREVIEW` with the full message and sends nothing.

The service's own credentials cannot send Gmail. Its token carries only the `cloud-platform` scope, and a service account has no mailbox. Pick one of the two Gmail options below.

### Option A: OAuth refresh token for a sending mailbox

Works with a Gmail or Google Workspace mailbox, such as a dedicated `finops-alerts@` account.

1. Enable the API: `gcloud services enable gmail.googleapis.com --project=my-project`.
2. Configure the OAuth consent screen. With user type **External** and publishing status **Testing**, Google expires refresh tokens after 7 days. Use an **Internal** app (Workspace) or publish the app.
3. Create an OAuth client of type *Web application* with the authorized redirect URI `https://developers.google.com/oauthplayground`.
4. In the [OAuth 2.0 Playground](https://developers.google.com/oauthplayground), open the settings, tick *Use your own OAuth credentials* and enter the client ID and secret. Authorize the scope `https://www.googleapis.com/auth/gmail.send` while signed in as the sending mailbox, then exchange the code and copy the refresh token.
5. Store the secrets in Secret Manager and point the service at them:

```bash
printf '%s' "$REFRESH_TOKEN" | gcloud secrets create finops-gmail-refresh-token --data-file=- --project=my-project
printf '%s' "$CLIENT_SECRET" | gcloud secrets create finops-gmail-client-secret --data-file=- --project=my-project
for s in finops-gmail-refresh-token finops-gmail-client-secret; do
  gcloud secrets add-iam-policy-binding "$s" --project=my-project \
    --member=serviceAccount:finops-dashboard-runtime-sa@my-project.iam.gserviceaccount.com \
    --role=roles/secretmanager.secretAccessor
done
gcloud run services update finops-dashboard-mcp-app --region=us-central1 --project=my-project \
  --update-secrets=FINOPS_GMAIL_REFRESH_TOKEN=finops-gmail-refresh-token:latest,FINOPS_GMAIL_CLIENT_SECRET=finops-gmail-client-secret:latest \
  --update-env-vars=FINOPS_GMAIL_CLIENT_ID=CLIENT_ID.apps.googleusercontent.com,FINOPS_GMAIL_SENDER=finops-alerts@example.com
```

### Option B: keyless domain-wide delegation (Google Workspace)

No secret is stored. The service signs a JWT as a service account through the IAM Credentials API and exchanges it for a `gmail.send` token for one Workspace mailbox.

1. Enable the APIs: `gcloud services enable gmail.googleapis.com iamcredentials.googleapis.com --project=my-project`.
2. Choose the delegating service account (the runtime service account works) and let the runtime service account sign as it, even when they are the same account:

```bash
gcloud iam service-accounts add-iam-policy-binding finops-dashboard-runtime-sa@my-project.iam.gserviceaccount.com \
  --member=serviceAccount:finops-dashboard-runtime-sa@my-project.iam.gserviceaccount.com \
  --role=roles/iam.serviceAccountTokenCreator --project=my-project
gcloud iam service-accounts describe finops-dashboard-runtime-sa@my-project.iam.gserviceaccount.com \
  --format='value(oauth2ClientId)'   # the client ID for the next step
```

3. A Workspace super admin opens *Admin console > Security > Access and data control > API controls > Manage Domain Wide Delegation*, adds that client ID, and allows only the scope `https://www.googleapis.com/auth/gmail.send`. Delegation lets the account send as any user in the domain, so keep the scope to `gmail.send` and limit who can act as the account.
4. Set `FINOPS_GMAIL_DWD_SERVICE_ACCOUNT=finops-dashboard-runtime-sa@my-project.iam.gserviceaccount.com` and `FINOPS_GMAIL_SENDER=finops-alerts@example.com` (a real mailbox in that domain) on the service.

Tokens are cached until shortly before they expire, so a run that sends many warnings mints one token.

## 4. Keep-Alive links

Every warning email carries a signed 7-day snooze link. Two settings make it work:

- **A URL the owner's browser can open.** `finops-dashboard-mcp-app` is private (`--no-allow-unauthenticated`), so a click from an email gets HTTP 403. Deploy the IAP-protected dashboard (`FINOPS_SPLIT_EDGE=1` in `deploy_cloud_run.sh`) and set `FINOPS_SNOOZE_BASE_URL` to its URL (for example `https://finops-tokenomics-dashboard-PROJECT_NUMBER.REGION.run.app`) on the service that sends email. Owners need `roles/iap.httpsResourceAccessor` on that service.
- **One signing secret everywhere.** Links are signed with `FINOPS_GOVERNANCE_SECRET`. Without it each instance makes up its own random secret, and a link fails with "Invalid or expired snooze token" on any other instance or after a restart. Store it in Secret Manager and set the same value on both services:

```bash
openssl rand -hex 32 | tr -d '\n' | gcloud secrets create finops-governance-secret --data-file=- --project=my-project
gcloud run services update SERVICE --region=us-central1 --project=my-project \
  --update-secrets=FINOPS_GOVERNANCE_SECRET=finops-governance-secret:latest
```

## 5. Clearance modes & 5-pillar governance score

- **`dry_run`** (default): records what would be cleared (`DRY_RUN`) and changes nothing in Google Cloud, leaving the runtime pending in `zombie_clearance_queue` so subsequent evaluations or an enforced run can still act on it.
- **`enforced`**: requires `CLEARANCE_MODE=enforced` on the job **and** `FINOPS_ALLOW_AUTOMATED_CLEARANCE=1` on the service (plus the custom IAM role `finopsIdleRuntimeCleaner` granted by `deploy/deploy_cloud_run.sh` when `FINOPS_ALLOW_AUTOMATED_CLEARANCE=1`). `finops_dashboard/server.py` wires `cost_governance.google_api_caller` into `CostGovernanceService`, executing live Cloud Run `minScale=0` (`PATCH ...?updateMask=scaling.minInstanceCount`) and Vertex AI Reasoning Engine deletion (`DELETE ...?force=true`). Protected runtimes (`finops-dashboard-mcp-app`, `finops-tokenomics-dashboard`, `K_SERVICE`, `FINOPS_CLEARANCE_EXCLUDE`) are never cleared. An enforced run automatically falls back to `dry_run` when it cannot re-read `zombie_clearance_queue` from BigQuery or when fleet telemetry is warming/incomplete, so a snooze or active runtime is never overridden by stale state.
- **5-Pillar Governance & Tokenomics Score (`compute_governance_score` in `finops_dashboard/cost_governance.py`)**: combines Pillar 1 (Prompt Cache Efficiency, 25 pts, `FINOPS_CACHE_HIT_TARGET_PCT`), Pillar 2 (Idle Runtime Hygiene, 25 pts), Pillar 3 (Trace & Observability Coverage, 20 pts), Pillar 4 (Cost Attribution & Budget Guardrails, 15 pts, `FINOPS_MONTHLY_BUDGET_USD`), and Pillar 5 (Seat & Quota Utilization, 15 pts, `FINOPS_SEAT_UTILIZATION_TARGET_PCT`).

## 6. Troubleshooting

| What you see | Cause | Fix |
| :--- | :--- | :--- |
| `delivery_channel: DRY_RUN_PREVIEW` | No delivery channel configured | Section 3 |
| `HTTP 403 from gmail.googleapis.com` ... insufficient authentication scopes | The token lacks `gmail.send` | Re-authorize with the `gmail.send` scope (option A) or fix the delegated scope (option B) |
| `HTTP 401 from oauth2.googleapis.com: ... unauthorized_client` | Domain-wide delegation not allowed for that client ID or scope | Section 3, option B step 3 |
| `HTTP 400 from oauth2.googleapis.com: ... invalid_grant` | Refresh token revoked or expired (7 days in *Testing* mode) | New refresh token; use an Internal or published app |
| `Permission 'iam.serviceAccounts.signJwt' denied` | Runtime account cannot sign as the delegating account | Grant `roles/iam.serviceAccountTokenCreator` (option B step 2) |
| Error names a missing `FINOPS_GMAIL_*` variable | Incomplete Gmail configuration | Set the variable it names |
| Scheduler job fails with HTTP 403 | Job identity lacks `roles/run.invoker` | Re-run `setup_cloud_scheduler.sh`; it warns when the grant fails |
| Snooze link gets HTTP 403 before reaching the app | Link points at the private MCP service | Section 4, `FINOPS_SNOOZE_BASE_URL` |
| Snooze link says "Invalid or expired snooze token" | `FINOPS_GOVERNANCE_SECRET` not set, or different on the two services | Section 4 |
| Digest call returns HTTP 400 `INVALID_REQUEST` | `recipient` is not a list of plain email addresses (at most 20) | Fix `DIGEST_RECIPIENTS` |
