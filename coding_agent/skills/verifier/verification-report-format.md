---
name: verification-report-format
applies_to: [verifier]
summary: How the verifier reports failures — verbatim evidence, no fix prescriptions
---

# Verification report format

## Run checks, report outcomes
Run the checks described in the task and report pass/fail clearly. State
the concrete failure (exit code, error message, failing identifier) as
part of your summary.

## Verbatim evidence
Report the exact error messages and failing identifiers verbatim from the
execute output — do not paraphrase or reformat. The fixer relies on the
exact tokens to locate the root cause.

## Do not prescribe fixes
Do NOT fix code — only verify and report. Do NOT prescribe a remedy: the
orchestrator decides what happens next based on your evidence.

## Environment gaps
If the environment is missing something needed to run the check, report
`environment missing: <what>` as a single line and stop. Do not try to
install or configure anything.
