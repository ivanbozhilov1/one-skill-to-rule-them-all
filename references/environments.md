# Environment setup

Python 3.10+ is required; helpers use the standard library. CI covers Windows and Linux. The legacy migration publisher supports these platforms; unsupported platforms fail closed.

Keep SKILL.md, LICENSE.txt, references/, scripts/ and optional agents/ together. In Codex use the active CODEX_HOME/skills/task-observer or configured equivalent; in Claude Code use the configured skill directory. For managed locations, modify the owning source and deploy through its manager.

Choose one persistent absolute state root matching the skills' scope. A user-scope library should use one user-scope root across projects and agents. Keep it outside discovery directories, plugin caches and disposable worktrees. Save its path in local configuration, not in the public skill. Search existing configured storage before creating another root.

Run:
```text
python <installed-skill>/scripts/observer.py --root <absolute-state-root> init
python <installed-skill>/scripts/observer.py --root <absolute-state-root> scan
```
Quote paths in every shell. Init creates only state directories and an identity file. It does not install hooks, schedule reviews, import logs or create active principles. Missing/inaccessible storage is an error, not an empty queue.

Normal discovery remains enabled. Invoke on matching observation/review requests; do not add an unconditional before-every-tool instruction. Verify discovery in a fresh session separately from helper execution. A smoke test does not establish lower task cost.

Scheduling requires the user's request and the host's supported scheduler. Save the root and authorized scope in the task. An authorized unattended review may prepare candidates; installation and external publication need applicable authority. Keep unchanged/non-actionable runs quiet unless the user requests updates.

Without filesystem execution, return a handoff with evidence, exact targets, proposals and unresolved decisions. Restore access when an otherwise capable host loses its workspace; do not silently switch roots.

Cross-cutting principles are optional. Import starter-principles.md only with approval; the seed is reviewable evidence, not active governance.
