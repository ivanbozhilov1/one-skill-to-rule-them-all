# Legacy log migration

For pre-3.0 single-file logs only. Fresh installations do not need migration. Input logs remain unchanged; converted records use JSON-object frontmatter with preserved bodies and metadata.

```text
python scripts/migrate-log.py --check <absolute-legacy-log>
python scripts/migrate-log.py --convert <absolute-legacy-log> --out <absolute-new-directory> --id-floor-from <absolute-existing-archive>
```

Check and convert are mutually exclusive. Conversion refuses occupied output, duplicate IDs, filename collisions, invalid records and unreadable floor sources before publication. Supply every authoritative archive with repeated --id-floor-from arguments. Singular/plural/repeated reference fields are preserved.

All records render before a sibling lock and unique staging directory are created. Publication never replaces another destination: Windows directory rename or Linux renameat2(RENAME_NOREPLACE). Other platforms fail closed with recovery state retained.

Keep old logs and archives. Compare output count, bodies, references, flags and historical floor before adoption. A zero exit alone is not proof of losslessness. Initialize a new observer workspace, then integrate verified records into observation-log through a scoped action; never replace a populated log wholesale.

Interrupted conversion may retain a sibling lock and stage. Inspect the owner and contents; confirm the owner stopped before releasing its lock. Preserve the interrupted stage and retry into a fresh destination. Do not automatically steal locks, truncate output or lower floors.
