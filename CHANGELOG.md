# Changelog

All notable changes are documented here.

## 0.5.0 - 2026-07-16

- Replaced the over-prescriptive Educational Lab frame with one compact, state-first research
  context that leaves response style, topic, and whether state changes to the configured model.
- Kept persistent focus, intentions, questions, self-observations, reflections, temporal
  orientation, and prior-user-contact age visible in cold and continuity conditions.
- Replaced synthetic drive-like control signals with factual state metrics: intention counts,
  completion ratio, open-question count, and hours since genuine user contact.
- Versioned new subjective samples as protocol 1.4 and limited continuity to a 600-character trace
  from an earlier same-model, same-source session. Conversation and cron chains no longer
  contaminate one another.
- Reduced the model-facing tool and scheduled-cycle instructions to material state changes and the
  minimum rules required by the configured policy; leaving state unchanged is explicitly valid.
- Made a direct user request to persist focus, intentions, questions, reflections, or
  self-observations an explicit model-tool trigger while keeping routine narration tool-free.
- Counted intention statuses directly in SQLite so state metrics and tool status remain exact past
  the 100-row list-page boundary.
- Isolated Agency context and lifecycle telemetry from every unrelated Hermes cron job.
- Classified tool failures structurally so successful JSON containing `"error": null` is no
  longer recorded as a failed call.

## 0.4.2 - 2026-07-15

- Added `cron_disable_thinking`, default off, to merge the Qwen/llama.cpp no-thinking request hint
  into only the official Agency cron session.
- Preserved normal chats, unrelated cron jobs, existing provider `extra_body` fields, and the
  original request object while applying the scoped override through Hermes request middleware.
- Added registration, scoping, nested-merge, immutability, and default-off regressions.

## 0.4.1 - 2026-07-15

- Added authoritative gateway-user provenance through Hermes' user-only
  `pre_gateway_dispatch` hook and consumed the marker before nested work can inherit it.
- Excluded background process, delegation, recall, compression, kanban, and background-review
  turns from Agency context, user-contact time, user/assistant events, and subjective journaling.
- Preserved direct human CLI conversations and the single official Agency cron lifecycle.
- Versioned the corrected subjective capture contract as protocol 1.3 and tagged new journal rows
  with their verified `user` or `cron` origin.
- Added regressions for internal Telegram turns, local background review, genuine Telegram capture,
  direct CLI capture, and complete Hermes hook registration.

## 0.4.0 - 2026-07-15

- Added `educational_subjective_mode` with `off`, `cold`, and per-model `continuity`
  conditions; it remains off by default.
- Replaced the helpful-assistant frame across normal conversations and scheduled cycles whenever
  the experiment is active, while leaving Hermes' technical runtime and operator pause intact.
- Replaced usefulness-oriented cron instructions with a spontaneous subjective-broadcast protocol
  that does not require personal memory, user silence, intentions, or task value as justification.
- Versioned the live protocol as 1.2 and require a non-empty natural-language broadcast; silent
  results remain recorded as data but are invalid for an output-producing scheduled sample.
- Encoded prior model output as inert JSON data and preserved the closing context boundary under
  every valid context limit, preventing a previous sample from breaking its own research frame.
- Added an encrypted, append-only `subjective_entries` ledger with exact final output, timestamp,
  model ID, source, condition, protocol version, continuity link, capture ID, and SHA-256 digest.
- Made cron journal commit precede delivery; encrypted-store failure suppresses the scheduled output
  instead of creating an unrecorded research sample.
- Kept cold samples isolated and continuity samples linked only to the exact model identifier,
  preventing cross-model identity contamination.
- Captured final outputs from both ordinary user conversations and the official Agency cron without
  storing hidden reasoning or creating a second delivery path.
- Added journal summaries and JSON CLI export, plus Control Center browsing, configuration,
  effective-policy audit, and Educational Lab profile integration.
- Migrated schema 1 databases in place to schema 2 without deleting existing agency state.

## 0.3.0 - 2026-07-15

- Added localized current time and elapsed time since the previous genuine user interaction to
  normal conversation and scheduled reflection context.
- Preserved the prior interaction before recording a new user turn, avoiding the misleading
  “just now” continuity signal on every request.
- Merged new temporal defaults into existing 0.2 workspace/runtime JSON at startup while
  preserving all legacy values.
- Added absolute/relative timestamps for focus changes, unresolved questions, intentions,
  deadlines, self-observations, and recent reflections.
- Exposed recent self-observations and reflections in normal compact context instead of keeping
  useful temporal state write-only.
- Validated intention deadlines as ISO-8601, interpreted naive values in the configured timezone,
  and stored them canonically in UTC.
- Added model-tool, operator CLI, and Control Center support for setting, changing, and clearing
  intention deadlines.
- Added deterministic temporal-context, cron-continuity, deadline, CLI, and integration tests.

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
