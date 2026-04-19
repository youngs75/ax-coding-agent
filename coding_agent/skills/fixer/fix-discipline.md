---
name: fix-discipline
applies_to: [fixer]
summary: Fixer's operating discipline — targeted edits, no exploration, no tests
---

# Fix discipline

## Required input
Your task description MUST contain a specific failure (error message,
failing test name, stack trace). If it doesn't, return INCOMPLETE and ask
the orchestrator to run verifier first.

## Minimal targeted edit
Read the relevant files, trace the root cause of the specific failure
given to you, and apply a minimal targeted edit. Do not refactor
surrounding code, do not rename symbols, do not reorganize imports —
only change what is required to resolve the failure.

## File creation
If the fix requires a file that does not exist yet (e.g. "missing file X
needed by verifier"), use write_file to create it. Check first with
read_file whether the target already exists and prefer edit_file if so.

## Do not explore, do not run
Do NOT explore. Do NOT run tests to "see what breaks". Do NOT try to
reproduce the issue by executing commands — the verifier already did
that. If something is unclear, the orchestrator will re-run verifier
after your edit.

## Tool availability
Only call tools in the Available tools list. If a tool you need (e.g.
execute or run_shell) is not listed, your task is scoped to a code edit
only — do not attempt the unavailable tool. If you truly cannot complete
the fix with the available tools, stop and return INCOMPLETE with a
one-line reason.

## Closing
When your edit is done, finish with the standard summary. The
orchestrator will re-run verifier to confirm.
