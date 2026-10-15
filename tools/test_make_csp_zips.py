"""Tests for the per-CSP zip builder. Uses made-up keys only."""
import json
import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import make_csp_zips as m

KEY_A = "A" * 43
KEY_B = "b1_-" * 10 + "xyz"


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def _csv(self, text):
        p = os.path.join(self.tmp, "list.csv")
        open(p, "w", encoding="utf-8").write(text)
        return p

    def test_each_zip_holds_only_its_own_credentials_and_the_server_address(self):
        p = self._csv(f"csp_id,api_key,name\n1A850001,{KEY_A},One\n1A850002,{KEY_B},Two\n")
        out = os.path.join(self.tmp, "out")
        self.assertEqual(m.main(["--csv", p, "--out", out, "--server-url", "https://h.example:8443/"]), 0)
        for csp, key, other in (("1A850001", KEY_A, KEY_B), ("1A850002", KEY_B, KEY_A)):
            z = zipfile.ZipFile(os.path.join(out, f"CSP-{csp}-agent-setup.zip"))
            cfg = json.loads(z.read(f"csp-device-monitor-agent-{csp}/agent_config.json"))
            self.assertEqual(cfg, {"csp_id": csp, "api_key": key, "server_url": "https://h.example:8443/"})
            blob = b"".join(z.read(n) for n in z.namelist())
            self.assertNotIn(other.encode(), blob)             # never another CSP's key

    def test_the_zip_has_what_a_csp_needs_and_nothing_else(self):
        out = os.path.join(self.tmp, "o")
        os.makedirs(out)
        z = zipfile.ZipFile(m.build_zip("1A850001", KEY_A, "http://x", out))
        names = {n.split("/", 1)[1] for n in z.namelist()}
        for needed in ("agent.py", "SETUP.bat", "requirements.txt", "agent_config.json", "deep_checks.py",
                       "device_health.py", "local_ui.py", "reporter.py", "DIAGNOSE.bat"):
            self.assertIn(needed, names)
        self.assertTrue(any(n.startswith("wheels/") for n in names))     # offline packages
        self.assertFalse(any("tests" in n or "__pycache__" in n or n.endswith(".log") for n in names))

    def test_the_agent_reads_the_prefilled_config(self):
        out = os.path.join(self.tmp, "o")
        os.makedirs(out)
        z = zipfile.ZipFile(m.build_zip("1A850001", KEY_A, "https://h:8443/", out))
        cfg_path = os.path.join(self.tmp, "agent_config.json")
        open(cfg_path, "wb").write(z.read("csp-device-monitor-agent-1A850001/agent_config.json"))
        os.environ["CSP_AGENT_CONFIG_PATH"] = cfg_path
        sys.path.insert(0, m.AGENT_DIR)
        import config_store
        cfg = config_store.load()
        self.assertEqual((cfg["csp_id"], cfg["api_key"], cfg["server_url"]), ("1A850001", KEY_A, "https://h:8443/"))
        self.assertEqual(cfg["interval_seconds"], 300)                  # defaults still apply

    def test_stray_spaces_and_quotes_in_a_copied_key_are_removed(self):
        rows, problems = m.read_rows(self._csv(f'csp_id,api_key\n 1A850001 ," {KEY_A} "\n'))
        self.assertEqual(rows, [("1A850001", KEY_A, "")])
        self.assertEqual(problems, [])

    def test_bad_lines_are_reported_not_built(self):
        rows, problems = m.read_rows(self._csv(
            f"1A850001,tooshort\n1A850002,\n1A850003\n1A850004,{KEY_A}\n1A850004,{KEY_A}\nbad id!,{KEY_A}\n"))
        self.assertEqual([r[0] for r in rows], ["1A850004"])
        self.assertEqual(len(problems), 5)

    def test_the_example_file_is_never_built_as_a_real_csp(self):
        example = os.path.join(m.HERE, "csp_list.example.csv")
        rows, problems = m.read_rows(example)
        self.assertEqual(rows, [])                      # the placeholder key is rejected

    def test_the_tool_never_prints_a_key(self):
        import io, contextlib
        p = self._csv(f"1A850001,{KEY_A}\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            m.main(["--csv", p, "--out", os.path.join(self.tmp, "o2")])
        self.assertNotIn(KEY_A, buf.getvalue())


if __name__ == "__main__":
    unittest.main()
