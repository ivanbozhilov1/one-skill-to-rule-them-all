# Task Observer user guide

Task Observer captures reusable workflow evidence and helps review skill changes. Installing it makes the skill available; it does not add a universal observer to every session.

## Start a scoped observation task

Ask: “Observe this task for recurring skill problems and record evidence.” Choose one persistent absolute workspace outside your skills directory, plugin cache and worktrees. Reuse it across projects when observing user-level skills.

```text
python <installed-skill>/scripts/observer.py --root <absolute-state-root> init
python <installed-skill>/scripts/observer.py --root <absolute-state-root> scan
```

The agent captures redacted evidence and exact skill IDs. It does not write to managed memories or modify skills merely because it found a possible improvement. No observations is a valid outcome.

## Review and change

Ask: “Review the observations for this skill and prepare the justified improvements.” The agent verifies sources, classifies scope/conflicts and stages a unique candidate from an immutable baseline. It edits the candidate, tests behavior and prepares a merge with current live content. New arrivals remain outside the reviewed set.

When you authorize installation, the helper checks for drift, validates the full candidate, preserves a backup and verifies installed files. It resolves only unchanged observations linked to the installed candidate. Managed/plugin skills must be updated through their owning source or manager.

Use [storage commands](references/storage.md) for capture, exact lookup, dispositions, stage/prepare/install, pending state and rollback. Use [legacy migration](references/migration.md) only for old single-file logs.

## Limits

Python 3.10+ is required. Helpers are offline and use the standard library. Unsupported legacy YAML fails explicitly; JSON metadata preserves nested values without dependencies. Windows/Linux migration uses no-replace publication. A crash may leave a visible lock or incomplete stage that needs owner-confirmed recovery.

A prepared artifact is not an installed change. A successful smoke test is not fresh-session activation proof or a token-savings benchmark. Scheduled reviews are optional and require separate scheduling authorization.
