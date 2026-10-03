---
name: evidence-driven-debugging
description: "Diagnose runtime issues, trace root causes, and verify fixes with concrete evidence. Use when debugging Python errors, validating repo changes, or checking whether a patch actually resolves the reported behavior."
---

# Evidence-Driven Debugging

## When to Use
- A bug, crash, or unexpected behavior is reported.
- A change needs validation before claiming it is fixed.
- The issue could be caused by data flow, config, environment, or a bad assumption in the code.
- You need a reliable process that avoids guesswork and repeated patching.

## Core Principle
Fix the root cause, not the symptom. Every claim must be backed by observed behavior or project evidence.

## Procedure
1. Reproduce the problem exactly.
   - Capture the user-visible symptom, command, stack trace, input, or failing condition.
   - Note the environment: OS, Python version, relevant config, and external dependencies.

2. Narrow to the failing layer.
   - Read only the specific file and function involved.
   - Check the precise boundary where the bad data or unexpected state enters the system.
   - Prefer the minimal code path needed to verify the failure.

3. Trace the data flow.
   - Identify the values, inputs, and assumptions that affect the behavior.
   - Check config, transforms, branching, and type expectations.
   - Ask: "Where does this value come from, and what changes it?"

4. Form one hypothesis at a time.
   - If there are multiple explanations, test the most likely root cause first.
   - Avoid stacking unrelated fixes.
   - Keep the change as small as possible.

5. Implement the minimal fix.
   - Change only the code needed to address the identified root cause.
   - Preserve behavior outside the affected path unless a regression is directly required.

6. Verify with the smallest proving command.
   - Run the focused repro or relevant test.
   - Confirm the bug is actually resolved, not merely hidden.
   - Record the evidence: command, output, and result.

7. Check for regressions.
   - Review adjacent code paths that could be affected.
   - If the change touches shared logic, validate the nearest relevant scenarios.
   - Add or update a regression check if the bug is repeatable and important.

## Decision Branches
- If the issue cannot be reproduced, inspect configuration, environment, and recent changes before patching.
- If the code path is not yet localized, read narrower slices of the stack or logs rather than broad files.
- If multiple hypotheses remain, test one hypothesis at a time and keep the evidence trail clear.
- If the problem is external to the repo, document the environment mismatch or dependency issue instead of changing code blindly.

## Completion Checklist
- The exact symptom is reproduced or clearly described.
- The root cause is identified and linked to a specific code path or data source.
- The fix is minimal and addresses the root cause.
- Verification command or test was run and the result is captured.
- The change does not introduce an obvious regression in the nearby behavior.

## Example Prompts
- "Trace why this conversion job fails only on PPTX files with charts."
- "Find the root cause of this state mismatch and verify the fix with the smallest relevant check."
- "Reproduce this bug, isolate the failing layer, and propose the minimal safe patch."
- "Before we merge this change, what evidence proves it fixes the issue and avoids regressions?"
