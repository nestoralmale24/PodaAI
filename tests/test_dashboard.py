"""Tests for `poda dashboard`: providers, Copilot sign-in and model selection (no network).

The LiteLLM gateway and GitHub are simulated with local HTTP servers.
"""
import http.client
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

from poda import llm_settings  # noqa: E402
from poda.config import load_env  # noqa: E402
from poda.dashboard.server import serve  # noqa: E402
from fixtures import MAIN_SERVLET, build_fixture  # noqa: E402
from fake_sonar import FakeSonar, scanner_command  # noqa: E402
from poda import sonar_setup  # noqa: E402

GATEWAY_KEY = "test-gateway-key"
OAUTH_TOKEN = "gho_" + "a1B2c3D4e5" * 3


def fake_upstream(seen):
    """One server playing the LiteLLM gateway and GitHub."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def _reply(handler, status, body):
            data = json.dumps(body).encode()
            handler.send_response(status)
            handler.send_header("Content-Type", "application/json")
            handler.end_headers()
            handler.wfile.write(data)

        def do_GET(handler):
            seen.append(("GET", handler.path, handler.headers.get("Authorization")))
            if handler.path == "/v1/models":
                if handler.headers.get("Authorization") != f"Bearer {GATEWAY_KEY}":
                    return handler._reply(401, {"error": "bad key"})
                return handler._reply(200, {"data": [{"id": "claude-sonnet"}, {"id": "claude-haiku"},
                                                     {"id": "anthropic/*"}]})
            if handler.path == "/user":
                return handler._reply(200, {"login": "octo"})
            handler._reply(404, {})

        def do_POST(handler):
            length = int(handler.headers.get("Content-Length") or 0)
            body = json.loads(handler.rfile.read(length) or b"{}")
            seen.append(("POST", handler.path, body.get("client_id")))
            if handler.path == "/login/device/code":
                return handler._reply(200, {"device_code": "dev-123", "user_code": "ABCD-1234",
                                            "verification_uri": "https://github.com/login/device",
                                            "interval": 1, "expires_in": 60})
            if handler.path == "/login/oauth/access_token":
                return handler._reply(200, {"access_token": OAUTH_TOKEN, "token_type": "bearer"})
            handler._reply(404, {})

        def log_message(handler, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class Dashboard(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="poda-dash-"))
        (self.tmp / "empty.env").write_text("", encoding="utf-8")
        self.seen = []
        self.upstream = fake_upstream(self.seen)
        base = f"http://127.0.0.1:{self.upstream.server_port}"
        clean = {k: v for k, v in os.environ.items()
                 if not k.startswith(("LITELLM_", "COPILOT_", "PODA_", "LLM_"))}
        clean.update({"PODA_SETTINGS_DIR": str(self.tmp / "settings"), "PODA_ENV_FILE": str(self.tmp / "empty.env"),
                      "PODA_OUTPUT_DIR": str(self.tmp / "output"),
                      "COPILOT_HOME": str(self.tmp / "copilot"), "COPILOT_GITHUB_WEB": base,
                      "COPILOT_GITHUB_API": base, "LITELLM_LOCAL_MODEL_COST_MAP": "True"})
        self.env = mock.patch.dict(os.environ, clean, clear=True)
        self.env.start()
        self.gateway = base
        self.server = serve("127.0.0.1", 0, token="test-token")
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, method, path, body=None, token="test-token", host=None, raw=None, cookie=None, full=False):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if token:
            headers["X-Poda-Token"] = token
        if cookie:
            headers["Cookie"] = cookie
        data = None
        if body is not None or raw is not None:
            data = raw if raw is not None else json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        text = response.read().decode("utf-8", errors="replace")
        set_cookie = response.getheader("Set-Cookie")
        conn.close()
        try:
            payload = json.loads(text)
        except ValueError:
            payload = text
        return (response.status, payload, set_cookie) if full else (response.status, payload)

    def test_access_is_protected(self):
        self.assertTrue(self.server.url.endswith(f":{self.port}/"))           # no token in the address
        status, page, set_cookie = self.call("GET", "/", token=None, full=True)
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Strict", set_cookie)
        session = set_cookie.split(";", 1)[0]
        self.assertEqual(self.call("GET", "/api/llm/providers", token=None, cookie=session)[0], 200)
        self.assertEqual(self.call("GET", "/api/llm/providers", token=None, cookie="poda_session=wrong")[0], 401)
        self.assertNotIn("test-token", page)                                  # the page never embeds it
        self.assertIn("Proveedores de IA", page)
        self.assertIn("Modelo de IA", page)
        self.assertEqual(self.call("GET", "/api/llm/providers", token=None)[0], 401)
        self.assertEqual(self.call("GET", "/api/llm/providers", token="wrong")[0], 401)
        self.assertEqual(self.call("GET", "/api/llm/providers", host="evil.example:80")[0], 421)  # DNS rebinding
        self.assertEqual(self.call("POST", "/api/llm/litellm", raw=b"not json")[0], 400)
        # NA. brand assets are public (no secrets); anything else under /assets is not served
        status, logo = self.call("GET", "/assets/logo-na.svg", token=None)
        self.assertEqual(status, 200)
        self.assertIn("<svg", logo)
        self.assertIn("--na-blue:#2F6BDB", self.call("GET", "/assets/na.css", token=None)[1])
        for path in ("/assets/../server.py", "/assets/index.html", "/assets/nope.svg"):
            self.assertEqual(self.call("GET", path, token=None)[0], 404, path)
        self.assertIn('href="/assets/na.css"', page)
        self.assertIn('src="/assets/logo-na.svg" alt="NA."', page)
        self.assertIn('rel="icon" type="image/png" href="/assets/poda-favicon.png"', page)
        self.assertEqual(self.call("GET", "/assets/poda-logo.png", token=None)[0], 200)
        self.assertIn('id="themeToggle"', page)                              # dark mode switch
        self.assertIn('src="/assets/logo-na-dark.svg"', page)
        self.assertEqual(self.call("GET", "/assets/chevron-light.svg", token=None)[0], 200)

    def test_litellm_gateway_and_model_selection(self):
        status, data = self.call("POST", "/api/llm/litellm", {"base_url": "ftp://x", "api_key": "k"})
        self.assertEqual(status, 400)
        status, data = self.call("POST", "/api/llm/litellm", {"base_url": self.gateway + "/v1/", "api_key": GATEWAY_KEY})
        self.assertEqual(status, 200, data)
        lite = next(p for p in data["providers"] if p["provider"] == "litellm")
        self.assertTrue(lite["ready"])
        self.assertEqual((lite["base_url"], lite["key_source"]), (self.gateway, "dashboard"))
        settings = (self.tmp / "settings" / "providers.json").read_text(encoding="utf-8")
        self.assertIn(GATEWAY_KEY, settings)                                  # stored on the server...
        for path in ("/api/llm/providers", "/api/config/model"):
            self.assertNotIn(GATEWAY_KEY, json.dumps(self.call("GET", path)[1]))  # ...never sent to the browser

        status, models = self.call("GET", "/api/llm/providers/litellm/models?refresh=1")
        self.assertEqual([m["id"] for m in models["models"]], ["claude-haiku", "claude-sonnet"])  # no wildcard routes
        self.assertEqual(models["source"], "live")
        self.assertEqual(self.call("GET", "/api/llm/providers/litellm/models")[1]["source"], "cache")

        status, data = self.call("POST", "/api/config/model", {"provider": "litellm", "model": "gpt-9"})
        self.assertEqual(status, 400)                                          # not offered by the gateway
        status, data = self.call("POST", "/api/config/model", {"provider": "litellm", "model": "claude-sonnet",
                                                               "triage_model": "claude-haiku"})
        self.assertEqual(status, 200, data)
        effective = data["effective"]
        self.assertEqual((effective["model"], effective["triage_model"], effective["source"]),
                         ("litellm_proxy/claude-sonnet", "litellm_proxy/claude-haiku", "dashboard"))
        cfg = load_env()                                                       # what `poda run` reads
        self.assertEqual((cfg["LLM_PROVIDER"], cfg["LITELLM_MODEL"], cfg["LITELLM_BASE_URL"]),
                         ("litellm", "claude-sonnet", self.gateway))

        status, data = self.call("POST", "/api/config/model", {"clear": True})
        self.assertEqual(data["selection"]["provider"], "")
        status, data = self.call("POST", "/api/llm/litellm", {"clear": True})
        self.assertTrue(data["removed"])
        self.assertFalse(next(p for p in data["providers"] if p["provider"] == "litellm")["ready"])
        self.assertNotIn(GATEWAY_KEY, (self.tmp / "settings" / "providers.json").read_text(encoding="utf-8"))
        self.assertFalse((self.tmp / "settings" / "litellm-models.json").exists())

    def test_dashboard_values_win_over_dotenv(self):
        (self.tmp / "empty.env").write_text(f"LLM_PROVIDER=litellm\nLITELLM_BASE_URL=https://old.gateway\n"
                                            f"LITELLM_API_KEY={GATEWAY_KEY}\nLITELLM_MODEL=claude-haiku\n", encoding="utf-8")
        self.assertEqual(self.call("GET", "/api/config/model")[1]["effective"]["source"], "env")
        self.call("POST", "/api/llm/litellm", {"base_url": self.gateway, "api_key": ""})   # keeps the .env key
        self.call("GET", "/api/llm/providers/litellm/models?refresh=1")
        self.call("POST", "/api/config/model", {"provider": "litellm", "model": "claude-sonnet"})
        cfg = load_env()
        self.assertEqual((cfg["LITELLM_BASE_URL"], cfg["LITELLM_MODEL"], cfg["PODA_TRIAGE_MODEL"]),
                         (self.gateway, "claude-sonnet", "claude-sonnet"))
        self.assertEqual(load_env(dashboard=False)["LITELLM_MODEL"], "claude-haiku")

    def test_copilot_device_flow_token_and_disconnect(self):
        status, flow = self.call("POST", "/api/llm/copilot/login", {})
        self.assertEqual(status, 200, flow)
        self.assertEqual(flow["user_code"], "ABCD-1234")
        self.assertNotIn("dev-123", json.dumps(flow))                          # device code stays on the server
        time.sleep(1.1)
        status, result = self.call("POST", "/api/llm/copilot/login/poll", {"flow_id": flow["flow_id"]})
        self.assertEqual(result["status"], "authorized", result)
        self.assertIn("octo", result["message"])
        token_file = self.tmp / "copilot" / "github-token"
        self.assertEqual(token_file.read_text(encoding="utf-8").strip(), OAUTH_TOKEN)  # same file as the proxy
        copilot = next(p for p in self.call("GET", "/api/llm/providers")[1]["providers"] if p["provider"] == "copilot")
        self.assertTrue(copilot["ready"])
        self.assertEqual(copilot["account"], "octo")
        self.assertNotIn(OAUTH_TOKEN, json.dumps(self.call("GET", "/api/llm/providers")[1]))

        status, data = self.call("POST", "/api/llm/copilot/token", {"token": "github_pat_" + "x" * 30})
        self.assertEqual(status, 400)
        self.assertIn("Conectar con GitHub", data["error"])
        status, data = self.call("POST", "/api/llm/copilot/logout", {})
        self.assertTrue(data["removed"])
        self.assertFalse(token_file.exists())
        self.assertEqual(self.call("POST", "/api/llm/copilot/login/poll", {"flow_id": "nope"})[1]["status"], "unknown")

    def test_starts_without_dotenv_and_only_the_key_is_required(self):
        status, data = self.call("GET", "/api/llm/providers")
        self.assertEqual(status, 200)
        self.assertFalse(any(p["ready"] for p in data["providers"]))         # nothing configured yet
        lite = next(p for p in data["providers"] if p["provider"] == "litellm")
        self.assertEqual(lite["base_url"], "")
        self.assertIn("URL", lite["message"])
        status, data = self.call("POST", "/api/llm/litellm", {"base_url": "", "api_key": ""})
        self.assertEqual(status, 400)                                         # URL and key are mandatory
        status, data = self.call("POST", "/api/llm/litellm", {"base_url": "", "api_key": GATEWAY_KEY})
        self.assertEqual(status, 400)                                         # no URL: nothing to connect to
        status, data = self.call("POST", "/api/llm/litellm", {"base_url": self.gateway, "api_key": GATEWAY_KEY})
        self.assertEqual(status, 200, data)
        lite = next(p for p in data["providers"] if p["provider"] == "litellm")
        self.assertTrue(lite["ready"])
        effective = self.call("GET", "/api/config/model")[1]["effective"]
        self.assertFalse(effective["ok"])                                     # model still to choose
        self.assertIn("poda dashboard", effective["error"])

    def test_choose_a_target_app_and_follow_the_run_live(self):
        target = self.tmp / "target"
        build_fixture(target / "legacy")
        (target / "other-app").mkdir()
        (target / "other-app" / "A.java").write_text("class A {}\n", encoding="utf-8")
        os.environ["PODA_TARGET_DIR"] = str(target)

        projects = self.call("GET", "/api/projects")[1]
        self.assertEqual([p["name"] for p in projects["projects"]], ["legacy", "other-app"])
        status, data = self.call("POST", "/api/runs", {"project": "legacy", "options": {}})
        self.assertEqual(status, 400)                                         # a provider is mandatory
        self.assertIn("proveedor", data["error"])
        self.assertEqual(self.call("POST", "/api/runs", {"project": "../x", "options": {}})[0], 400)
        self.assertEqual(self.call("GET", "/api/projects/..%2Fx/files/run.log")[0], 400)

        os.environ.update({"LLM_PROVIDER": "openai", "PODA_MODEL": "gpt-4o",
                           "PODA_LLM_TRANSPORT": "fake_llm:transport", "FAKE_LLM_PROJECT": str(target / "legacy")})
        sonar_setup.forget_cache()
        status, data = self.call("POST", "/api/runs", {"project": "legacy", "options": {}})
        self.assertEqual(status, 400)                                         # SonarQube is mandatory too
        self.assertIn("SonarQube", data["error"])

        lines = MAIN_SERVLET.splitlines()
        comment = next(i for i, line in enumerate(lines, 1) if "Helper.legacy()" in line)
        component = "poda-legacy:src/com/acme/web/MainServlet.java"
        sonar = FakeSonar(issues=[
            {"rule": "java:S125", "component": component, "textRange": {"startLine": comment, "endLine": comment + 1},
             "message": "This block of commented-out lines of code should be removed."},
            {"rule": "java:S1481", "component": component, "line": 14, "textRange": {"startLine": 14, "endLine": 14},
             "message": "Remove this unused local variable."}])
        self.addCleanup(sonar.close)
        os.environ.update({"SONAR_HOST_URL": sonar.url, "SONAR_SCANNER": scanner_command()})
        sonar_setup.forget_cache()
        ready = self.call("GET", "/api/sonar/status")[1]["sonar"]
        self.assertEqual(ready["state"], "ready", ready)                      # prepared on its own
        self.assertNotEqual(sonar.password, "admin")
        stored = (self.tmp / "settings" / "providers.json").read_text(encoding="utf-8")
        self.assertIn(sonar.password, stored)
        self.assertNotIn(sonar.password, json.dumps(ready))                   # never sent to the browser
        status, data = self.call("POST", "/api/runs", {"project": "legacy", "options": {"no_discover": True}})
        self.assertEqual(status, 200, data)
        run_id, since, events = data["run"]["id"], 0, []
        deadline = time.time() + 240
        while time.time() < deadline:
            status, data = self.call("GET", f"/api/runs/{run_id}/events?since={since}")
            events += data["events"]
            since += len(data["events"])
            if data["run"]["status"] not in ("starting", "running"):
                break
            time.sleep(0.5)
        run = data["run"]
        self.assertEqual(run["status"], "done", [e["line"] for e in events if e["kind"] in ("error", "warn")])
        from poda.dashboard import runs
        self.assertEqual(runs.RUNS.get().pipeline.opts.max_candidates, 0)          # the AI validates every finding
        kinds = {e["kind"] for e in events}
        self.assertTrue({"phase_start", "phase_end", "tool_call", "verdicts", "removed", "done"} <= kinds)
        starts = [e["data"]["title"] for e in events if e["kind"] == "phase_start"]
        self.assertEqual(starts[:3], ["Inventario", "SonarQube", "Análisis estático"])   # SonarQube goes first
        self.assertTrue(any("🔬 SonarQube: 2 incidencias" in (e["line"] or "") for e in events))
        output = self.tmp / "output" / "legacy"                              # results go to output/<app>
        self.assertEqual(run["out_dir"], str(output.resolve()))
        findings = json.loads((output / "findings.json").read_text(encoding="utf-8"))["findings"]
        removed = [f for f in findings if f["status"] == "removed"]
        self.assertTrue(removed)
        self.assertTrue(all(f["llm"] and f["llm"]["decision"] == "remove" for f in removed))  # nothing without AI
        self.assertTrue(any(d.startswith("sonar:") for f in findings for d in f["detectors"]))
        self.assertIn(("poda-legacy", "java"), sonar.bound)                  # analysed with the Poda AI profile
        args = json.loads((output / ".poda" / "sonar" / "work" / "scanner-args.json").read_text())
        settings = next(a for a in args if a.startswith("-Dproject.settings="))
        self.assertTrue(settings.endswith("poda-empty.properties"))         # the app's own settings are ignored
        self.assertTrue(any("perfil «Poda AI (código sin uso)»" in (e["line"] or "") for e in events))
        self.assertTrue(any("🔎 busca" in (e["line"] or "") for e in events))   # what the agent reads/searches
        self.assertGreater(run["summary"]["files_deleted"], 0)
        report = self.call("GET", "/api/projects/legacy/files/poda_report.md")[1]
        self.assertIn("Limpieza de código muerto", report["content"])
        self.assertTrue((output / "legacy").is_dir())                         # the clean copy
        self.assertEqual(sorted(p.name for p in target.iterdir()), ["legacy", "other-app"])  # target/ untouched
        listed = {p["name"]: p for p in self.call("GET", "/api/projects")[1]["projects"]}
        self.assertEqual(sorted(listed), ["legacy", "other-app"])
        self.assertTrue(listed["legacy"]["has_output"])
        self.assertEqual(self.call("POST", "/api/runs/cancel", {})[0], 400)  # nothing running

    def test_removing_litellm_also_drops_the_dotenv_key_and_the_model(self):
        (self.tmp / "empty.env").write_text(f"LITELLM_API_KEY={GATEWAY_KEY}\nLITELLM_BASE_URL={self.gateway}\n",
                                            encoding="utf-8")
        self.call("GET", "/api/llm/providers/litellm/models?refresh=1")
        self.call("POST", "/api/config/model", {"provider": "litellm", "model": "claude-sonnet"})
        self.assertTrue(self.call("GET", "/api/config/model")[1]["effective"]["ok"])
        status, data = self.call("POST", "/api/llm/litellm", {"clear": True})
        lite = next(p for p in data["providers"] if p["provider"] == "litellm")
        self.assertEqual((lite["ready"], lite["removed"], lite["api_key_set"]), (False, True, False))   # .env ignored
        self.assertEqual(self.call("GET", "/api/config/model")[1]["selection"]["provider"], "")      # model dropped
        cfg = load_env()
        self.assertNotIn("LITELLM_API_KEY", cfg)                              # `poda run` no longer sees it
        self.assertEqual(self.call("POST", "/api/llm/litellm", {"base_url": "", "api_key": ""})[0], 400)
        self.call("POST", "/api/llm/litellm", {"base_url": self.gateway, "api_key": GATEWAY_KEY})   # back on demand
        self.assertTrue(next(p for p in self.call("GET", "/api/llm/providers")[1]["providers"]
                             if p["provider"] == "litellm")["ready"])

    def test_disconnecting_copilot_stops_the_proxy_and_deletes_the_session(self):
        home = self.tmp / "copilot"
        home.mkdir()
        (home / "github-token").write_text(OAUTH_TOKEN, encoding="utf-8")
        (home / "proxy-key").write_text("local-proxy-key", encoding="utf-8")
        llm_settings._update_section("selection", {"provider": "copilot", "model": "gpt-4o"})
        stopped = []

        class Proxy(http.server.BaseHTTPRequestHandler):
            def do_POST(handler):
                stopped.append((handler.path, handler.headers.get("x-api-key")))
                handler.send_response(200)
                handler.end_headers()

            def log_message(handler, *args):
                pass

        proxy = http.server.HTTPServer(("127.0.0.1", 0), Proxy)
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        os.environ["COPILOT_PROXY_PORT"] = str(proxy.server_port)
        try:
            status, data = self.call("POST", "/api/llm/copilot/logout", {})
        finally:
            proxy.shutdown()
            proxy.server_close()
        self.assertTrue(data["removed"])
        self.assertEqual(stopped, [("/shutdown", "local-proxy-key")])            # proxy told to exit
        self.assertFalse((home / "github-token").exists())
        self.assertFalse((home / "proxy-key").exists())
        self.assertEqual(llm_settings.selection()["provider"], "")

    def test_names_from_before_the_rename_still_work(self):
        # .env written for "deadcode": DEADCODE_* variables are read as PODA_*
        (self.tmp / "empty.env").write_text("DEADCODE_TRIAGE_MODEL=claude-haiku\n", encoding="utf-8")
        self.assertEqual(load_env()["PODA_TRIAGE_MODEL"], "claude-haiku")
        # previous outputs (<app>-deadcode/.deadcode) are not offered as apps
        target = self.tmp / "target"
        (target / "billing").mkdir(parents=True)
        (target / "billing-deadcode" / ".deadcode").mkdir(parents=True)
        os.environ["PODA_TARGET_DIR"] = str(target)
        self.assertEqual([p["name"] for p in self.call("GET", "/api/projects")[1]["projects"]], ["billing"])
        # settings saved in .deadcode-settings move to .poda-settings
        home = self.tmp / "home"
        (home / ".deadcode-settings").mkdir(parents=True)
        (home / ".deadcode-settings" / "providers.json").write_text('{"selection": {"provider": "litellm", "model": "m"}}',
                                                                     encoding="utf-8")
        os.environ.pop("PODA_SETTINGS_DIR", None)
        with mock.patch.object(llm_settings, "PROJECT_ROOT", home):
            self.assertEqual(llm_settings.selection()["model"], "m")
        self.assertTrue((home / ".poda-settings" / "providers.json").is_file())
        self.assertFalse((home / ".deadcode-settings").exists())

    def test_during_a_run_providers_are_locked_but_the_model_can_change(self):
        from poda.dashboard import runs
        runs.RUNS.current = runs.Run(id="run-x", project="legacy", options={}, status="running")
        self.addCleanup(setattr, runs.RUNS, "current", None)
        self.assertTrue(self.call("GET", "/api/llm/providers")[1]["locked"])
        for method, path, body in [("POST", "/api/llm/litellm", {"base_url": "", "api_key": GATEWAY_KEY}),
                                   ("POST", "/api/llm/litellm", {"clear": True}),
                                   ("POST", "/api/llm/copilot/login", {}),
                                   ("POST", "/api/llm/copilot/token", {"token": OAUTH_TOKEN}),
                                   ("POST", "/api/llm/copilot/logout", {}),
                                   ("GET", "/api/llm/providers/litellm/models?refresh=1", None)]:
            status, data = self.call(method, path, body)
            self.assertEqual(status, 409, path)
            self.assertIn("limpieza en marcha", data["error"])
        self.assertFalse((self.tmp / "settings" / "providers.json").exists())    # nothing was saved
        self.assertNotEqual(self.call("GET", "/api/llm/providers/litellm/models")[0], 409)  # cached list: fine
        status, data = self.call("POST", "/api/config/model", {"provider": "litellm", "model": "claude-sonnet"})
        self.assertEqual(status, 200, data)                                   # the model can change
        runs.RUNS.current.status = "done"
        self.assertEqual(self.call("POST", "/api/llm/litellm", {"clear": True})[0], 200)  # unlocked again

    def test_selection_requires_a_known_provider(self):
        with self.assertRaises(llm_settings.LLMSettingsError):
            llm_settings.save_selection("bedrock", "x")
        with self.assertRaises(llm_settings.LLMSettingsError):
            llm_settings.save_selection("litellm", "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
