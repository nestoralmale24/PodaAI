<!-- poda-task: discover -->
# Task: find dead code that static analysis cannot see

## Why this matters
Poda already found the items under "Already known" with static analysis: unreferenced files, unused members and imports, constant-false branches, code after `return`, commented-out code and unused libraries. It cannot reason about program logic across files or about configuration semantics. Find the remaining dead or obsolete code inside the scope below, precisely enough that it can be removed safely before the migration.

## Project
- Name: {{PROJECT_NAME}}
- How the application is started and wired:

```json
{{CONTEXT_JSON}}
```

## Scope
Only analyse `{{SCOPE}}` (relative to the root). Report at most {{MAX_FINDINGS}} findings, the most valuable first (largest amount of provably dead code).

## What to look for
1. **Permanently disabled features**: flags, parameters or constants defined in another file or in configuration that switch a code path off in every configuration present in the repository.
2. **Dead configuration**: mappings, routes, beans, forwards, menu entries, scheduled jobs or properties that point to classes, pages or methods that do not exist, or that nothing can trigger.
3. **Unused members the name search missed**: public methods, functions, procedures or fields whose names are common words, when you can prove that no caller exists anywhere (pages and configuration included).
4. **Leftovers**: test or debug pages and endpoints, sample code, and old versions of a component that were replaced (`LoginOld`, `*_bak`, `Copia de ...`, `v1`), as long as nothing reaches them.

Do not report: style issues, duplicated code that is still used, TODO comments, deprecated-but-used APIs, generated code, or anything already known.

## Already known (do not report again)
{{KNOWN_FINDINGS}}

## Location rules (mandatory)
Poda removes whole lines only, and only when the lines still match what you report.
- `start_line` and `end_line` are 1-based and inclusive, and cover one complete syntactic unit (a whole method, a whole XML element, a whole statement or block), including comments or annotations directly above it that only describe it.
- `first_line_text` and `last_line_text` are the exact text of those two lines, without leading or trailing spaces.
- For a whole file or directory, give `path` and leave the line fields null.
- If removing it would require editing part of a line or other lines, or would leave the code unable to compile or run, use decision `unsure` and explain what a human must change.

## Output
Return one JSON object that matches this JSON Schema (an empty `findings` list is a valid answer):

```json
{{SCHEMA}}
```
