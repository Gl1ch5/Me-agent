"""Local recording stand: mock LLM + the real server (server/app.py) on a free port + the promo-demo account.
Nothing here touches me-agent.ru; everything lives in the work dir and dies with the process."""
import json, os, socket, subprocess, sys, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "promo"))
import mock_promo  # noqa: E402

ACCOUNT = ("promo-demo", "promo-demo-pass")  # throwaway account on the local stand only


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Stand:
    def __init__(self, work):
        self.work = Path(work)
        self.work.mkdir(parents=True, exist_ok=True)
        self.llm = mock_promo.serve()
        self.llm_url = f"http://127.0.0.1:{self.llm.server_port}"
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        env = dict(os.environ, ME_DB=str(self.work / "promo.db"), ME_DATA=str(self.work / "data"), ME_SANDBOX="0",
                   ME_INTERNAL_URL=self.base, ME_PUBLIC_URL=self.base, ME_CLIENT=str(ROOT / "me.py"))
        self.log = open(self.work / "server.log", "w")
        self.proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--port", str(self.port)], cwd=ROOT / "server",
                                     env=env, stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
        for _ in range(200):
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise SystemExit("promo stand: server did not start (see " + str(self.work / "server.log") + ")")
        self._provision()
        self._publish_promo_theme()

    def _provision(self):
        """Log the promo account in and push a provider/model config that points at the mock (synced to the cloud sandbox)."""
        os.environ["ME_SERVER"] = self.base
        sys.path.insert(0, str(ROOT))
        import me  # noqa: E402
        s = me.Store(str(self.work / "cloud-home"))
        s.auth = {"server": self.base}
        c = me.Cloud(s)
        c.login(*ACCOUNT, register=True)
        s.config["providers"]["mock"] = {"base_url": self.llm_url + "/v1", "api_key": "promo-mock"}
        s.config["models"] = [{"name": "promo-mock", "id": "mock-1", "provider": "mock"}]
        s.config["model"] = "promo-mock"
        s.save()
        c.sync()

    def _publish_promo_theme(self):
        """The /theme scene installs a theme from the marketplace list, so the list needs one: the promo's own palette is
        published here, by the throwaway account, as any user's theme would be (server/app.py, POST /api/market)."""
        f = ROOT / "scripts" / "promo" / "theme-coffee.json"
        if not f.exists():
            return
        json.loads(f.read_text("utf-8"))  # a broken theme file must not take the whole stand down
        body = json.dumps({"ext": "json", "note": "promo stand", "content": f.read_text("utf-8")}).encode()
        req = urllib.request.Request(self.base + "/api/market", method="POST", data=body,
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.token_for_browser()})
        urllib.request.urlopen(req, timeout=10).read()

    def token_for_browser(self):
        """The session token the web app keeps in localStorage (same login the account page does)."""
        req = urllib.request.Request(self.base + "/api/login", method="POST",
                                     data=json.dumps({"username": ACCOUNT[0], "password": ACCOUNT[1]}).encode(),
                                     headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=10).read())["token"]

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()
        self.llm.shutdown()
