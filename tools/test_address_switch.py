"""End-to-end tests for the server-address switch: the REAL server code runs on
a local port, a REAL copy of the agent is installed in a temp folder, and the
REAL switch tool runs as a subprocess against them. Made-up keys only."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER_DIR = os.path.join(ROOT, "server")
AGENT_DIR = os.path.join(ROOT, "agent")
sys.path.insert(0, HERE)
import make_csp_zips as maker
import make_address_switch_zip as switch_zip

DEAD = "http://127.0.0.1:9/"           # nothing listens here


class _RealServer:
    """The real Flask app on an ephemeral localhost port."""
    def __init__(self):
        sys.path.insert(0, SERVER_DIR)
        cwd = os.getcwd()
        os.chdir(SERVER_DIR)
        try:
            from tests.helpers import make_test_app, issue_test_key
        finally:
            os.chdir(cwd)
        self.app, self.db_path = make_test_app()
        self.issue = issue_test_key
        from werkzeug.serving import make_server
        self.httpd = make_server("127.0.0.1", 0, self.app, threaded=True)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()

    def csp_row(self, csp_id):
        import db
        with db.get_connection() as conn:
            row = conn.execute("SELECT csp_id, last_seen FROM csps WHERE csp_id=?", (csp_id,)).fetchone()
        return dict(row) if row else None


class SwitchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _RealServer()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.agent = os.path.join(self.tmp, "agent")
        os.makedirs(self.agent)
        for name in maker.AGENT_FILES:
            shutil.copy(os.path.join(AGENT_DIR, name), self.agent)
        for name in ("change_address.py", "CHANGE_SERVER_ADDRESS.bat"):
            shutil.copy(os.path.join(HERE, "address_switch", name), self.agent)

    def _install(self, csp_id, key, server_url, **extra):
        cfg = {"csp_id": csp_id, "api_key": key, "server_url": server_url, "biometric_name": "Keep Me"}
        cfg.update(extra)
        with open(os.path.join(self.agent, "agent_config.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)

    def _cfg_text(self):
        with open(os.path.join(self.agent, "agent_config.json"), encoding="utf-8") as f:
            return f.read()

    def _run(self, new_url=None, old_url=None):
        if new_url is not None:
            open(os.path.join(self.agent, "new_address.txt"), "w").write(new_url + "\n")
        if old_url is not None:
            open(os.path.join(self.agent, "old_address.txt"), "w").write(old_url + "\n")
        p = subprocess.run([sys.executable, "change_address.py"], cwd=self.agent,
                           capture_output=True, text=True, timeout=180)
        return p.returncode, p.stdout

    def test_switch_succeeds_end_to_end_and_keeps_everything_else(self):
        key = self.server.issue("SW1", "Switch One")
        self._install("SW1", key, DEAD)
        code, out = self._run(self.server.url)
        self.assertEqual(code, 0, out)
        self.assertIn("SUCCESS", out)
        cfg = json.loads(self._cfg_text())
        self.assertEqual(cfg["server_url"], self.server.url)
        self.assertEqual((cfg["csp_id"], cfg["api_key"], cfg["biometric_name"]), ("SW1", key, "Keep Me"))
        self.assertIsNotNone(self.server.csp_row("SW1"))                  # the new server really got a report
        self.assertTrue([n for n in os.listdir(self.agent) if n.startswith("agent_config.json.bak-")])
        self.assertNotIn(key, out)                                         # never prints the key

    def test_an_unreachable_new_address_changes_nothing(self):
        key = self.server.issue("SW2", "Switch Two")
        self._install("SW2", key, DEAD)
        before = self._cfg_text()
        code, out = self._run("http://127.0.0.1:8/")        # a different, dead address
        self.assertEqual(code, 1)
        self.assertIn("NOTHING WAS CHANGED", out)
        self.assertEqual(self._cfg_text(), before)

    def test_a_server_that_does_not_know_the_key_gets_reverted(self):
        self._install("SW3", "Z" * 43, DEAD)                               # key the server never issued
        before = self._cfg_text()
        code, out = self._run(self.server.url)
        self.assertEqual(code, 1, out)
        self.assertIn("OLD address has been put back", out)
        self.assertIn("401", out)
        self.assertEqual(self._cfg_text(), before)                         # byte-identical
        self.assertIsNone(self.server.csp_row("SW3"))

    def test_old_address_guard_leaves_other_agents_alone(self):
        key = self.server.issue("SW4", "Switch Four")
        self._install("SW4", key, "http://some-other-server:1234/")
        before = self._cfg_text()
        code, out = self._run(self.server.url, old_url="http://the-old-address:8443/")
        self.assertEqual(code, 2)
        self.assertEqual(self._cfg_text(), before)

    def test_already_on_the_new_address_is_a_no_op(self):
        key = self.server.issue("SW5", "Switch Five")
        self._install("SW5", key, self.server.url)
        code, out = self._run(self.server.url)
        self.assertEqual(code, 2)
        self.assertIn("Already using", out)

    def test_an_agent_that_was_never_set_up_is_not_touched(self):
        code, out = self._run(self.server.url)          # no agent_config.json at all
        self.assertEqual(code, 1)
        self.assertIn("never set up", out)

    def test_a_missing_or_invalid_new_address_is_rejected(self):
        key = self.server.issue("SW6", "Switch Six")
        self._install("SW6", key, DEAD)
        before = self._cfg_text()
        code, out = self._run("not-a-url")
        self.assertEqual(code, 1)
        self.assertEqual(self._cfg_text(), before)

    def test_a_result_file_is_left_for_support(self):
        key = self.server.issue("SW7", "Switch Seven")
        self._install("SW7", key, DEAD)
        self._run(self.server.url)
        with open(os.path.join(self.agent, "address-change-result.txt"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("SUCCESS", text)
        self.assertNotIn(key, text)


class ZipBuilderTests(unittest.TestCase):
    def test_the_switch_zip_contains_the_address_and_no_credentials(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        path = switch_zip.build("https://new.example:8443", "https://old.example:8443/", tmp)
        z = zipfile.ZipFile(path)
        names = {n.split("/", 1)[1] for n in z.namelist()}
        self.assertEqual(names, {"change_address.py", "CHANGE_SERVER_ADDRESS.bat", "new_address.txt",
                                 "old_address.txt", "READ_ME.txt"})
        self.assertEqual(z.read("switch-server-address/new_address.txt").decode().strip(), "https://new.example:8443/")
        blob = b"".join(z.read(n) for n in z.namelist()).lower()
        self.assertNotIn(b"api_key\": \"", blob)
        self.assertNotIn(b"agent_config.json\"", z.read("switch-server-address/new_address.txt"))

    def test_an_invalid_url_is_refused(self):
        with self.assertRaises(ValueError):
            switch_zip.build("ftp://x", "", tempfile.mkdtemp())


if __name__ == "__main__":
    unittest.main()
