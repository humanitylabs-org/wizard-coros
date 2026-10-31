---
name: wizard-coros
description: Use when the owner asks about his COROS connection, COROS setup or sync. Health questions go to Health.
version: 0.1.0
author: Humanity Labs
license: GPL-3.0-only
metadata:
  hermes:
    tags: [coros, health-data, connector]
    related_skills: [wizard-health]
---
# Coros (connector)

The box's COROS connection (`health-data` spot, contract `wizard.health-data/1`). It holds the COROS sign-in and a synced copy; the **Health** app (`/wizard-health/`) shows the data and answers health questions. Tools: `mcp__wizard__wizard_coros__<tool>`.

- Questions about sleep, HRV, resting heart rate, training load, workouts or trends: use the Health app's tools, not these.
- "Is my COROS connected / when did it last sync": `health_data_status`. `connected`, `syncing` or `rate_limited` = data is flowing; `not_configured` or `needs_login` = the owner must connect (again). `recovery_now` is COROS's current recovery %, a snapshot only.
- Setup or reconnecting: send the owner the app link `[Coros](/wizard-coros/)`; its screen has one Connect with COROS button and he signs in on COROS's own page. Never ask for his COROS password or codes in chat.
- COROS gives no daily recovery score; this source sends none and never computes one.
- `health_data_sync` asks for fresh data now (the app also syncs every 20 minutes). `health_data_days` is the raw contract read; load is COROS Training Load (sum of the day's activities), unchanged.
- Scripts on the box can read the same contract at `http://127.0.0.1:8842/api/status` and `/api/days`.
