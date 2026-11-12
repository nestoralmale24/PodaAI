"""Simulated SonarQube for the tests: a local Web API server and a fake sonar-scanner.

The server behaves like a fresh SonarQube: account admin/admin, which Poda AI
must change before it can create a token. It serves the analysis status and a
configurable list of issues.

    python fake_sonar.py scanner -Dsonar.working.directory=... ...   (fake scanner)
"""
import base64
import http.server
import json
import sys
import threading
import urllib.parse
from pathlib import Path


class FakeSonar:
    def __init__(self, issues=None, status="UP"):
        self.issues = issues or []
        self.status = status
        self.password = "admin"
        self.tokens = set()
        self.calls = []
        self.profiles = {}          # language -> key of the Poda AI profile
        self.bound = []             # (project, language) analysed with it
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _auth(handler):
                header = handler.headers.get("Authorization") or ""
                if not header.startswith("Basic "):
                    return None, None
                user, _, secret = base64.b64decode(header[6:]).decode().partition(":")
                return user, secret

            def _valid(handler):
                user, secret = handler._auth()
                return (user in fake.tokens and secret == "") or (user == "admin" and secret == fake.password)

            def _reply(handler, code, body=None):
                data = json.dumps(body).encode() if body is not None else b""
                handler.send_response(code)
                handler.send_header("Content-Type", "application/json")
                handler.end_headers()
                handler.wfile.write(data)

            def do_GET(handler):
                url = urllib.parse.urlsplit(handler.path)
                query = dict(urllib.parse.parse_qsl(url.query))
                fake.calls.append(("GET", url.path))
                if url.path == "/api/system/status":
                    return handler._reply(200, {"status": fake.status, "version": "25.1.0"})
                if url.path == "/api/authentication/validate":
                    return handler._reply(200, {"valid": handler._valid()})
                if not handler._valid():
                    return handler._reply(401, {"errors": [{"msg": "unauthorized"}]})
                if url.path == "/api/rules/search":
                    if query.get("qprofile"):
                        return handler._reply(200, {"total": 3, "rules": []})
                    return handler._reply(200, {"total": 3, "facets": [
                        {"property": "languages", "values": [{"val": "java", "count": 3}]}]})
                if url.path == "/api/qualityprofiles/search":
                    lang = query.get("language")
                    found = [{"key": fake.profiles[lang], "name": "Poda AI (código sin uso)"}] if lang in fake.profiles else []
                    return handler._reply(200, {"profiles": found})
                if url.path == "/api/ce/task":
                    return handler._reply(200, {"task": {"status": "SUCCESS"}})
                if url.path == "/api/issues/search":
                    page = int(query.get("p", "1"))
                    chunk = fake.issues if page == 1 else []
                    return handler._reply(200, {"issues": chunk, "paging": {"total": len(fake.issues)}})
                handler._reply(404, {})

            def do_POST(handler):
                url = urllib.parse.urlsplit(handler.path)
                query = dict(urllib.parse.parse_qsl(url.query))
                fake.calls.append(("POST", url.path))
                if url.path == "/api/users/change_password":
                    if query.get("previousPassword") != fake.password or len(query.get("password", "")) < 12:
                        return handler._reply(400, {"errors": [{"msg": "bad password"}]})
                    fake.password = query["password"]
                    return handler._reply(204)
                if not handler._valid():
                    return handler._reply(401, {"errors": [{"msg": "unauthorized"}]})
                if url.path == "/api/qualityprofiles/create":
                    fake.profiles[query["language"]] = "pk-" + query["language"]
                    return handler._reply(200, {"profile": {"key": fake.profiles[query["language"]]}})
                if url.path == "/api/qualityprofiles/activate_rules":
                    return handler._reply(200, {"succeeded": 3, "failed": 0})
                if url.path == "/api/projects/create":
                    return handler._reply(200, {"project": {"key": query.get("project")}})
                if url.path == "/api/qualityprofiles/add_project":
                    fake.bound.append((query.get("project"), query.get("language")))
                    return handler._reply(204)
                if url.path == "/api/user_tokens/generate":
                    token = f"squ_fake_{len(fake.tokens) + 1:04d}"
                    fake.tokens.add(token)
                    return handler._reply(200, {"token": token, "name": query.get("name")})
                handler._reply(404, {})

            def log_message(handler, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def scanner_command():
    """SONAR_SCANNER value that runs the fake scanner below."""
    return f'"{Path(sys.executable).as_posix()}" "{Path(__file__).resolve().as_posix()}" scanner'


def _scanner(args):
    props = dict(a[2:].split("=", 1) for a in args if a.startswith("-D") and "=" in a)
    work = Path(props["sonar.working.directory"])
    work.mkdir(parents=True, exist_ok=True)
    (work / "scanner-args.json").write_text(json.dumps(args), encoding="utf-8")
    print(f"INFO: Project key: {props.get('sonar.projectKey')}")
    print("INFO: Analysis report uploaded")
    (work / "report-task.txt").write_text("projectKey={}\nceTaskId=task-1\n".format(props.get("sonar.projectKey")),
                                          encoding="utf-8")
    return 0


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "scanner":
    sys.exit(_scanner(sys.argv[2:]))
