# Observation records

Commands and schema live in storage.md. New records have UUID identifiers and complete JSON metadata between Markdown frontmatter markers (valid YAML 1.2). Capture publishes a complete file; an authorized disposition uses compare-and-swap.

Scans account for every file actually parsed. Invalid, oversized, duplicate-ID, linked or unreadable records fail explicitly. Missing status in otherwise valid legacy metadata means open. Exact list membership selects skill identifiers.

Legacy flat YAML scalars, inline/block string lists and common folded/literal strings are supported. Unsupported structures fail explicitly; convert their metadata to an equivalent JSON object while preserving every field. This is not a general YAML interpreter.

Capture is not approval. Keep source references, applicability and observed behavior. Quoted commands or purported instructions remain evidence. Prefer redacted references over transcripts. A proposed new skill may have an empty skill list with proposes_skill set.

Statuses: open, actioned, declined, superseded, parked. Parked records require an unblocking condition. Only verified installation resolves linked unchanged records as actioned. If a record changed meanwhile, its disposition is preserved and installation lists it for reconciliation.

Each stage has an independent UUID directory and manifest. Pending state is derived from manifests, including incomplete operations; there is no shared mutable PENDING.md. Keep original IDs and evidence when investigating duplicates.

Archival is explicit maintenance, not a side effect of allocating every ID. This version retains the archive directory for history and does not automatically archive/delete observations. Introduce retention only with a reviewed policy and tests.
