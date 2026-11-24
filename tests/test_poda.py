"""Tests for Poda AI (no network: the LLM is tests/fake_llm.py, built on LiteLLM's mocks).

    python -m unittest discover -s tests -v
"""
import asyncio
import hashlib
import http.server
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for _p in (str(ROOT), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

from fixtures import LEGACY_1252, MAIN_SERVLET, build_fixture, class_bytes  # noqa: E402

from poda.config import (ConfigError, LLMConfig, default_output_dir,  # noqa: E402
                         read_dotenv, resolve_llm)
from poda.pipeline import Pipeline, RunOptions  # noqa: E402
from poda.core.agent import run_agent  # noqa: E402
from poda.core.analyzers import ast_members  # noqa: E402
from poda.core.analyzers.comments import line_score, score_block  # noqa: E402
from poda.core.analyzers.dependencies import class_refs  # noqa: E402
from poda.core.evidence import EvidenceBuilder  # noqa: E402
from poda.core.inventory import Inventory  # noqa: E402
from poda.core.llm_engine import LLMEngine, LLMFatalError, extract_json_objects  # noqa: E402
from poda.core.rewriter import decide, protected_by  # noqa: E402
from poda.core.tools import TOOL_SPECS, ReadOnlyTools  # noqa: E402
from poda.core.validators import validate  # noqa: E402
from poda.events import Event, EventBus  # noqa: E402
from poda.models import Finding, Findings  # noqa: E402
from poda.prompts import render  # noqa: E402
from poda.reporting.metrics_report import build_metrics  # noqa: E402
from poda.schemas import ProjectContext, VerdictBatch  # noqa: E402
from poda.utils.metrics import MetricsTracker, loc_stats, not_counted_text  # noqa: E402
from poda.utils.text import SYNTAXES, classify_lines, split_lines, split_lines_b  # noqa: E402
from poda.utils.ui import FileLog, PlainUI, RichUI  # noqa: E402

CLEAN_PREFIXES = ("LITELLM_", "COPILOT_", "PODA_", "SONAR_", "ANTHROPIC_", "AWS_", "FAKE_LLM_")


def test_env(tmp: Path, **extra: str) -> dict:
    """Process environment isolated from the developer's .env and credentials."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(CLEAN_PREFIXES) and k not in ("LLM_PROVIDER", "LLM_ENV_FILE", "EXCLUDE_DIRS",
                                                              "CLAUDE_CODE_USE_BEDROCK", "OUTPUT_LANGUAGE")}
    empty = tmp / "empty.env"
    empty.write_text("", encoding="utf-8")
    env.update({"PODA_ENV_FILE": str(empty), "PODA_HOME": str(tmp / "home"),
                "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1", "OUTPUT_LANGUAGE": "Spanish",
                "LITELLM_LOCAL_MODEL_COST_MAP": "True",
                "PYTHONPATH": os.pathsep.join([str(ROOT), str(HERE)])})
    env.update(extra)
    return env


def run_cli(args, env, cwd=None):
    return subprocess.run([sys.executable, "-m", "poda", *args], env=env, cwd=cwd,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                          errors="replace", timeout=600)


def snapshot(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


AI_ARGS = ["-y", "--plain", "--provider", "openai", "--model", "gpt-4o"]


class EndToEndWithAI(unittest.TestCase):
    """Full run: static analysis, context, snippet verification, agent escalation and discovery."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="poda-e2e-"))
        cls.project = cls.tmp / "legacy"
        build_fixture(cls.project)
        cls.before = snapshot(cls.project)
        cls.counter = cls.tmp / "calls.txt"
        cls.env = test_env(cls.tmp, PODA_LLM_TRANSPORT="fake_llm:transport",
                           FAKE_LLM_COUNTER=str(cls.counter), FAKE_LLM_PROJECT=str(cls.project))
        cls.out = cls.tmp / "out"
        cls.result = run_cli(["run", str(cls.project), "--out", str(cls.out), *AI_ARGS], cls.env)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def code(self, rel):
        return self.out / "legacy" / rel

    def findings(self):
        return json.loads((self.out / "findings.json").read_text(encoding="utf-8"))["findings"]

    def finding(self, path, category=None):
        items = [f for f in self.findings() if f["path"] == path and (category is None or f["category"] == category)]
        self.assertTrue(items, f"sin hallazgo para {path}")
        return items[0]

    def metrics(self):
        return json.loads((self.out / "execution_metrics.json").read_text(encoding="utf-8"))

    def test_run_succeeds_and_writes_every_output(self):
        self.assertEqual(self.result.returncode, 0, self.result.stdout[-4000:])
        for name in ("poda_report.md", "findings.json", "execution_metrics.json", "execution_metrics.md",
                     "run.log", ".poda/llm-cache.json"):
            self.assertTrue((self.out / name).exists(), name)
        self.assertFalse((self.out / "changes.diff").exists())     # no diff: it added nothing
        self.assertIn("🔎 busca", self.result.stdout)               # what the agent searches is visible
        self.assertIn("veredictos:", self.result.stdout)
        self.assertIn("🗑", self.result.stdout)
        self.assertEqual(snapshot(self.project), self.before)       # the original is never touched

    def test_junk_removed(self):
        self.assertFalse(self.code("core.12345").exists())
        self.assertFalse(self.code("Thumbs.db").exists())
        self.assertFalse(self.code("WebContent/WEB-INF/classes/com/acme/util/Helper.class").exists())
        self.assertTrue(self.code("WebContent/WEB-INF/classes/com/thirdparty/Only.class").exists())

    def test_unused_files_and_directories(self):
        self.assertFalse(self.code("src/com/acme/util/OldHelper.java").exists())
        self.assertFalse(self.code("WebContent/old/pantallaVieja.jsp").exists())
        self.assertFalse(self.code("WebContent/images/unused.png").exists())
        self.assertFalse(self.code("WebContent/js/vendor").exists())
        self.assertTrue(self.code("WebContent/images/logo.png").exists())
        self.assertTrue(self.code("src/com/acme/util/KeepMeHelper.java").exists())     # AI said keep
        self.assertTrue(self.code("WebContent/protected/kept.jsp").exists())           # never_remove
        self.assertTrue(self.code("src/com/acme/util/Helper.java").exists())

    def test_snippet_escalates_to_agent(self):
        old = self.finding("src/com/acme/util/OldHelper.java", "unused_file")
        self.assertEqual(old["llm"]["mode"], "agent")
        self.assertEqual(old["llm"]["decision"], "remove")
        self.assertEqual(old["status"], "removed")
        pantalla = self.finding("WebContent/old/pantallaVieja.jsp", "unused_file")
        self.assertEqual(pantalla["llm"]["mode"], "snippet")                           # settled without tools
        self.assertGreaterEqual(self.metrics()["llm"]["escalations_to_agent"], 1)

    def test_libraries_and_dependencies(self):
        lib = "WebContent/WEB-INF/lib/"
        self.assertFalse(self.code(lib + "unused-lib-1.0.jar").exists())
        self.assertTrue(self.code(lib + "struts-fake.jar").exists())
        self.assertTrue(self.code(lib + "beanutils-fake.jar").exists())    # needed by struts (bytecode)
        self.assertTrue(self.code(lib + "ojdbc-keepme.jar").exists())
        pom = self.code("pom.xml").read_text(encoding="utf-8")
        self.assertNotIn("commons-lang", pom)
        self.assertIn("struts-fake", pom)
        self.assertIn("junit", pom)                                          # AI unsure -> kept for review
        ET.fromstring(pom)

    def test_code_inside_files(self):
        servlet = self.code("src/com/acme/web/MainServlet.java").read_text(encoding="utf-8")
        self.assertNotIn("import java.util.List;", servlet)
        self.assertNotIn("neverUsed", servlet)
        self.assertNotIn("Helper.legacy()", servlet)
        self.assertNotIn("if (false)", servlet)
        self.assertIn("Helper.help(action);", servlet)
        self.assertEqual(servlet.count("{"), servlet.count("}"))
        helper = self.code("src/com/acme/util/Helper.java").read_text(encoding="utf-8")
        self.assertNotIn("orphanPublic", helper)
        self.assertNotIn("legacy()", helper)                                 # found by the discovery agent
        self.assertIn("help(Object o)", helper)
        app = self.code("WebContent/js/app.js").read_text(encoding="utf-8")
        self.assertNotIn("neverCalled", app)
        web = self.code("WebContent/WEB-INF/web.xml").read_text(encoding="utf-8")
        self.assertNotIn("OldServlet", web)
        self.assertIn("com.acme.web.MainServlet", web)
        index = self.code("WebContent/index.jsp").read_text(encoding="utf-8")
        self.assertNotIn("banner", index)

    def test_encoding_and_line_endings_preserved(self):
        data = self.code("src/com/acme/util/Legacy1252.java").read_bytes()
        expected = LEGACY_1252.replace("        // System.out.println(\"versión antigua\");\r\n", "") \
                              .replace("        // s = s.trim();\r\n", "").encode("cp1252")
        self.assertEqual(data, expected)

    def test_discovery(self):
        discovered = [f for f in self.findings() if f["category"] == "discovered"]
        self.assertEqual(sorted(f["status"] for f in discovered), ["failed", "removed"])
        failed = next(f for f in discovered if f["status"] == "failed")
        self.assertIn("no coinciden", failed["status_detail"])               # stale location never applied
        self.assertIn("package com.acme.util;", self.code("src/com/acme/util/Helper.java").read_text(encoding="utf-8"))

    def test_statuses_metrics_and_report(self):
        pom = [f for f in self.findings() if f["path"] == "pom.xml" and f["symbol"] and "junit" in f["symbol"]]
        self.assertEqual(pom[0]["status"], "review")
        self.assertEqual(self.finding("src/com/acme/util/KeepMeHelper.java")["status"], "kept")
        kept = self.finding("WebContent/protected/kept.jsp")
        self.assertEqual(kept["status"], "kept")
        self.assertIn("protegido", kept["status_detail"])
        metrics = self.metrics()
        self.assertGreaterEqual(metrics["llm"]["calls"], 6)
        self.assertGreater(metrics["llm"]["prompt_tokens"], 0)
        self.assertGreater(metrics["llm"]["tool_calls"], 0)
        self.assertEqual(metrics["llm"]["provider"], "openai")
        self.assertIsNotNone(metrics["cost"]["total_usd"])                    # LiteLLM price table for gpt-4o
        self.assertGreater(metrics["loc"]["reduced"]["code"], 0)
        self.assertGreater(metrics["changes"]["files_deleted"], 0)
        self.assertEqual(set(metrics["llm"]["by_phase"]), {"context", "verify", "discover"})
        report = (self.out / "poda_report.md").read_text(encoding="utf-8")
        self.assertIn("## Pendiente de revisión manual", report)
        self.assertIn("OldHelper.java", report)
        summary = (self.out / "execution_metrics.md").read_text(encoding="utf-8")
        self.assertIn("Tokens de entrada / salida", summary)
        self.assertIn("Líneas de código", summary)

    def test_resume_reuses_ai_answers(self):
        out = self.tmp / "out-resume"
        first = run_cli(["run", str(self.project), "--out", str(out), *AI_ARGS], self.env)
        self.assertEqual(first.returncode, 0, first.stdout[-3000:])
        before = self.counter.read_text(encoding="utf-8").count("\n")
        again = run_cli(["run", str(self.project), "--out", str(out), "--resume", *AI_ARGS], self.env)
        self.assertEqual(again.returncode, 0, again.stdout[-3000:])
        self.assertEqual(self.counter.read_text(encoding="utf-8").count("\n"), before)   # no new LLM calls
        self.assertIn("reutiliza", again.stdout)
        metrics = json.loads((out / "execution_metrics.json").read_text(encoding="utf-8"))
        self.assertEqual(metrics["llm"]["calls"], 0)
        self.assertGreater(metrics["llm"]["reused_results"], 0)
        self.assertEqual((out / "legacy/src/com/acme/web/MainServlet.java").read_bytes(),
                         self.code("src/com/acme/web/MainServlet.java").read_bytes())


class Modes(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="poda-mode-"))
        self.project = self.tmp / "legacy"
        build_fixture(self.project)
        self.env = test_env(self.tmp, PODA_LLM_TRANSPORT="fake_llm:transport",
                            FAKE_LLM_PROJECT=str(self.project))
        self.out = self.tmp / "out"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_poda(self, *extra, env=None):
        return run_cli(["run", str(self.project), "--out", str(self.out), "-y", "--plain", *extra], env or self.env)

    def test_nothing_is_removed_without_the_ai(self):
        result = self.run_poda("--no-llm")
        self.assertEqual(result.returncode, 0, result.stdout[-3000:])
        code = self.out / "legacy"
        self.assertEqual(snapshot(code), snapshot(self.project))                  # clean copy == original
        self.assertIn("sin IA: solo informe, no se elimina nada", result.stdout)
        findings = json.loads((self.out / "findings.json").read_text(encoding="utf-8"))["findings"]
        removable = [f for f in findings if f["edit"]]
        self.assertTrue(removable)
        self.assertTrue(all(f["needs_llm"] and f["status"] == "review" for f in removable))   # even the junk
        metrics = json.loads((self.out / "execution_metrics.json").read_text(encoding="utf-8"))
        self.assertEqual(metrics["llm"]["calls"], 0)
        self.assertEqual(metrics["cost"]["total_usd"], 0.0)
        self.assertIn("omitido (--no-llm)", result.stdout)
        self.assertIn("no cuentan como líneas de código: documentación", result.stdout)
        self.assertIn("sin comentarios, líneas en blanco, documentación ni metadatos del IDE", result.stdout)
        self.assertIn("No cuenta como código", (self.out / "execution_metrics.md").read_text(encoding="utf-8"))
        self.assertIn("documentation", metrics["loc"]["not_counted"])

    def test_every_finding_goes_to_the_ai_unless_capped(self):
        self.assertEqual(RunOptions(project=self.project).max_candidates, 0)       # default: no cap
        result = self.run_poda("--provider", "openai", "--model", "gpt-4o", "--no-discover", "--dry-run",
                               "--max-candidates", "1")
        self.assertEqual(result.returncode, 0, result.stdout[-3000:])
        findings = json.loads((self.out / "findings.json").read_text(encoding="utf-8"))["findings"]
        capped = [f for f in findings if "max-candidates" in (f.get("status_detail") or "")]
        self.assertTrue(capped and all(f["status"] == "review" and not f.get("llm") for f in capped))

    def test_dry_run_writes_no_code(self):
        result = self.run_poda("--provider", "openai", "--model", "gpt-4o", "--no-discover", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout[-3000:])
        self.assertFalse((self.out / "legacy").exists())
        self.assertTrue((self.out / "poda_report.md").exists())
        findings = json.loads((self.out / "findings.json").read_text(encoding="utf-8"))["findings"]
        self.assertTrue(any("neverUsed" in (f.get("symbol") or "") and f["status"] == "removed" for f in findings))

    def test_repair_when_first_answer_has_no_json(self):
        env = dict(self.env, FAKE_LLM_MODE="prose_first")
        result = self.run_poda("--provider", "openai", "--model", "gpt-4o", "--no-discover", env=env)
        self.assertEqual(result.returncode, 0, result.stdout[-3000:])
        self.assertIn("corrija el formato", result.stdout)
        self.assertFalse((self.out / "legacy/src/com/acme/util/OldHelper.java").exists())
        self.assertIn("omitido (--no-discover)", result.stdout)

    def test_agent_mode_never_keeps_unsettled_candidates(self):
        result = self.run_poda("--provider", "openai", "--model", "gpt-4o", "--agent-mode", "never")
        self.assertEqual(result.returncode, 0, result.stdout[-3000:])
        self.assertTrue((self.out / "legacy/src/com/acme/util/OldHelper.java").exists())   # needs_context
        self.assertFalse((self.out / "legacy/WebContent/old/pantallaVieja.jsp").exists())
        self.assertNotIn("🔎 busca", result.stdout)                                          # no tools at all

    def test_refuses_existing_output_without_flags(self):
        self.assertEqual(self.run_poda("--no-llm").returncode, 0)
        again = self.run_poda("--no-llm")
        self.assertEqual(again.returncode, 2)
        self.assertIn("--resume", again.stdout)
        forced = self.run_poda("--no-llm", "--force")
        self.assertEqual(forced.returncode, 0, forced.stdout[-3000:])

    def test_refuses_output_inside_project(self):
        result = run_cli(["run", str(self.project), "--out", str(self.project / "out"), "-y", "--no-llm"], self.env)
        self.assertEqual(result.returncode, 2)

    def test_missing_model_is_a_clear_error(self):
        result = self.run_poda("--provider", "openai")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Falta el modelo", result.stdout)

    def test_check_and_version(self):
        check = run_cli(["check", "--provider", "openai", "--model", "gpt-4o"], self.env)
        self.assertEqual(check.returncode, 0, check.stdout)
        self.assertIn("openai/gpt-4o", check.stdout)
        version = run_cli(["--version"], self.env)
        self.assertIn("Poda AI 2.", version.stdout)


class SonarIntegration(unittest.TestCase):
    """SonarQube Web API simulated with a local HTTP server."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="poda-sonar-"))
        self.project = self.tmp / "legacy"
        build_fixture(self.project)
        lines = MAIN_SERVLET.splitlines()
        comment = next(i for i, line in enumerate(lines, 1) if "Helper.legacy()" in line)
        unused = next(i for i, line in enumerate(lines, 1) if "neverUsed" in line)
        component = "poda-legacy:src/com/acme/web/MainServlet.java"
        issues = [
            {"rule": "java:S125", "component": component, "textRange": {"startLine": comment, "endLine": comment + 1}, "message": "commented"},
            {"rule": "java:S1128", "component": component, "line": 3, "textRange": {"startLine": 3, "endLine": 3}, "message": "import"},
            {"rule": "java:S1144", "component": component, "line": unused, "textRange": {"startLine": unused, "endLine": unused}, "message": "private"},
            {"rule": "java:S1481", "component": component, "line": 14, "textRange": {"startLine": 14, "endLine": 14}, "message": "local"},
        ]
        self.seen_auth = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(handler):
                self.seen_auth.append(handler.headers.get("Authorization"))
                if handler.path.startswith("/api/ce/task"):
                    body = {"task": {"status": "SUCCESS"}}
                else:
                    page = int(dict(p.split("=") for p in handler.path.split("?", 1)[1].split("&"))["p"])
                    body = {"issues": issues if page == 1 else [], "paging": {"total": len(issues)}}
                data = json.dumps(body).encode()
                handler.send_response(200)
                handler.send_header("Content-Type", "application/json")
                handler.end_headers()
                handler.wfile.write(data)

            def log_message(handler, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_summary_by_rule(self):
        from poda.core.analyzers.sonar import summarize
        issues = [{"rule": "java:S125"}, {"rule": "java:S125"}, {"rule": "java:S1144"}]
        summary = summarize(issues, {"removable": 3, "reported": 0, "ignored": 0}, "poda-legacy")
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["by_rule"][0], {"rule": "S125", "label": "código comentado", "count": 2})
        self.assertIn("código comentado 2", summary["text"])

    def test_issues_become_findings(self):
        from poda.core.analyzers.sonar import Sonar, import_issues
        url = f"http://127.0.0.1:{self.server.server_port}"
        sonar = Sonar({"SONAR_HOST_URL": url, "SONAR_TOKEN": "squ_test"}, self.tmp / "work")
        sonar.wait("task-1", timeout=10)
        issues = sonar.issues("poda-legacy")
        self.assertEqual(len(issues), 4)
        self.assertTrue(all(a and a.startswith("Basic ") for a in self.seen_auth))
        findings = Findings()
        stats = import_issues(issues, Inventory(self.project), findings)
        self.assertEqual(stats, {"removable": 3, "reported": 1, "ignored": 0})
        self.assertEqual(sorted(f.category for f in findings),
                         ["commented_code", "sonar_other", "unused_import", "unused_member"])
        member = next(f for f in findings if f.category == "unused_member")
        self.assertEqual(member.symbol, "neverUsed")
        self.assertFalse(member.needs_llm)


class SonarManyIssues(unittest.TestCase):
    """More than 10,000 issues: SonarQube caps every search, so they are fetched by batches of files."""

    def test_issues_beyond_the_limit_come_by_batches_of_files(self):
        files = ["poda-big:src/A.java", "poda-big:src/B.java", "poda-big:src/C.java"]
        totals = {"poda-big": 25_000, ",".join(files): 12_000, files[0]: 9_000, ",".join(files[1:]): 3_000}
        queries = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(handler):
                url = urllib.parse.urlsplit(handler.path)
                q = dict(urllib.parse.parse_qsl(url.query))
                if url.path == "/api/components/tree":
                    body = {"components": [{"key": k} for k in files], "paging": {"total": len(files)}}
                else:
                    comps = q["componentKeys"]
                    queries.append(comps)
                    page = int(q.get("p", "1"))
                    issues = [{"rule": "java:S125", "component": c} for c in comps.split(",")] if page == 1 else []
                    body = {"issues": issues, "paging": {"total": totals[comps]}}
                data = json.dumps(body).encode()
                handler.send_response(200)
                handler.end_headers()
                handler.wfile.write(data)

            def log_message(handler, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        from poda.core.analyzers.sonar import Sonar
        with tempfile.TemporaryDirectory() as work:
            sonar = Sonar({"SONAR_HOST_URL": f"http://127.0.0.1:{server.server_port}", "SONAR_TOKEN": "squ_test"},
                          Path(work))
            issues = sonar.issues("poda-big")
        self.assertEqual(sorted(i["component"] for i in issues), sorted(files))   # every file, once
        self.assertIn(",".join(files), queries)            # batch of files...
        self.assertIn(files[0], queries)                   # ...split in two because it was still over the limit
        self.assertIn(",".join(files[1:]), queries)


# ---------------------------------------------------------------------------
# LLM engine and agent (scripted transports returning real LiteLLM responses)
# ---------------------------------------------------------------------------

async def _mock(content=None, tool_calls=None, tools=None):
    import litellm
    kwargs = {"model": "gpt-4o", "messages": [{"role": "user", "content": "x"}], "_skip_mcp_handler": True}
    if tool_calls:
        return await litellm.acompletion(**kwargs, tools=tools or TOOL_SPECS, mock_tool_calls=tool_calls)
    return await litellm.acompletion(**kwargs, mock_response=content)


def _tool_call(name, args, number=1):
    return {"id": f"call_{number}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


OPENAI = LLMConfig("openai", "openai/gpt-4o", "openai/gpt-4o", display="openai · gpt-4o")


class EngineAndAgent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="poda-engine-"))
        build_fixture(cls.tmp / "legacy")
        cls.inv = Inventory(cls.tmp / "legacy")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def engine(self, transport, cfg=OPENAI, **kwargs):
        return LLMEngine(cfg, MetricsTracker(), transport=transport, **kwargs)

    def test_skip_mcp_handler_allows_tool_mocks(self):
        """Regression: LiteLLM 1.106 imports the optional `mcp` package for any call with tools."""
        response = asyncio.run(_mock(tool_calls=[_tool_call("grep", {"pattern": "Helper"})]))
        self.assertEqual(response.choices[0].message.tool_calls[0].function.name, "grep")

    def test_judge_repairs_an_answer_without_json(self):
        answers = ["He revisado todo y está bien.", '{"verdicts": [{"id": "FIL-0001", "decision": "remove", '
                                                   '"confidence": "high", "reason": "sin uso"}]}']

        async def transport(**kwargs):
            return await _mock(content=answers.pop(0))

        engine = self.engine(transport)
        messages = [{"role": "user", "content": "<!-- poda-task: verify-snippet -->"}]
        result = asyncio.run(engine.judge(messages, VerdictBatch, phase="verify", label="t", mode="snippet"))
        self.assertEqual(result.verdicts[0].decision, "remove")
        self.assertEqual(engine.metrics.summary()["calls"], 2)
        self.assertEqual(engine.metrics.calls[1].mode, "repair")

    def test_resume_cache_skips_the_call(self):
        cache = self.tmp / "cache.json"
        calls = []

        async def transport(**kwargs):
            calls.append(kwargs)
            return await _mock(content='{"stack_summary": "Java"}')

        messages = [{"role": "user", "content": "contexto"}]
        first = asyncio.run(self.engine(transport, cache_path=cache).judge(messages, ProjectContext, phase="context",
                                                                           label="c", mode="snippet"))
        engine = self.engine(transport, cache_path=cache, resume=True)
        again = asyncio.run(engine.judge(messages, ProjectContext, phase="context", label="c", mode="snippet"))
        self.assertEqual(first, again)
        self.assertEqual(len(calls), 1)
        self.assertEqual(engine.metrics.reused_results, 1)

    def test_fatal_error_stops_every_call_and_hides_the_key(self):
        import litellm

        async def transport(**kwargs):
            raise litellm.AuthenticationError(message="Incorrect API key provided: test-key-123",
                                              llm_provider="openai", model="gpt-4o")

        cfg = LLMConfig("openai", "openai/gpt-4o", "openai/gpt-4o", api_key="test-key-123")
        engine = self.engine(transport, cfg=cfg)
        with self.assertRaises(LLMFatalError):
            asyncio.run(engine.complete([{"role": "user", "content": "hola"}], phase="p", label="l", mode="m"))
        self.assertIsNotNone(engine.fatal)
        self.assertNotIn("test-key-123", engine.fatal)
        with self.assertRaises(LLMFatalError):
            asyncio.run(engine.complete([{"role": "user", "content": "otra"}], phase="p", label="l", mode="m"))
        self.assertEqual(engine.metrics.summary()["failed_calls"], 1)

    def test_agent_uses_tools_then_answers(self):
        seen = []

        async def transport(**kwargs):
            if not any(m.get("role") == "tool" for m in kwargs["messages"]):
                return await _mock(tool_calls=[_tool_call("grep", {"pattern": "OldHelper"}),
                                               _tool_call("read_file", {"path": "docs/notes.md"}, 2)])
            seen.extend(m["content"] for m in kwargs["messages"] if m.get("role") == "tool")
            return await _mock(content='```json\n{"verdicts": [{"id": "FIL-0001", "decision": "keep", '
                                       '"confidence": "high", "reason": "citado"}]}\n```')

        events = []
        tools = ReadOnlyTools(self.inv)
        result = asyncio.run(run_agent(self.engine(transport), tools, system="s", task="t", schema=VerdictBatch,
                                       phase="verify", label="agente", emit=lambda kind, msg="", **d: events.append((kind, msg))))
        self.assertEqual(result.verdicts[0].decision, "keep")
        self.assertTrue(any("docs/notes.md:1" in text for text in seen))                  # grep result
        self.assertTrue(any("OldHelper was replaced" in text for text in seen))           # read_file result
        self.assertIn(("tool_call", '🔎 busca "OldHelper"'), events)
        self.assertIn(("tool_call", "📖 lee docs/notes.md"), events)

    def test_agent_finalizes_when_the_turn_budget_runs_out(self):
        choices = []

        async def transport(**kwargs):
            choices.append(kwargs.get("tool_choice"))
            if kwargs.get("tool_choice") == "none":
                return await _mock(content='{"verdicts": []}')
            return await _mock(tool_calls=[_tool_call("list_dir", {}, len(choices))])

        result = asyncio.run(run_agent(self.engine(transport), ReadOnlyTools(self.inv), system="s", task="t",
                                       schema=VerdictBatch, phase="verify", label="a", max_turns=3))
        self.assertEqual(result.verdicts, [])
        self.assertEqual(choices, ["auto", "auto", "auto", "none"])

    def test_copilot_cost_is_estimated_from_github_rates(self):
        async def transport(**kwargs):
            return await _mock(content="OK")

        def run(model):
            cfg = LLMConfig("copilot", f"anthropic/{model}", f"anthropic/{model}")
            engine = self.engine(transport, cfg=cfg)
            reply = asyncio.run(engine.complete([{"role": "user", "content": "hola"}], phase="p", label="l", mode="m"))
            metrics = build_metrics(engine.metrics, project="p", llm={"provider": "copilot", "model": cfg.model},
                                    started=0, finished=1, loc_before={"totals": {}, "by_language": {}},
                                    loc_after=None, stats=_Stats(), findings=[], dry_run=True)
            return reply, metrics

        reply, metrics = run("claude-sonnet-4.6")                          # 3 $ in / 15 $ out per M tokens
        expected = round((reply.prompt_tokens * 3.0 + reply.completion_tokens * 15.0) / 1e6, 8)
        self.assertEqual((reply.cost_source, reply.cost_usd), ("copilot_estimate", expected))
        self.assertAlmostEqual(metrics["cost"]["total_usd"], expected, places=6)
        self.assertIn("Estimación con las tarifas por token de GitHub Copilot", metrics["cost"]["note"])
        reply, metrics = run("modelo-sin-tarifa")                          # GitHub lists no rate for it
        self.assertEqual((reply.cost_source, reply.cost_usd), ("copilot_subscription", None))
        self.assertIsNone(metrics["cost"]["total_usd"])
        self.assertIn("no publica tarifa", metrics["cost"]["note"])

    def test_copilot_rates(self):
        from datetime import date
        from poda.providers import copilot_pricing as pricing
        self.assertIs(pricing.lookup("anthropic/claude-sonnet-4-6"), pricing.lookup("claude-sonnet-4.6"))
        self.assertIsNone(pricing.lookup("gpt-4o"))
        # cached input is cheaper: 1M fresh (2 $) + 1M cached (0,10 $) + 1M out (10 $) on Claude Sonnet 5.5
        self.assertAlmostEqual(pricing.estimate("claude-sonnet-5.5", 2_000_000, 1_000_000, 1_000_000), 12.10)
        # long context: above 272K input tokens GPT-5.6 Terra costs 4 $ / 18 $ instead of 2 $ / 12 $
        self.assertAlmostEqual(pricing.estimate("gpt-5.6-terra", 100_000, 0), 0.2)
        self.assertAlmostEqual(pricing.estimate("gpt-5.6-terra", 300_000, 0), 1.2)
        # promotional price that ends on a known date
        self.assertAlmostEqual(pricing.estimate("gemini-4-carbon", 1_000_000, 0, today=date(2026, 11, 30)), 2.0)
        self.assertAlmostEqual(pricing.estimate("gemini-4-carbon", 1_000_000, 0, today=date(2026, 12, 1)), 4.0)

    def test_price_table_overrides_the_provider(self):
        async def transport(**kwargs):
            return await _mock(content="OK")

        engine = self.engine(transport, price_in=3.0, price_out=15.0)
        reply = asyncio.run(engine.complete([{"role": "user", "content": "hola"}], phase="p", label="l", mode="m"))
        expected = round(reply.prompt_tokens / 1e6 * 3.0 + reply.completion_tokens / 1e6 * 15.0, 8)
        self.assertEqual((reply.cost_usd, reply.cost_source), (expected, "price_table"))

    def test_extract_json_objects(self):
        text = 'Antes {"a": 1} y luego ```json\n{"b": [1, 2]}\n``` fin'
        self.assertIn({"b": [1, 2]}, extract_json_objects(text))
        self.assertIn({"a": 1}, extract_json_objects(text))


class _Stats:
    files_deleted = dirs_deleted = files_modified = lines_removed = bytes_before = bytes_after = 0
    reverted = []


# ---------------------------------------------------------------------------
# Static layer
# ---------------------------------------------------------------------------

class StaticLayer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="poda-static-"))
        cls.project = cls.tmp / "legacy"
        build_fixture(cls.project)
        cls.inv = Inventory(cls.project)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_ast_members_java(self):
        self.assertTrue(ast_members.available("java"))
        members = ast_members.extract(self.inv, self.inv.by_rel["src/com/acme/web/MainServlet.java"])
        by_name = {m.name: m for m in members}
        self.assertTrue(by_name["neverUsed"].private)
        self.assertFalse(by_name["doGet"].private)
        lines = MAIN_SERVLET.splitlines()
        self.assertEqual(lines[by_name["neverUsed"].start_line - 1].strip(), "private void neverUsed() {")
        self.assertEqual(lines[by_name["neverUsed"].end_line - 1].strip(), "}")

    def test_unreachable_constant_false_branch(self):
        findings = Findings()
        ast_members.detect_unreachable(self.inv, findings)
        unreachable = [f for f in findings if f.category == "unreachable_code"]
        self.assertEqual(len(unreachable), 1)
        f = unreachable[0]
        lines = MAIN_SERVLET.splitlines()
        start, end = f.edit["ranges"][0]
        self.assertEqual(lines[start - 1].strip(), "if (false) {")
        self.assertEqual(lines[end - 1].strip(), "}")
        self.assertFalse(f.needs_llm)

    def test_validators_revert_broken_files(self):
        good = b"class A {\n  void a() { }\n}\n"
        self.assertTrue(validate("A.java", good, b"class A {\n}\n")[0])
        ok, why = validate("A.java", good, b"class A {\n  void a() {\n}\n")
        self.assertFalse(ok)
        self.assertTrue(why)
        self.assertFalse(validate("a.xml", b"<a><b/></a>", b"<a><b/>")[0])
        self.assertFalse(validate("a.py", b"def f():\n    return 1\n", b"def f():\n")[0])
        self.assertTrue(validate("a.json", b'{"a": 1}', b'{}')[0])

    def test_tools_are_read_only_and_sandboxed(self):
        tools = ReadOnlyTools(self.inv)
        for bad in ("../secret.txt", "C:/Windows/win.ini", "/etc/passwd", "src/../../x"):
            self.assertTrue(tools.run("read_file", {"path": bad}).startswith("Error"), bad)
        self.assertIn("Error", tools.run("delete_file", {"path": "pom.xml"}))
        self.assertIn("Error", tools.run("grep", {"patron": "x"}))
        self.assertIn("Sin coincidencias", tools.run("grep", {"pattern": "OldHelper("}))  # bad regex: literal search
        self.assertIn("docs/notes.md:1", tools.run("grep", {"pattern": "oldhelper"}))
        read = tools.run("read_file", {"path": "src/com/acme/util/Legacy1252.java", "start_line": 3, "end_line": 3})
        self.assertIn("año, versión", read)                                           # cp1252 decoded
        self.assertIn("WEB-INF/", tools.run("list_dir", {"path": "WebContent"}))
        tools.flagged.add("src/com/acme/util/OldHelper.java")
        self.assertIn("[propuesto para eliminar]", tools.run("glob", {"pattern": "**/OldHelper.java"}))

    def test_evidence_pack_tags_documentation(self):
        finding = Finding(category="unused_file", subtype="java_class", path="src/com/acme/util/OldHelper.java",
                          reason="sin referencias", id="FIL-0001")
        pack = EvidenceBuilder(self.inv, ReadOnlyTools(self.inv)).pack(finding)
        self.assertEqual(pack["occurrences"]["searched"], ["OldHelper"])
        self.assertTrue(any(line.startswith("docs/notes.md:1:") and "[documentación]" in line
                            for line in pack["occurrences"]["shown"]))
        self.assertIn("public class OldHelper", pack["file_head"])

    def test_loc_stats(self):
        stats = loc_stats(self.inv)
        self.assertGreater(stats["totals"]["code"], 0)
        self.assertIn("java", stats["by_language"])
        java = stats["by_language"]["java"]
        self.assertLess(java["code"], java["physical"])                    # blanks and comments excluded
        self.assertNotIn("md", stats["by_language"])                       # documentation is not code...
        doc = stats["not_counted"]["documentation"]
        self.assertGreaterEqual(doc["files"], 1)                           # ...but it is reported apart
        self.assertIn("documentación (.md, .txt, .adoc…): ", not_counted_text(stats))

    def test_split_lines_keeps_cp1252_bytes(self):
        data = "a\r\nb\x85c\nd".encode("latin-1")
        self.assertEqual(split_lines_b(data), [b"a\r\n", b"b\x85c\n", b"d"])

    def test_comment_scoring(self):
        self.assertEqual(score_block(['String old = Helper.legacy();', 'Helper.print(old);'], "c", "java"), "high")
        self.assertIsNone(score_block(["Calcula el total de la factura aplicando el descuento."], "c", "java"))
        self.assertEqual(score_block(['<servlet-name>old</servlet-name>'], "xml", "xml"), "high")
        self.assertEqual(line_score("Dim rs As ADODB.Recordset", "vb", "bas")[0], 1)
        self.assertEqual(line_score("MOVE WS-A TO WS-B.", "cobol", "cbl")[0], 1)

    def test_classify_block_comment_boundaries(self):
        cl = classify_lines(split_lines("int a; /* start\n old();\n end */ int b;\n"), SYNTAXES["c"])
        self.assertEqual([li.kind for li in cl.lines], ["mixed", "comment", "mixed"])

    def test_class_refs(self):
        name, refs = class_refs(class_bytes("a/b/C", ["x/y/Z"]))
        self.assertEqual(name, "a/b/C")
        self.assertIn("x/y/Z", refs)


# ---------------------------------------------------------------------------
# Configuration, decision policy and prompts
# ---------------------------------------------------------------------------

class ConfigAndPolicy(unittest.TestCase):
    def test_litellm_gateway(self):
        llm = resolve_llm({"LLM_PROVIDER": "litellm", "LITELLM_BASE_URL": "https://gw.internal/v1/chat/completions",
                           "LITELLM_API_KEY": "test-key", "LITELLM_MODEL": "claude-sonnet",
                           "PODA_TRIAGE_MODEL": "claude-haiku"})
        self.assertEqual((llm.model, llm.triage_model, llm.api_base),
                         ("litellm_proxy/claude-sonnet", "litellm_proxy/claude-haiku", "https://gw.internal"))
        self.assertEqual(llm.call_kwargs(), {"api_base": "https://gw.internal", "api_key": "test-key"})

    def test_cli_overrides_env(self):
        llm = resolve_llm({"LLM_PROVIDER": "litellm", "LITELLM_BASE_URL": "https://gw.internal",
                           "LITELLM_API_KEY": "test-key", "LITELLM_MODEL": "a"}, "anthropic", "claude-sonnet-4-5")
        self.assertEqual((llm.provider, llm.model), ("anthropic", "anthropic/claude-sonnet-4-5"))

    def test_sample_placeholders_count_as_empty(self):
        # A .env copied from .env.sample does not block anything: placeholders are ignored.
        with self.assertRaises(ConfigError) as ctx:
            resolve_llm({"LLM_PROVIDER": "litellm", "LITELLM_BASE_URL": "https://litellm.example.com",
                         "LITELLM_API_KEY": "k", "LITELLM_MODEL": "m"})
        self.assertIn("URL", str(ctx.exception))
        with self.assertRaises(ConfigError) as ctx:
            resolve_llm({"LLM_PROVIDER": "litellm", "LITELLM_BASE_URL": "https://gw.test",
                         "LITELLM_API_KEY": "sk-REEMPLAZAR-CON-TU-CLAVE", "LITELLM_MODEL": "m"})
        self.assertIn("API key", str(ctx.exception))

    def test_url_and_api_key_are_required_for_litellm(self):
        with self.assertRaises(ConfigError) as ctx:
            resolve_llm({"LITELLM_API_KEY": "test-key"}, None, "claude-sonnet")    # no gateway URL
        self.assertIn("URL", str(ctx.exception))
        cfg = {"LITELLM_API_KEY": "test-key", "LITELLM_BASE_URL": "https://gw.test"}
        llm = resolve_llm(cfg, None, "claude-sonnet")
        self.assertEqual((llm.provider, llm.api_base), ("litellm", "https://gw.test"))
        with self.assertRaises(ConfigError) as ctx:
            resolve_llm(cfg)                                               # no model yet
        self.assertIn("poda dashboard", str(ctx.exception))

    def test_nothing_configured_points_to_the_dashboard(self):
        with tempfile.TemporaryDirectory() as home:
            with self.assertRaises(ConfigError) as ctx:
                resolve_llm({"COPILOT_HOME": home})
            self.assertIn("poda dashboard", str(ctx.exception))
            (Path(home) / "github-token").write_text("gho_x", encoding="utf-8")
            self.assertEqual(resolve_llm({"COPILOT_HOME": home, "COPILOT_MODEL": "gpt-4o"},
                                         start_proxy=False).provider, "copilot")

    def test_provider_from_model_prefix(self):
        llm = resolve_llm({}, None, "openai/gpt-4o")
        self.assertEqual((llm.provider, llm.model), ("openai", "openai/gpt-4o"))

    def test_bedrock(self):
        llm = resolve_llm({"LLM_PROVIDER": "bedrock", "ANTHROPIC_MODEL": "eu.anthropic.claude-sonnet",
                           "AWS_REGION": "eu-central-1"})
        self.assertEqual(llm.model, "bedrock/eu.anthropic.claude-sonnet")
        self.assertEqual(llm.extra, {"aws_region_name": "eu-central-1"})

    def test_copilot_needs_login(self):
        with tempfile.TemporaryDirectory() as home:
            with self.assertRaises(ConfigError) as ctx:
                resolve_llm({"LLM_PROVIDER": "copilot", "COPILOT_MODEL": "claude-sonnet-4", "COPILOT_HOME": home})
            self.assertIn("poda login copilot", str(ctx.exception))

    def test_read_dotenv(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_bytes(b'# comment\r\nexport A=1\r\nB="dos # tres"\r\nC=cuatro # nota\r\nBAD LINE\r\n')
            self.assertEqual(read_dotenv(path), {"A": "1", "B": "dos # tres", "C": "cuatro"})

    def test_decision_policy(self):
        findings = Findings()
        keep = findings.add(Finding("unused_file", "x", "a/A.java", "r", edit={"op": "delete_path"},
                                    llm={"decision": "keep", "confidence": "high", "reason": "usado"}))
        medium = findings.add(Finding("unused_file", "x", "a/B.java", "r", edit={"op": "delete_path"},
                                      llm={"decision": "remove", "confidence": "medium", "reason": "quizá"}))
        sure = findings.add(Finding("unused_file", "x", "a/C.java", "r", edit={"op": "delete_path"},
                                    llm={"decision": "remove", "confidence": "high", "reason": "seguro"}))
        guarded = findings.add(Finding("unused_file", "x", "web/p/D.jsp", "r", edit={"op": "delete_path"},
                                       llm={"decision": "remove", "confidence": "high", "reason": "seguro"}))
        unverified = findings.add(Finding("unused_member", "x", "a/E.java", "r", edit={"op": "delete_lines"}))
        safe = findings.add(Finding("unused_import", "x", "a/F.java", "r", confidence="high", needs_llm=False,
                                    edit={"op": "delete_lines"}))
        decide(findings, "high", True, [{"pattern": "./web/p/*", "why": "enlazada desde fuera"}])
        self.assertEqual([f.status for f in (keep, medium, guarded, unverified)], ["kept", "review", "kept", "review"])
        self.assertEqual([sure.decision, safe.decision], ["remove", "remove"])
        self.assertIn("la IA no dio veredicto", unverified.status_detail)
        loose = Finding("unused_file", "x", "a/G.java", "r", edit={"op": "delete_path"},
                        llm={"decision": "remove", "confidence": "medium"})
        decide([loose], "medium", True)                                  # --min-confidence medium
        self.assertEqual(loose.decision, "remove")

    def test_default_output_is_apart_from_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / "target" / "billing"
            app.mkdir(parents=True)
            with mock.patch.dict(os.environ, {"PODA_OUTPUT_DIR": str(root / "output")}):
                self.assertEqual(default_output_dir(app), (root / "output" / "billing").resolve())
                _, out_dir, work_dir = Pipeline(RunOptions(project=app), EventBus()).prepare_output()
            self.assertEqual(out_dir, (root / "output" / "billing").resolve())       # output/<app>
            self.assertTrue(work_dir.is_dir())
            self.assertEqual([p.name for p in (root / "target").iterdir()], ["billing"])  # target/ untouched

    def test_protected_patterns(self):
        rules = [{"pattern": ".htaccess", "why": "servidor"}, {"pattern": "./WebContent/protected/*", "why": "fuera"}]
        self.assertIsNotNone(protected_by(".htaccess", rules))
        self.assertIsNone(protected_by("htaccess", rules))
        self.assertIsNotNone(protected_by("WebContent/protected/kept.jsp", rules))

    def test_render_is_single_pass(self):
        text = render("repair", {"ERROR": "el código tenía {{NAME}} de Mustache", "SCHEMA": "{}"})
        self.assertIn("{{NAME}} de Mustache", text)
        with self.assertRaises(RuntimeError):
            render("repair", {"ERROR": "falta SCHEMA"})

    def test_every_prompt_is_english_and_tagged(self):
        for name in ("project_context", "verify_snippet", "verify_agent", "discover", "finalize", "repair"):
            text = (ROOT / "poda/prompts" / f"{name}.md").read_text(encoding="utf-8")
            self.assertTrue(text.startswith("<!-- poda-task: "), name)
            self.assertNotRegex(text, r"\b(el|los|las|para|eliminar)\b", name)


# ---------------------------------------------------------------------------
# Terminal output
# ---------------------------------------------------------------------------

class TerminalUI(unittest.TestCase):
    def events(self):
        return [
            Event("phase_start", "", {"index": 4, "total": 7, "title": "Verificación (IA)"}),
            Event("progress", "", {"phase": "verify", "done": 1, "total": 3}),
            Event("tool_call", '🔎 busca "OldHelper"', {"label": "agente 1/1"}),
            Event("verdicts", "", {"label": "snippet 1/1", "remove": 3, "keep": 1, "unsure": 1, "escalate": 1}),
            Event("removed", "src/OldHelper.java — sin referencias", {"files": 1}),
            Event("usage", "", {"prompt_tokens": 1200, "completion_tokens": 300, "cost": 0.0123}),
            Event("phase_end", "", {"index": 4, "title": "Verificación (IA)", "seconds": 2.0, "summary": "3 eliminar"}),
        ]

    def test_rich_dashboard(self):
        from rich.console import Console
        stream = io.StringIO()
        console = Console(file=stream, force_terminal=True, width=140, color_system=None)
        ui = RichUI(console)
        ui.start()
        for event in self.events():
            ui(event)
        console.print(ui.render())
        ui.stop()
        output = stream.getvalue()
        for text in ("Poda AI", "Verificación (IA)", "🗑", "veredictos: 3 eliminar", "tokens 1.200/300"):
            self.assertIn(text, output)
        self.assertEqual(ui.counters["removed_files"], 1)

    def test_plain_output_and_log_file(self):
        from rich.console import Console
        stream, log = io.StringIO(), io.StringIO()
        bus = EventBus()
        bus.subscribe(PlainUI(Console(file=stream, no_color=True, width=200)))
        bus.subscribe(FileLog(log))
        for event in self.events():
            bus.emit(event.kind, event.message, **event.data)
        for text in ("▶ 4/7 · Verificación (IA)", '[agente 1/1] 🔎 busca "OldHelper"', "1 pasan al agente"):
            self.assertIn(text, stream.getvalue())
        self.assertIn("🗑  src/OldHelper.java", log.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
