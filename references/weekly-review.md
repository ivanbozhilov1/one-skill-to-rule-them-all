# Review observations and prepare updates

Use for a review request or an already-authorized scheduled review. Weekly cadence is optional. Reuse existing authority rather than asking again for an approved action.

1. Query metadata for relevant skills; read bodies only for applicable open records. Check pending manifests and owning skill sources. Resolve inaccessible or malformed state instead of treating it as empty.
2. Group and deduplicate against the actual skill. Classify reusable improvements, task-specific context, obsolete claims, conflicts and uncertainty. Validate evidence and counterexamples. Preserve a decline's rationale; new evidence can justify reconsideration.
3. When authority is missing, present concrete changes. New-skill scope, destructive restructuring, conflicts and principle propagation need their corresponding decision. An unattended preparation scope does not automatically authorize installation or publication.
4. Read skill-authoring.md. Stage the full owning skill with observer.py stage, linking the exact reviewed observation IDs. Edit only the unique candidate; preserve the immutable baseline, behavior and attribution.
5. Test the change, then prepare under existing authority. The helper checks record revisions and merges baseline-to-live with baseline-to-candidate. Conflicts or baseline tampering stop. Review the final prepared diff including incorporated live changes.
6. Late observations do not enter an approved set automatically. Leave them open or classify them and begin a new stage with applicable authority. A changed/declined linked record invalidates preparation.
7. When authorized, install. The helper checks the prepared artifact and current live hash, serializes publication, retains backup and verifies installed bytes. Only unchanged linked records become actioned. Use the owning manager for generated/plugin skills.
8. Report evidence, changes, verification, unresolved items, prepared/installed state and rollback path. Report partial work accurately.

Commands and recovery are in storage.md. Multiple workers may prepare independent candidates; publication uses a per-skill lock and live-hash precondition. Never use file length/date as a merge verdict or copy one side wholesale to satisfy a diff.

After a conflict, preserve all three inputs. Create a fresh stage from current live and integrate intended changes with an explicit resolution, then test and prepare again. Do not edit a stored baseline hash to bypass the guard.
