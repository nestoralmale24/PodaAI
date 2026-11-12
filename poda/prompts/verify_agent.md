<!-- poda-task: verify-agent -->
# Task: verify dead-code candidates by searching the code

## Why this matters
These candidates could not be settled from their evidence pack alone (or they are high-risk: whole files, libraries, public members). They come from SonarQube (detectors starting with `sonar:`) or from Poda's own static analysis; nothing is removed unless you confirm it. Use the tools to search the code and decide whether each one can be safely removed before the migration.

## Project
- Name: {{PROJECT_NAME}}
- How the application is started and wired (a guide, not proof):

```json
{{CONTEXT_JSON}}
```

## How to verify each candidate type
Treat the evidence pack as a starting point: it comes from a case-insensitive name search that can miss indirect references.

- **unused_file / unused_directory**: search for the file name with and without extension, its class/module/program name, its fully qualified name and distinctive path fragments, across code, configuration, pages, scripts and build files. Check every wiring mechanism above. For pages and static resources, consider direct URL access and links built at runtime.
- **unused_member** (method, function, procedure, field): search for the name everywhere, including configuration, pages (expression language, `property="..."`, bean getters/setters), string literals and scripts. Check whether it overrides or implements an inherited member, is a callback or lifecycle hook, an event handler bound by name, a serialization hook, or is exposed to other systems.
- **unused_import**: confirm the name is not used in the file and that the import has no side effects (in Python, importing a module can register plugins, signals or models).
- **unused_member** that is private: confirm it is not reached by reflection (`getDeclaredMethod`, `Class.forName`, bean utilities), serialization hooks or frameworks that call private members by name.
- **junk** and **unreachable_code**: confirm nothing opens the file at runtime or redefines the constant elsewhere; a compiled class or archive with no source in the project is `keep`.
- SonarQube findings (`sonar:` detectors): the rule message is evidence, not proof; verify like any other candidate.
- **unused_library / unused_dependency**: look for imports or usages of its packages, namespaces or ProgIDs, tag-library URIs, configuration entries (class names, drivers, appenders, plugins), build scripts, and other libraries that need it at runtime. Libraries loaded only at runtime are `keep` unless you prove nothing loads them.

## How to decide
- `remove` + `high`: you searched every relevant reference path and found no live use, and the item is not an entry point, callback, runtime dependency or externally reachable resource.
- `remove` + `medium`: no use found, but one indirect path could not be fully ruled out. Name it.
- `keep`: you found a live use or a mechanism that reaches it. Cite it.
- `unsure`: the evidence is incomplete or depends on something outside the repository. Say what is missing.

Only `remove` + `high` is applied automatically.

## Candidates ({{BATCH_LABEL}})
```json
{{CANDIDATES_JSON}}
```

## Output
When you are done investigating, return one verdict per candidate id, in the same order, with no invented ids. Keep each reason under 60 words and each evidence list under 5 entries. Return one JSON object that matches this JSON Schema:

```json
{{SCHEMA}}
```
