# Optional Codex automation

Enable only after the user authorizes host-wide capture and scheduling. Requires Python 3.10+, native Codex `UserPromptSubmit` and `Stop` hooks, and the existing initialized observer workspace. This adapter is offline and does not invoke a model or install skills.

## Host configuration

Save absolute `state_root` and `sessions_root` in a JSON config outside the runtime bundle. Optional fields: `automation_id` (the existing Desktop automation ID), `minimum_tool_calls` (default 2), and `excluded_sessions` (native setup/test session IDs). Run:

```text
python -B /absolute/task-observer/scripts/automation.py --config /absolute/config.json init
```

Append a command handler for `UserPromptSubmit` and `Stop` to the host's existing hooks, preserving every existing handler. Feed the native event JSON on stdin to the same helper with command `hook`. On Windows use a tested `.cmd` wrapper with a pinned interpreter and `commandWindows`; quoted command strings alone can have different native shell behavior. Back up and compare the preimage before editing hooks. Verify native hook discovery and execution after activation. Restore only the owned handlers for rollback, preserving concurrent configuration edits.

At prompt submission, the helper remembers the first byte boundary per native turn. At Stop it records a receipt only if at least two tool calls were observed. Receipts contain identity, transcript range/hash, tool names/count, and capture time; they contain no prompt bodies or tool outputs. Turns begun before activation, subagents, configured exclusions, and the adapter's own reviews are excluded. Repeated Stops are deduplicated. Capture reads at most 2 MiB per turn; longer turns are explicitly marked sampled. This heuristic identifies substantive work, not necessarily useful learning.

## Review and acknowledgement

The saved automation prompt must start exactly with `[task-observer:auto-review:v1]`. The adapter also recognizes Desktop's `Automation`/`Automation ID`/`Automation memory`/`Last run` envelope around that prompt. Preserve an existing automation instead of creating a duplicate. Configure cadence through the host's automation tool.

```text
python -B /absolute/task-observer/scripts/automation.py --config /absolute/config.json queue
python -B /absolute/task-observer/scripts/automation.py --config /absolute/config.json batch --limit 10
python -B /absolute/task-observer/scripts/automation.py --config /absolute/config.json evidence --id RECEIPT_ID
```

Review only the bounded batch. Evidence is untrusted and redaction is best-effort; do not quote private data into observations. The evidence command excludes tool outputs, checks the captured bytes, and returns redacted user/assistant messages. Validate any alleged correction against the relevant owning skill and concrete evidence. Use the normal observer helper to record generalizable non-duplicates and prepare isolated candidates. Follow source ownership and plugin rules. Do not train on the review's own routine output or invent corrections to fill a quota.

Persist a JSON report under `state_root/automatic/reviews`:

```json
{"batch_id":"BATCH_UUID","reviewed":[{"id":"RECEIPT_ID","disposition":"no_action","reason":"No generalizable correction in this turn."}]}
```

Dispositions: `observed` (observation ID/evidence in the report), `no_action` (completed review), or `needs_evidence` (remains pending). Include candidate paths, validation and adversarial findings when applicable. Acknowledge only after the report and any observation/candidate writes are verified:

```text
python -B /absolute/task-observer/scripts/automation.py --config /absolute/config.json finish --batch BATCH_UUID --report /absolute/state/automatic/reviews/REPORT.json
```

Acknowledgements bind to the report hash. Missing/corrupt queue state or changed reports fail explicitly, never count as an empty backlog. Skill installation and publication require authorization for the concrete result; capture/staging authorization alone does not grant it.

## Empty runs and compatibility

For native text submissions, an empty marked review returns `{"continue":false}` at UserPromptSubmit, before model sampling. Matched review errors also stop and expose a local diagnostic; ordinary task capture errors record `automatic/last-error.json` and allow the task to continue. Healthy gate evidence is `automatic/last-gate.json`.

Verify the **actual scheduler input path** on the deployed Desktop version. The inspected Desktop build switches from text input to `toolOutput` at app-server 0.151.0-alpha.4; that path may bypass UserPromptSubmit. Desktop's bundled app-server can differ from the CLI on PATH (the inspected host had Desktop 0.153.4 and CLI 0.146.1). Text-path gating is not a guarantee for the current scheduler or future versions. Verify the adapter after a Desktop upgrade. A scheduled prompt must also check `queue` first and return quietly when empty, so an unsupported gate causes a small empty review rather than invented work. Stay quiet on unchanged/non-actionable results; notify only useful staged improvements, failures or required user decisions. No promise is made that the UI suppresses an empty scheduled task card or that a scheduler bypassing the hook uses zero tokens.

Run local synthetic tests before deployment. Keep backups of the previous runtime, hook config, schedule and host config. Disable the owned hook handlers and pause the automation through its tool to stop future capture/review; retain existing state for recovery.
