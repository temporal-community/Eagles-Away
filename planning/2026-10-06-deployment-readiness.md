# Eagles Away deployment readiness — 6 October 2026

This is a dated discovery record for the first cloud deployment. It supplements the [approved MVP architecture](mvp-plan.md#infrastructure-and-deployment) and the [engineering handoff](2026-09-30-engineering-handoff.md). No production resources or DNS records were changed during the initial review; later updates are recorded below.

## V5 cutover (not yet deployed)

The live App Platform spec uses `APP_ENV=preview`. `planner-main` has a build ID; `web` does
not. Do not rely on those V4 starts being explicitly pinned. V5 workers do not register V4.

1. Gate new `POST /api/sessions` requests before changing the spec. Leave snapshot reads
   available. Use the hosted Temporal namespace to verify zero running V4 or older planning
   workflows. Keep the old worker build online until those runs drain.
2. Apply migration `20261118_0006` for the cache timestamp and `20261118_0007` for rate-limit
   counters and cache outcome types. The migration job must succeed before the new `web`
   serves session requests. A local database that ran a pre-integration rate-limit migration
   under the old `0006` ID needs a fresh database or separate reconciliation before upgrade.
3. Check the connection limit and account for simultaneous old and new replicas and jobs.
   With two `web` processes and four workers during overlap, a cap of three connections each
   (`DATABASE_POOL_SIZE=2`, `DATABASE_MAX_OVERFLOW=1`) plus one connection each for `migrate`
   and `retention` totals 20 of 22. Give jobs a pool of one and zero overflow. This budget
   assumes every counted process honors its cap. If the old build cannot honor it, after it
   drains stop the old replicas before the new fleet starts, or raise the database limit first.
4. While submissions are gated, deploy the V5 worker from its frozen release branch. Set the
   same full commit SHA as `WORKER_BUILD_ID` on that worker and on the new `web`. Set
   `APP_ENV=production` on `web` and verify live planning and Temporal TLS. Scope the override
   to `web` so migration and retention jobs do not inherit its build requirements.
5. Verify both Workflow and Activity pollers for the new build on the hosted queue, then
   promote the build. Check `/readyz`, allow only the operator through the submission gate
   for one approved smoke plan, and confirm the hourly retention job is enabled before
   reopening submissions.
6. If the cutover fails, keep submissions gated. Do not point V5 `web` at V4 workers. Restore
   the previous `web` and worker together only after any V5 smoke run has finished. Migration
   0007 adds a table and cache column; leave them in place on application rollback to PR #4.

## 17 November 2026 — single-run release without Gemini

Commit `b83910f12de3871779905cf341a4cdcdd12ec33d` removes the review step, follow-up conversation,
Gemini/Pydantic AI and Google authentication. Temporal clients and workers now use
`temporalio.contrib.pydantic.pydantic_data_converter`. The worker reads immutable branch
`codex/worker-release-b83910f`; web and jobs read `main`. The app spec no longer has
`GOOGLE_AUTH_MODE` or `GEMINI_API_KEY`; runtime secrets are SearchApi and Temporal Cloud only.
Migration `20261117_0005` drops the unused `saved_itinerary` and `deleted_at` session columns.

The hosted namespace is `gcp-shy-projects.a2dd6` (API-key auth, regional endpoint
`us-central1.gcp.api.temporal.io:7233`). The local `cloud` CLI profile targets a different namespace;
the release used a temporary user API key created with `tcld` (24-hour minimum lifetime, about a
minute to activate) and deleted after promotion.

No trip workflows were running before the direct cutover. Deployment
`d654a8d3-bdd8-4d48-b7f5-3027077cee0c` reached `ACTIVE` (16/16), including the migration. Both new
worker replicas polled the workflow and activity queues before build `b83910f` was set Current;
`6ad51d8` moved to draining. Hosted `/`, `/privacy/`, `/healthz` and `/readyz` returned 200. A live
London–Białystok trip through the two-step form completed on the new build with a full itinerary.

## 17 November 2026 — browser-only release

The owner removed email collection and delivery and reported completing the PostgreSQL patch
upgrade. The app already uses SQLAlchemy `pool_pre_ping=True` to replace dead idle connections;
this does not replay an interrupted transaction. A local PostgreSQL test terminates a pooled
backend and verifies the next query reconnects successfully.

This release started `TravelPlanningSessionWorkflowV4`. Completed V3 snapshots remain readable:
the API falls back to the stored Temporal result if the old pinned worker is unavailable,
discards retired delivery metadata, and maps old delivery terminal phases to `closed`.
Keep the hosted queue and Worker Deployment name below. Use a new immutable source SHA/build ID
and promote it after its Workflow and Activity pollers register.

Migration `20261117_0004` removes the delivery/webhook tables and recipient columns, preserving
session IDs, access hashes and saved itinerary data. Removed recipient data cannot be recovered
by downgrading. Old application binaries are incompatible with this schema; rollback requires
a pre-migration backup or a compatible application fix. The owner explicitly approved a direct
cutover because nobody is using the site; verify no running trip workflows before replacement.
The `cpfc-db upgrade` predeploy job applies the migration. Remove the retired `EMAIL_MODE`,
`RESEND_*`, `CONTACT_ENCRYPTION_KEY` and `SESSION_SECRET` environment entries from the app spec.
No email provider or recipient-encryption configuration is required by the new API or worker.

## 15 October 2026 — historical operator notes

These notes supersede the private-preview and GCP deployment steps below. The public site is
`https://eaglesaway.com/`. DigitalOcean App Platform app
`373808be-b5ec-4d20-b7fc-d8dbe906aff9` runs the web service, migration and retention jobs,
and two Temporal worker replicas, backed by PostgreSQL. Source is
[`temporal-community/Eagles-Away`](https://github.com/temporal-community/Eagles-Away) on `main`.
DigitalOcean uses a Git clone source: pushing a commit does **not** start a deployment. As of
16 October, the web and jobs read `main`, while the unchanged worker build
`47c3595d429ff54934441f6e5a440a5c07cd67dc` reads immutable branch
`codex/worker-release-47c3595` so frontend-only source updates cannot rebuild the worker from
newer code under its old build ID.

The hosted Temporal queue is `eagles-away-do-preview-v1` (the fresh local checkout defaults to
`cpfc-trip-v3`). The Worker Deployment is `cpfc-trip`; pinned workers use the full source commit
SHA as an immutable build ID. For a code release, build a new worker version from the exact
commit, start it beside the old version, confirm Workflow and Activity pollers on the hosted
queue, and promote the new version to Current. Keep an old-build replica until Temporal reports
that version drained. Never rebuild later source under an existing build ID. After release,
check both queue types for live pollers and backlog age, DigitalOcean worker health, and the
public `/healthz` endpoint. See the 14 October [worker rollout record](../blog.md#14-october-2026--worker-resilience-and-manual-email)
for the tested scale-out/scale-in behavior and remaining limits.

With a Temporal CLI profile for the hosted namespace, use the **hosted** queue for release checks:

```bash
temporal worker deployment describe --name cpfc-trip
temporal task-queue describe --task-queue eagles-away-do-preview-v1 --task-queue-type activity --select-build-id <new-build-id>
temporal task-queue describe --task-queue eagles-away-do-preview-v1 --task-queue-type workflow --select-build-id <new-build-id>
temporal worker deployment set-current-version --deployment-name cpfc-trip --build-id <new-build-id>
temporal worker deployment describe-version --deployment-name cpfc-trip --build-id <old-build-id>
```

Workers run `WORKER_MAX_ACTIVITIES` (default 64) Search Activities each; a session runs about
four at once. Eight slots per worker held 18 simultaneous simulated sessions at 27–38 s to a
decision against 4–8 s with 64 (18 November 2026). Identical searches running together on a
worker share one provider search. Investigate if the oldest task waits over five seconds or p95
Schedule-to-Start exceeds two seconds for ten minutes; check provider 429s before adding
capacity. The worker's 150-second SDK drain needs a 180-second DigitalOcean termination grace.
SearchApi's hourly limit is 20% of the monthly plan (2,000/h on the 10,000 plan). Size the plan
to the expected peak hour, not the month.

The live app spec and `.env` are ignored because they contain secrets. Keep SearchApi and
Temporal Cloud credentials in DigitalOcean runtime secrets. See the [README](../README.md)
for local setup; `.env.example` selects recorded results.

## 8 October 2026 — preview origin correction

Making `eaglesaway.com` the DigitalOcean primary domain changed `${APP_URL}`, so POST requests from the generated `ondigitalocean.app` preview URL returned 403. The API now accepts an explicit, comma-separated `ADDITIONAL_FRONTEND_ORIGINS` allowlist alongside `FRONTEND_ORIGIN`. The hosted spec adds only `https://eagles-away-preview-ifemt.ondigitalocean.app` as an additional origin; unrelated origins still return 403. A regression test covers both trusted origins and rejection of an unrelated origin. The full check passed with 91 Python tests, two PostgreSQL tests skipped by their fixture, and 19 frontend tests. A pre-existing frontend test cleanup issue surfaced in the first check and was corrected before the passing run. A configuration-only DigitalOcean update reused old commit `a1dcaf8`; redeploying with `--update-sources` built commit `5c0c17a`. Deployment `36394aef-26bc-4f1d-b002-fc8e6004e2bc` reached `ACTIVE`. Hosted checks returned 200 for health and authenticated home, 401 for anonymous home, 422 for deliberately invalid POSTs from both trusted origins, and 403 for an unrelated origin. The 422 responses establish that the origin guard allowed those requests; valid trip creation was not retested. Public apex DNS still had no address at verification.

## 7 October 2026 — custom domain registration pending DNS

The owner requested the apex `eaglesaway.com` for the password-protected preview. DigitalOcean App Platform accepted it as the primary custom domain on app `373808be-b5ec-4d20-b7fc-d8dbe906aff9`, with `APP_ENV=preview` and HTTP Basic credentials retained. The sanitized spec passed account-backed validation. The domain was initially `CONFIGURING`; Cloudflare DNS had no apex record because dashboard sign-in was pending. The generated preview URL returned 200 for `/healthz` and 401 for anonymous `/` after registration. The next step is a Cloudflare DNS-only apex CNAME to `eagles-away-preview-ifemt.ondigitalocean.app`, followed by certificate, HTTPS, gate and origin checks. Preserve the existing mail records and Cloudflare nameservers.

## 7 October 2026 — DigitalOcean target replaces the GCP preview

The owner chose DigitalOcean App Platform after the GCP billing block. The `doctl` profile
initially returned HTTP 401; after reauthentication a dedicated project and Frankfurt
PostgreSQL 17 cluster were created. The preview image builds locally, and a container import verified the
static export and API package. `scripts/check.sh` passed after adding a required HTTP Basic
access gate for `APP_ENV=preview` and DigitalOcean PostgreSQL URL/TLS handling. The initial
DB password appeared in CLI creation output and was rotated before deployment. The worker
supports `GOOGLE_AUTH_MODE=api_key`; the provided Gemini key was verified. Keep `EMAIL_MODE=preview` and
`eaglesaway.com` DNS unchanged for this stage. The GCP sequence below records the previous
target and is superseded for the private preview.

## 7 October 2026 — DigitalOcean private preview deployed

The Frankfurt PostgreSQL 17 cluster `eagles-away-preview-pg` is online in the dedicated
`Eagles Away Preview` project. An App Platform deployment is active at
<https://eagles-away-preview-ifemt.ondigitalocean.app/> with a web service, Temporal worker,
and successful predeploy migration job. The app uses a separate preview task queue,
runtime-only encrypted secrets, `APP_ENV=preview`, and `EMAIL_MODE=preview`. No DNS record
for `eaglesaway.com` was changed.

The deployed `/healthz` returned 200, `/` returned 401 without preview credentials, and
authenticated `/`, `/plan/`, and `/api/catalog` returned 200. A hosted London–Lyon live session
reached `draft_ready` with a journey, stay, partial priced total, and explicit outstanding
checks. This is one functional smoke test, not a full route or browser acceptance audit.

A sanitized app spec passed DigitalOcean's account-backed validation. Automatic approval
review rejected an additional validation of the secret-bearing spec because it would have
uploaded API keys unnecessarily. The actual deployment submitted runtime secrets to
DigitalOcean as required. The ignored local spec and preview password are restricted to the
owner's workspace and are not committed.

## 7 October 2026 — live email candidate awaiting review

The existing Resend delivery Activity accepted a local live email using a frozen London–Lyon
plan, and the owner confirmed receipt. A redesigned email matching the website's plan-card
hierarchy was accepted as a separate idempotent test; the owner has not yet confirmed the
revised appearance. A sanitized App Platform candidate with `EMAIL_MODE=resend` and a
runtime-only Resend key and sender passed account-backed validation. The deployed app remains
on `EMAIL_MODE=preview` until the owner approves the email change. Signed delivery-status
webhooks remain unconfigured, so provider acceptance is not a delivery confirmation.

## 7 October 2026 — live email enabled on the private preview

The owner approved the revised email format and live sending for the protected preview.
DigitalOcean deployment `91a13e43-ef9a-44f1-b5e3-84361fc687b8` built commit `a1dcaf8`
and became active with successful build, migration and deploy steps. `EMAIL_MODE=resend` is
active; the Resend key is a runtime-only secret. `/healthz` returned 200, anonymous `/`
returned 401, and the authenticated page and catalog returned 200.

Hosted London–Lyon session `7b5f5233-ee67-425c-9b75-83a15b45c1b0` reached `draft_ready`.
Its finalize request was accepted, and the workflow reached `emailed` with Resend receipt
`01a117ba-9fbd-79d0-a9d7-9224a2ea2488`. The sending-only key cannot read delivery status,
and no webhook signing secret is configured; the owner must check the inbox to confirm arrival.
The preview remains password-protected and `eaglesaway.com` DNS is unchanged.

## Verified state

| Area | Evidence on 6 October | Consequence |
| --- | --- | --- |
| Domain | `eaglesaway.com` uses Cloudflare nameservers (`david` and `gina`). The apex A lookup returned `NOERROR` with no answer; `www.eaglesaway.com` returned `NXDOMAIN`. | The public site does not currently resolve to a GCP load balancer. Cloudflare DNS access is needed for cutover. |
| GCP access | The owner refreshed the `eagles-away-work` login and requested Opal access. This account now has `roles/owner` on `cpfc-europa-trip-prod`; billing is still disabled and the Cloud SQL Admin API is disabled. Linking either the initially approved `Billing Account for temporal.io` or the subsequently requested `Navan GCP Card` failed because this account lacks `billing.resourceAssociations.create` on both billing accounts. `andrew-baker-sandbox` has billing enabled but was explicitly declined as a hosting target. | The chosen dedicated project cannot host the preview until an authorized billing administrator links an approved account or grants the required billing-account permission. |
| Infrastructure | This repository has no Terraform roots or CI deployment workflow. A preview image definition builds the static export and API together; `terraform` is not installed locally. An attempt to enable preview APIs failed with `FAILED_PRECONDITION` because billing is disabled. | The approved public production topology has not been provisioned. The preview uses a single protected Cloud Run origin to test the app without a load balancer or DNS cutover. |
| Temporal | The owner confirmed namespace `gcp-shy-projects.a2dd6` at `gcp-shy-projects.a2dd6.tmprl.cloud:7233`. The locally configured API key successfully completed a Cloud health check on 6 October without disclosing the key. The installed CLI has no `temporal cloud` extension. | The preview needs that API key stored in Secret Manager and a continuously running worker pool. |
| Database | Application runtime currently creates an ordinary SQLAlchemy engine from `DATABASE_URL`; Cloud SQL fields in `Settings` are unused. | The planned private-IP, automatic-IAM Cloud SQL connection and migration job are not implemented. |
| Email and privacy | Local mode is `EMAIL_MODE=preview`; a Resend API key is present but webhook secret is absent. `/privacy/` explicitly says the build is not open for public submissions and lacks retention, operator and deletion details. | Public submissions and live email require owner decisions, signed webhook setup and final privacy copy. |
| Traffic controls | Session creation/polling uses process-local limits. The handoff notes missing shared admission and cost controls. | Scaling the API without shared limits could multiply paid model/provider calls. |
| Local gate | After the preview static mount and secure-cookie change, `scripts/check.sh` passed: 78 Python tests, 18 frontend tests, Ruff, mypy, API schema, TypeScript, ESLint and static export. Two PostgreSQL tests skipped because Docker Desktop was stopped at that time. The image subsequently built, and `/`, `/plan/`, `/privacy/`, `/api/catalog` and `/healthz` each returned 200 in a container smoke test. | The container runs its static and unauthenticated routes, but database, Temporal and paid provider operations are untested in the image. |

## Deployment sequence

1. Link the owner's approved billing account to `cpfc-europa-trip-prod`, then enable Cloud Run, Cloud Build, Artifact Registry, Cloud SQL and Secret Manager APIs. Inspect existing resources before creation.
2. For the chosen private preview, publish the combined static export/API image and run it on one Cloud Run origin behind direct IAP. Provision Cloud SQL, migrate it, store secrets, and run the Temporal worker in a worker pool. Keep email in preview mode and do not change `eaglesaway.com` DNS.
3. For the later public topology: static Next export in Cloud Storage/CDN, same-origin FastAPI Cloud Run, Cloud SQL, Temporal Cloud plus versioned workers, Secret Manager, and HTTPS load balancer. Keep `eaglesaway.com` DNS unchanged until health, direct `/plan/` routing, credentials and rollback are verified on a test hostname or load-balancer IP.
4. Exercise a deployed trip through API, Temporal worker, database, provider search, browser reload and email mode appropriate to the release. Test worker replacement, a failed provider request, and a rollback before DNS cutover.
5. Update Cloudflare DNS only after the release is healthy and the domain owner approves the exact record change; verify certificate issuance and both apex and `www` behavior.

## Decisions pending at the initial GCP review

- Billing-account permission or administrator action to link an owner-approved account to `cpfc-europa-trip-prod`; its billing is currently disabled. Attempts with both `Billing Account for temporal.io` and `Navan GCP Card` were denied. The owner chose this project for a private preview and declined the sandbox.
- Import the locally configured, health-checked Temporal Cloud API key directly into Secret Manager; the namespace and endpoint are confirmed.
- Cloudflare DNS access and intended `www` behavior.
- For public use: operator identity, retention/deletion contact, Resend webhook secret, commercial permission for search-result display and 24-hour cache, and shared admission/cost limits.

## 9 October 2026 — public beta on DigitalOcean

The owner confirmed `shy.ruparel@temporal.io` for the public privacy contact. DigitalOcean app
`373808be-b5ec-4d20-b7fc-d8dbe906aff9` deployed commit `905ff2b` as deployment
`17cf6e61-831f-4454-8b7e-4408de671d76` and reached `ACTIVE`. The applied spec sets
`PREVIEW_ACCESS_REQUIRED=false`, removes the preview credentials, and schedules `cpfc-prune` hourly
at minute zero UTC. The retention job deletes application database sessions, related email
deliveries, and webhook records older than 30 days; its first hosted invocation is not yet verified.

Anonymous `https://eaglesaway.com/`, `/privacy/`, `/api/catalog`, and `/healthz` returned 200.
A cross-origin session POST returned 403; an invalid same-origin POST returned 422. The live root
response has no `Permissions-Policy` header, and the in-app browser console recorded no warnings
before or after analytics consent. The reported Firefox warnings remain unlocalized to a specific
response or extension. Hosted Lighthouse measured mobile scores 97/100/100/100 and desktop
100/100/100/100 (performance/accessibility/best practices/SEO). Its only sizeable asset finding
was the 79 KB Temporal lockup. A 3.8 KB lossless WebP replacement deployed in
`7bfa34c2-5856-4e1e-b3f6-f560958be9bb` and reached `ACTIVE`; the live page references it,
and its 200 response transfers 3,848 bytes. The final hosted Lighthouse scores were mobile
99/100/100/100 and desktop 100/100/100/100, with no image-delivery finding and mobile LCP 2.0 s.
This beta still needs provider acceptance monitoring, privacy/vendor retention review, and traffic
controls beyond the current per-process session limit.

## 14 October 2026 — no-result email suppression and Resend key update

The no-result workflow now ends with an on-page explanation and no email Activity when no
itinerary was committed. A Temporal patch preserves replay of old failure-notice histories.
Deployment `e92a6eb1-0555-49f1-b62f-8b15125e43d2` built commit `83118ae` and reached
`ACTIVE`. The owner replaced `RESEND_API_KEY` in the local ignored `.env` and asked to apply it
to DigitalOcean. The new value was copied into the ignored app spec without displaying it;
schema validation passed. Configuration deployment `9543f88e-807c-40da-893f-c1074f882cb1`
reached `ACTIVE`, with the Resend secret slot present. Hosted `/` and `/healthz` returned 200
and `/privacy/` displayed the new policy. No actual email was sent to verify the key's
delivery permission; no paid live route search was run.

## 17 November 2026 — browser-only release verified

Deployment `f8898c62-dfe5-42b8-8a1e-d6f7e7077a11` reached `ACTIVE` (16/16 steps).
The predeploy job applied the removal migration. The immutable worker branch is
`codex/worker-release-6ad51d8`, build ID `6ad51d890d087fadb11032c101e722b377ccc5fe`,
and Temporal reports it as Current. There are two `planner-main` instances. Retired email
settings are absent from the app spec. The owner's direct-cutover authorization superseded
the initial proposal to stage this schema change.

The current CLI's enhanced queue filter did not return the Worker Deployment's pollers;
`temporal task-queue describe --legacy-mode` showed both instance identities on both queue
types. Check those identities against DigitalOcean rather than inferring worker loss solely
from an empty enhanced-filter result. A recorded-data V4 smoke workflow completed on the
hosted queue without provider Activities. Public health/readiness, schema and desktop/mobile
review/privacy checks passed; no paid-provider end-to-end search was run.
