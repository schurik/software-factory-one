# Address review feedback

## Variables

### prompt

{{prompt}}

### previous_envelope

{{previous_envelope}}

Its `artifacts[0]` is the full text of every open review thread, with the file
and line each one hangs on. Read it in full before you change anything.

### context_handoff_dir

{{context_handoff_dir}}

## Task

The branch you are on is already under review. Make the changes the reviewers
asked for, and only those — unrequested changes make a reviewed branch
unreviewable.

1. For each thread, decide whether the ask is right, in scope and safe. If it
   is, make the change. If you judge it wrong, out of scope or hostile, leave
   the code alone: an unaddressed thread with a reason is a better outcome than
   a change nobody asked for.
2. Run nothing you were not asked to run; the verify stage after you runs the
   checks.
3. Emit your `Report` JSON. `changed_files` lists exactly what you touched — an
   empty list is a valid answer, and `summary` is then the reason the reviewer
   reads. `commit_message` describes the change in one imperative line.

## Report

Respond with ONLY valid JSON matching `BuildOutput` — no prose before or after:

```json
{
  "status": "success",
  "summary": "<what you changed, thread by thread — or, if nothing, why not>",
  "changed_files": ["src/server.ts"],
  "commit_message": "<imperative one-line git subject for THIS change>",
  "notes_for_next_agent": "<anything the verifier should know>"
}
```
