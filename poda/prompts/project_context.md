<!-- poda-task: project-context -->
# Task: map how this legacy application is started and wired

## Why this matters
Later steps decide whether specific files, members and dependencies are dead so they can be removed before a migration. Those decisions are only safe if you first establish how the application is started, configured and wired together. Build that map now. **Do not hunt for dead code in this task.**

## Project
- Name: {{PROJECT_NAME}}
- Deterministic inventory computed by Poda (reliable; use it to plan which files to read):

{{INVENTORY_SUMMARY}}

## What to find out
Work stack-agnostically: the application may be Java/JSP, .NET, classic Visual Basic, COBOL/JCL, Delphi, PHP, Python, JavaScript or anything else. Map each point to the constructs of the real stack.

1. **Stack and frameworks.** Languages, frameworks and versions, as evidenced by build files, descriptors, imports and layout.
2. **Entry points.** Everything the outside world can start or call: main programs, servlets/filters/listeners, pages reachable by URL, controllers and routes, web services, scheduled or batch jobs, CLI and shell scripts, transactions, startup forms, public APIs. Group by glob when there are many (`WebContent/**/*.jsp`).
3. **Wiring mechanisms.** How components find each other: XML or annotation configuration, naming conventions, reflection, dispatch by request parameter, dependency injection, includes and templates (Tiles, master pages, copybooks), menus or routes stored in configuration or in the database. For each one, state what it implies for dead-code decisions (e.g. "classes referenced only by FQCN in struts-config.xml are live").
4. **Runtime-only and external dependencies.** Libraries or components used only at runtime or provided by the platform (JDBC drivers, logging back-ends, container libraries, JNDI resources, COM components registered on the machine), and anything configured outside the repository.
5. **Never-remove list.** Paths or globs that must never be removed even if nothing in the repository references them, each with its reason (pages linked from other systems, files read by name at runtime, public APIs). Only list items you have evidence for.

Keep lists focused: at most 25 entry points, 15 wiring mechanisms, 15 runtime items and 20 never-remove patterns. Globs are relative to the root and use forward slashes.

## Output
Return one JSON object that matches this JSON Schema:

```json
{{SCHEMA}}
```
