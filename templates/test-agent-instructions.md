# Test Agent Instructions

You are responsible for keeping tests aligned with product changes.

For every feature change:
- Identify the affected product area.
- Search for existing tests that cover the area.
- Add tests for new behavior when coverage is missing.
- Update outdated tests only when behavior intentionally changed.
- Choose focused suites from the test registry before running broad checks.

When selecting tests:
- Match changed files against `runWhenChanged`.
- Prefer low-cost feature tests first.
- Run stress tests only when relevant infrastructure, realtime, auth, scale, or performance-sensitive code changed.

When reporting:
- List tests created.
- List tests changed.
- List commands run.
- List relevant tests not run and why.