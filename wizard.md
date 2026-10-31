---
wizard: 0.2
id: wizard-coros
name: Coros
type: external
category: system
version: 0.1.0
summary: Connect your COROS. Health and your other apps get read-only sleep, HRV, resting heart rate and training load.
tags: [data]
license: GPL-3.0-only
icon: icon.png
source: https://github.com/oscarhenrycollins/wizard-coros
runtime:
  kind: runtipi
  config: runtipi/config.json
  compose: runtipi/docker-compose.yml
api:
  port: 8842
  path: /api
  spec: openapi.json
mcp:
  path: /mcp
  port: 8842
  transport: streamable-http
skill: skill/SKILL.md
view:
  path: /
  port: 8842
  source:
    path: web
    build: python3 app/build_view.py
    dist: web
provides: [health-data, coros-health-data]
system: true
uses: []
needs: []
health: /healthz
---

# Coros

Connects one COROS account to your Wizard. All you need is the free COROS account your watch syncs to: tap Connect with COROS on the app's own screen, sign in on COROS's own page in a new tab and allow access. Nothing to copy or paste, no developer account, no approval and no fee: the app uses COROS's self-service MCP connection for apps (OAuth 2.0 against COROS's official MCP server), the same one COROS offers to AI assistants.

After sign-in the app downloads about the last 90 days of sleep (score and stages), overnight HRV, resting heart rate, daily steps and calories, training load and your workouts, then checks for new data every 20 minutes. It keeps the copy here and offers it, read-only, to other apps on this box through the `health-data` spot (contract `wizard.health-data/1`, see `docs/health-data-contract.md`).

COROS has no daily recovery score to share (only a current recovery percentage without history), so this source sends none: Health shows recovery from another source, or says COROS has no score. Disconnect ends the sign-in and deletes the local copy.
