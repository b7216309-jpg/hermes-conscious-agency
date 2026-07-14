# Security policy

## Scope and guarantees

Hermes Conscious Agency grants no external-action authority. Its model tool writes only to the
plugin's local database. In a proactive cycle, non-agency tools are blocked after the required
`tick` call. The model can pause itself but cannot resume, change configuration, operate cron, or
increase permissions through the model-facing tool.

For the plugin's own cron job, a final-output transform permits only the exact text committed by
`record_decision`. A missing `tick` or decision commit is converted to `[SILENT]` and recorded in the
ledger. This control requires Hermes' `transform_llm_output` hook. Do not place another non-empty
output transform ahead of Conscious Agency in plugin load order.

These controls do not replace Hermes' own approval, sandbox, credential, or platform security.

## Sensitive data

Transcript excerpts are disabled by default. If enabled, use SQLCipher and protect both the database
and `CONSCIOUS_AGENCY_DB_KEY`. Anyone with the key and database can read stored state. Anyone who can
modify Hermes configuration, plugin code, or the running process is already inside this plugin's
trust boundary.

Never commit `.env`, database files, keys, or exported user state.

## Reporting

Please report vulnerabilities privately to the repository owner before opening a public issue.
Include the version, Hermes version, reproduction steps, expected safety boundary, and observed
behavior. Do not include real user transcripts, API keys, database keys, or platform credentials.

## Supported versions

Until a stable release, only the latest tagged version is supported.
