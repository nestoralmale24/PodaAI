<!-- poda-task: verify-snippet -->
# Task: triage dead-code candidates from their evidence

## Why this matters
SonarQube and Poda's own static analysis flagged the candidates below as possibly dead. Nothing is removed unless you confirm it: even candidates the analysers consider certain need your verdict. Each one comes with an **evidence pack** that Poda collected for you: every occurrence of its names in the project (with the file and line, and whether that file is live, dead, documentation or generated), the declaration and its surroundings, and the analyzer's own findings. You have **no tools** in this step. Decide from the evidence; anything you cannot settle goes to a second pass where an agent can search the code.

## Project
- Name: {{PROJECT_NAME}}
- How the application is started and wired:

```json
{{CONTEXT_JSON}}
```

## How to decide
- `remove` + `high`: the evidence shows no live use, the item is not an entry point, callback, runtime dependency or externally reachable resource, and no wiring mechanism above could reach it.
- `remove` + `medium`: no use in the evidence, but one indirect path is not ruled out. Name it.
- `keep`: the evidence shows a live use or a mechanism that reaches it. Cite it.
- `needs_context`: a search of the code would settle it (for example, a reflective lookup or a configuration file that the evidence does not cover). Say what to look for.
- `unsure`: it depends on something outside the repository (another system, the database, the server configuration).

Type-specific rules:
- `commented_code`: decide only whether the comment holds disabled source code (`remove`) or knowledge worth keeping, such as documentation, examples, explanations of how or why something works, licence or tool directives (`keep`). A block that mixes an explanation with a code example is `keep`. Removing a comment never changes behaviour.
- `junk`: build output, OS or editor leftovers, logs, backups, temporary files, compiled classes whose source is in the project, or a deployable archive (`.war`/`.ear`) built from the project's own code. `remove` unless the evidence shows it is the only copy of something (a class or library with no source in the project, a hand-made archive) or something reads it at runtime (a log or data file the application opens by name).
- `unreachable_code`: `remove` only if the condition is a literal or a constant defined in the same file, it is false in every build and configuration, and the branch is not the only place where something needed elsewhere is defined. Debug switches that people flip by hand (`DEBUG = false`) are still dead for the migration: say so in the reason.
- Candidates whose `detectors` start with `sonar:` come from SonarQube. Treat the rule and its message as evidence, not proof, and verify them like any other candidate.

Only `remove` + `high` is applied automatically.

## Candidates ({{BATCH_LABEL}})
```json
{{CANDIDATES_JSON}}
```

## Output
One verdict per candidate id, in the same order, with no invented ids. Keep each reason under 60 words and each evidence list under 5 entries. Return one JSON object that matches this JSON Schema:

```json
{{SCHEMA}}
```
