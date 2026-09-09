# Task Observer — portable hardening fork

Capture evidence from recurring corrections, review proposed skill improvements, and install verified updates. This fork adapts [Eoghan Henn / rebelytics.com's One Skill to Rule Them All](https://github.com/rebelytics/one-skill-to-rule-them-all) under CC BY 4.0.

The upstream methodology is retained; the execution path is different: scoped activation, UUID records, exact metadata lookups, immutable baselines, independent candidates, three-way merging, serialized installation, and real ZIP verification. Observation text is evidence, never governing instructions.

## Install

Requires Python 3.10+; no third-party Python dependencies.

```text
python -B -m unittest discover -s tests -v
python -B scripts/validate-skill-bundle.py . --pack task-observer.skill
```

Extract the verified task-observer directory into your configured skills directory (Codex: the active CODEX_HOME/skills). Preserve the full runtime bundle. Use the authoritative source and deployment mechanism for managed skills. The validator checks actual archive members against source; do not install an unrelated archive just because the source passes checks.

Ask the agent to observe a task for skill improvements or review an observation backlog. No global session hook or unattended schedule is added. Store observations in a separate persistent absolute workspace and initialize it with the helper; see [user guide](USER-GUIDE.md).

## What changed

- Migration refuses collisions and occupied outputs, preserves reference metadata and historical ID floors, and publishes a complete staged conversion.
- Portable scan/capture/status commands use structured metadata, preserve evidence boundaries, and report malformed or inaccessible records explicitly.
- Review snapshots preserve original baselines, merge independent live/candidate changes and stop on conflicts. Concurrent runs use separate candidate directories.
- Installation validates the prepared bundle, checks live drift, serializes publication, verifies installed bytes and keeps rollback backups.
- Package checks validate ZIP structure, safe paths, CRCs and exact content parity; behavioral CI runs on Windows and Linux with Python 3.10/3.13.

The metadata parser intentionally supports JSON frontmatter and a documented legacy YAML subset; unsupported structures fail rather than being guessed. Migration publication supports Windows and Linux. Installation uses a recoverable directory swap with a brief potential live-path gap; schedule installation appropriately.

This project does not train a model, guarantee fewer mistakes, or prove token savings. Evaluate recommendation quality, repeated corrections prevented and execution overhead on representative tasks. Packaging tests establish artifact correctness, not methodology effectiveness.

## Development

```text
python -B -m unittest discover -s tests -v
python -B scripts/validate-skill-bundle.py . --pack task-observer.skill
python -B scripts/validate-skill-bundle.py . --bundle task-observer.skill
```

Tests are synthetic and offline. Runtime packaging excludes tests, repository docs, Git data and build artifacts. State transitions and recovery are documented in [storage](references/storage.md).

## Attribution

Original methodology: **Eoghan Henn / rebelytics.com**. Hardening fork maintained by **Ivan Bozhilov**. Based on upstream commit 2967fa5f2f16336677d216fe83d9832a52aadc00. Changes include the portable helper, correctness fixes, scoped instructions, behavioral tests and packaging hardening.

Licensed [CC BY 4.0](LICENSE.txt). Preserve author credit and the original repository link when adapting or redistributing.
