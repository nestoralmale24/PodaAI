# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

Poda AI (command and Python package `poda`; formerly *deadcode*) is a standalone Python tool (Python ≥ 3.10), driven from a local web dashboard, that removes dead code from a legacy application before it is documented or migrated. `DEADCODE_*` variables, `.deadcode-settings/` and `<app>-deadcode/` outputs from before the rename are still recognised.

- **Input**: a project directory. The original project is never modified.
- **Output**: `output/<project>/` (`config.default_output_dir`: `$PODA_OUTPUT_DIR` or `<home>/output`, kept apart from the apps in `target/`; `--out` overrides it), containing:
  - a clean copy (`<project>/`);
  - `poda_report.md`, `findings.json`, `execution_metrics.json` and `execution_metrics.md`;
  - `run.log`;
  - `.poda/llm-cache.json`, used by `--resume`.

## Running

```sh
uv venv && uv pip install -r requirements.lock && uv pip install --no-deps -e .   # local setup
python -m poda run <app> -y --plain                # autonomous run with the provider from .env
python -m poda run <app> --no-llm --dry-run        # free deterministic preview
python -m poda check --ping                        # provider/model check (one tiny call)
python -m unittest discover -s tests -v                # tests: fake LLM and fake SonarQube, no network
```

**Docker**:

- Build and enter with `docker compose up -d --build` and `docker exec -it poda-cli bash`.
- The repository is mounted at `/home/poda/workspace`, so code changes do not need a rebuild.
- Dependencies come from `requirements.lock`, installed with `--require-hashes`.
- After changing `pyproject.toml`, regenerate the lock:

```sh
uv pip compile pyproject.toml --universal --python-version 3.11 --generate-hashes --no-header -o requirements.lock
```

## Architecture

**`pipeline.py`** runs eight phases: 1. inventory; 2. SonarQube (mandatory from the dashboard). Poda AI has its own SonarQube in docker-compose (pinned version, own PostgreSQL, `poda`-prefixed containers/volumes, UI on 9001): never point it at another tool's server, because `sonar_setup.py` changes the admin password and creates the token on its own (stored in `.poda-settings/`). Each run binds the project to the "Poda AI (código sin uso)" profile (only rules tagged `unused`), ignores the app's own `sonar-project.properties` and pages past SonarQube's 10,000-result cap by batches of files; 3. Poda's own static analysis; 4. AI project context; 5. AI verification; 6. AI discovery; 7. apply; 8. reports.

**Nothing is removed without the AI**: after phase 3 every finding with an edit gets `needs_llm = True`, SonarQube's included. With `--no-llm` (CLI only) the run is a report and removes nothing. Phases 4–6 are skipped with `--no-llm`. The core reports everything through `events.EventBus`, and `utils/ui.py` renders it with `RichUI` (rich Live), `PlainUI` or `FileLog`. Never print from the core.

**Static layer**: `core/static_analyzer.py` runs the analyzers in `core/analyzers/` and merges duplicates.

- `junk`, `comments`, `dependencies` (pom, Gradle, npm, pip, vbp and NuGet, plus jar bytecode, jars inside `.war`/`.ear` and `~/.m2`).
- `reachability`: file reachability, members and imports.
- `ast_members`: tree-sitter members and unreachable code, falling back to the `legacy_scanners` regexes.
- `sonar`: optional.

**Semantic layer**:

- `core/llm_engine.py` is the only module that calls LiteLLM. It handles structured output, one repair attempt, fatal versus per-call errors, cost and the `--resume` cache.
- `core/semantic.py` runs project context, verification (snippet with `evidence.py` packs, escalating to `agent.py`) and discovery.
- `core/tools.py` provides the agent's read-only, sandboxed tools.

**Rewriting**: the LLM never edits code.

- `core/rewriter.py` decides remove, keep or review, applying the confidence policy and the `never_remove` patterns.
- `core/edits.py` applies line deletions byte-exactly on the copy.
- `core/validators.py` reverts any file that gets worse.

**Providers**: `config.py` resolves the provider into a LiteLLM model string plus `api_base`/`api_key`:

| Provider | Resolves to |
|---|---|
| `litellm` | `litellm_proxy/<alias>` |
| `copilot` | local proxy `poda/providers/copilot_proxy.py` via `anthropic/<model>` |
| `github_copilot` | LiteLLM native provider |
| `bedrock` | `bedrock/<id>` |
| others | passthrough |

`llm_settings.py` stores what `poda dashboard` saves (LiteLLM URL/key and the model selection in `.poda-settings/providers.json`, the Copilot token in `<copilot home>/github-token`); `load_env()` overlays it. `dashboard/runs.py` lists the apps under `target/` (`PODA_TARGET_DIR`), writes each result to `output/<app>/` (`PODA_OUTPUT_DIR`) and runs the same `Pipeline` in a background thread (one at a time, cancellable between steps and LLM calls), keeping its events for the page to poll; a provider is mandatory to start a run from the dashboard. `dashboard/server.py` is a stdlib HTTP server (loopback by default, per-run token in `X-Poda-Token`, Host check) serving `dashboard/index.html`. Precedence is: CLI options, then the dashboard, then `.env`, then the environment. `.env` and `.copilot/` live in `config.PROJECT_ROOT`, which is `PODA_HOME`, else the source checkout, else `~/.poda`.

## Conventions

- **LiteLLM calls**: always pass `_skip_mcp_handler=True` on LiteLLM calls. LiteLLM 1.106 otherwise imports the optional `mcp` package whenever `tools` are passed. `LITELLM_LOCAL_MODEL_COST_MAP=True` is set in `poda/__init__.py`.
- **Prompts**:
  - Prompts are English Markdown in `poda/prompts/`, each starting with `<!-- poda-task: name -->`, which the tests' fake LLM relies on.
  - `render()` fills `{{PLACEHOLDERS}}` in one pass and fails on missing ones.
  - Output schemas come from the pydantic models in `schemas.py`; never hand-write JSON Schema.
- **Legacy files**: never re-encode them. Edit bytes and split lines only on CRLF/LF/CR (`utils/text.py`).
- **Uncertainty**: anything uncertain goes to manual review. Only `remove` with high confidence (or medium, with `--min-confidence medium`) is applied.
- **Tests**: the LLM is replaced via `PODA_LLM_TRANSPORT=module:function` (`tests/fake_llm.py`), and end-to-end tests run the CLI in a subprocess. No test may reach the network.
- **Copilot cost**: Copilot bills tokens in GitHub AI Credits at per-model rates that Poda AI cannot read back, so `providers/copilot_pricing.py` holds a copy of GitHub's rate table (`PRICES_AS_OF`, `PRICES_URL`) and `llm_engine` turns each Copilot call into an estimate (`cost_source = "copilot_estimate"`). Update the table when GitHub changes its rates; models it does not list keep `cost = None`.
- **Language**: console output and reports are in Spanish (English when `OUTPUT_LANGUAGE` starts with "en"); prompts are in English.
