# Changelog

All notable changes are documented here.

## 0.2.0 - 2026-07-14

- Replaced source-patching experiments with five explicit, strict, default-off Educational Lab
  controls for the honesty contract, proactive gates, cron tool isolation, cycle limits, and
  committed-output enforcement.
- Made the installed Hermes cron prompt derive from current configuration so `install-cron`
  reliably refreshes the effective policy instead of retaining stale prompt text.
- Kept operator pause and the plugin master switch authoritative in every research mode.
- Added dynamic cron-context rendering and a full safe/unrestricted test matrix.

## 0.1.0 - 2026-07-14

- Added persistent self-model, workspace, intentions, questions, reflections, and decision ledger.
- Added compact `pre_llm_call` context injection without transcript storage by default.
- Added bounded `conscious_agency` model tool and explicit operator CLI/slash commands.
- Added independent scheduled-reflection and proactive-speech gates.
- Added quiet hours, cooldown, daily budget, recent-user protection, and message-length enforcement.
- Added task-scoped proactive-cycle tool isolation and per-tick mutation limits.
- Added fail-closed cron pre-gate and Hermes cron installer.
- Added optional SQLCipher database encryption.
- Added bounded event retention, audit records, tests, and full documentation.
- Filtered scheduled episodic context so operational cron/session/tool telemetry cannot dominate
  reflection, while keeping the complete ledger available through `events` diagnostics.
- Added recent decision context and explicit action-specific model-tool requirements.
- Added authoritative final-output enforcement through Hermes' `transform_llm_output` hook; missing
  cycle or decision commits now fail closed to `[SILENT]`.
- Changed idempotent cron installation to refresh the stored prompt, gate script, schedule, and
  delivery configuration instead of leaving stale job definitions in place.
- Added strict fail-closed configuration typing and a default prior-user-interaction gate for
  proactive speech.
- Hardened ledger invariants, schema downgrade detection, sidecar permissions, internal-turn
  filtering, single-decision cycles, and soft-failure handling for Hermes cron commands.
- Added a coordinated set of 3D architecture, agency-core, and proactive-safety visuals plus a
  complete opt-in procedure for real Hermes home-channel delivery.
