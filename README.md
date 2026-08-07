# test-aware-agent

llama3.1 on Docker

## Connecting a target repo

1. Make the target repo available as a writable workspace/root to the process that runs this agent.
2. Point `config.json` at that repo:

```json
{
  "defaultProject": "targetRepo",
  "instructionsPath": "templates/test-agent-instructions.md",
  "projects": {
    "fiestaconnect": {
      "path": "C:/Users/name/targetRepo",
      "registry": "test-registry.json"
    }
  }
}
```

3. Keep a test registry in the target repo. The registry maps changed files to runnable test commands.
4. Run `doctor` in the agent prompt to check repo and registry visibility.
5. Run `doctor-write` only when you want to confirm the agent can create and remove a temporary file in the target repo.

If the target repo is not mounted as writable, the agent may still read snippets but cannot reliably create or update tests there.

## Broken-test repair state

Failed test runs are captured for later repair in two files inside the target repo:

- `.test-agent/test-failures.json` is the current queue of open failures that may need repair.
- `.test-agent/test-failure-history.jsonl` is an append-only audit trail of failures and repair attempts.

Keep the current queue focused on unresolved failures. Use the history file for debugging or auditing what happened across runs.

## Commands

- `failures` lists captured test failures that can be inspected or repaired.
- `inspect-failure <id-or-file>[, <file>]` shows one captured failure. If no ID is provided, it uses the latest open failure.
- `fix-tests <id>` attempts a guarded repair for one captured failure.
- `fix-tests <source-or-test-file>[, <source-or-test-file>]` finds a matching captured failure, or runs related tests first to capture one.
- `fix-tests` uses the latest open failure.
- `clear-failures` clears the current failure queue without deleting the history file.
- `test-all` runs the target repo's full test suite with `npm run test`. Aliases: `all-tests`, `run-all-tests`, `test all`, `run all tests`.
- `test-diff [base-ref]` reads the target repo's `git diff` against `HEAD` by default and automates test work from file status. Alias: `diff-tests`.
  - Added/copied source files run through `create-tests`.
  - Modified/renamed source files run through `edit-tests`.
  - Changed test files run directly.
  - Deleted source files remove safely matched test files and prune exact registry mappings.
  - Deleted test files are pruned from the registry.
  - Deleted config or non-code files are reported as skipped.
- `create-tests <source-file>[, <source-file>]` creates or registers tests for a feature change when coverage is missing.
- `create-missing-tests [--limit <count>]` scans the target repo for source files without nearby or registered tests, then creates/registers tests in small batches.
- `edit-tests <source-file>[, <source-file>]` updates existing related test cases for an updated feature, then reruns the edited or matched tests. `update-tests` is an alias.
- `simulate <source-file>[, <source-file>]` runs the registry suites that match the changed source files.
- `diff <source-file>[, <source-file>]` previews diffs for one or more changed source files.

For an existing feature that has been updated, run:

```text
edit-tests src/components/example/UpdatedFeature.tsx
```

For multiple files, separate paths with commas:

```text
edit-tests src/components/example/UpdatedFeature.tsx, src/hooks/useUpdatedFeature.ts
```
