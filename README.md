# Coros (Wizard connector)

A Wizard OS connector app for one COROS account. It holds the COROS sign-in, keeps a private synced copy of sleep, HRV, resting heart rate, daily activity, training load and workouts, and offers that copy read-only to other apps on the same box through the `health-data` spot, contract [`wizard.health-data/1`](docs/health-data-contract.md). The [Health](https://github.com/oscarhenrycollins/wizard-health) app is the screen for the data; this app is only the connection.

**What the owner needs:** only a free COROS account. The app uses COROS's self-service path for apps, [Build on COROS MCP](https://support.coros.com/hc/en-us/articles/53181619102996-Build-on-COROS-MCP) ("No application or approval required"): it is an MCP client of COROS's official server `https://mcp.coros.com/mcp`, following COROS's own [COROS-MCP repo](https://github.com/coroslab/COROS-MCP) (Tool Guide and login helper).

## Setup (all on the app's own screen)

1. A COROS account with a watch synced in the COROS app.
2. Connect with COROS: COROS's sign-in page opens in a new tab; the owner signs in and allows access. The app finishes by itself (it polls COROS's login session, then exchanges the code with PKCE). An optional region picker (Automatic, United States, Europe, Mainland China) follows COROS's setup guide. "Check COROS" calls COROS's real token endpoint with a made-up token to show the sign-in service is answering.

Behind the button: OpenID discovery at the gateway (it picks the regional server), dynamic client registration once per box (public client, scopes `openid offline_access mcp.tools`), `POST /api/v1/cli/login-sessions`, claim until `authorized`, the authorization request finished with the login ticket, code exchange. Access tokens renew with the refresh token; a refused refresh asks the owner to connect again.

## Data and sync

- Backfill of about 90 days after sign-in, then every 20 minutes with a 4-day overlap. COROS has no webhooks for this path, so the app polls.
- Tools: querySleepOverview (28-day windows), querySleepHrv (7-day windows), queryRestingHeartRate, queryDailyHealthData, queryTrainingLoadAssessment, querySportRecords + getActivityDetail (once per activity), queryRecoveryStatus.
- No readiness: COROS shares only the current recovery %, without history. Mappings are in `docs/health-data-contract.md` under "Provider mappings".
- The latest raw answer of each tool is kept in the local database so a later version can re-read it if COROS changes its text format.

## Verified vs not

Verified for real: discovery, client registration and the login session against COROS's live servers (session `pending` until a person signs in), and the token endpoint's `invalid_client` answer to a fake client. Tested against a fake COROS MCP server: the full sign-in, refresh, MCP JSON/SSE replies and sync. Not yet run with a real COROS account. COROS answers in formatted text; the Tool Guide shows examples only for the sleep overview, activity list and activity detail, so the readers for HRV, resting heart rate, daily data, training load and recovery are built tolerant of several plausible layouts and may need a fix after the first real sign-in.

## Interfaces

- Screen: `/wizard-coros/` (status, Sync now, Disconnect, guided setup).
- Contract (box-only): `GET /api/status`, `GET /api/days?from&to&limit&page&changed_since`.
- Setup API used by the screen: `GET /api/setup`, `POST /api/login/start`, `/api/login/cancel`, `/api/check`, `/api/sync`, `/api/disconnect`.
- MCP (`POST /mcp`, box-only): `health_data_status`, `health_data_sync`, `health_data_days`.
