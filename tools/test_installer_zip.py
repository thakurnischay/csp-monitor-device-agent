import json, os, sys, tempfile, unittest, zipfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import make_installer_zip as m


class Zip(unittest.TestCase):
    def test_contents_have_no_secrets_and_a_valid_config(self):
        out = m.build("https://monitor.example.com", tempfile.mkdtemp())
        with zipfile.ZipFile(out) as z:
            names = z.namelist()
            base = {os.path.basename(n) for n in names}
            self.assertFalse(base & m.FORBIDDEN)
            self.assertIn("CSP-Device-Monitor-Setup/SETUP.bat", names)
            cfg = json.loads(z.read("CSP-Device-Monitor-Setup/installer_config.json"))
            self.assertEqual(cfg["server_url"], "https://monitor.example.com")
            self.assertNotIn("api_key", cfg)
            pk = json.loads(z.read("CSP-Device-Monitor-Setup/release_public_keys.json"))
            self.assertIn("keys", pk)
            self.assertIsNone(z.testzip())

    def test_bad_url_is_refused(self):
        with self.assertRaises(SystemExit):
            m.build("monitor.example.com", tempfile.mkdtemp())



class Imports(unittest.TestCase):
    """Every local module the agent imports must ship in BOTH kinds of zip (a missing one = agent crash on a CSP PC)."""
    def local_modules(self, files):
        import re
        agent = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent")
        have = {os.path.splitext(os.path.basename(f))[0] for f in files if f.endswith(".py")}
        need = set()
        for f in files:
            if f.endswith(".py") and "/" not in f:
                src = open(os.path.join(agent, f), encoding="utf-8").read()
                for mod in re.findall(r"^\s*(?:import|from)\s+([a-z_0-9]+)", src, re.M):
                    if os.path.isfile(os.path.join(agent, mod + ".py")):
                        need.add(mod)
        return need - have

    def test_installer_zip_is_complete(self):
        self.assertEqual(self.local_modules(m.FILES), set())

    def test_legacy_per_csp_zip_is_complete(self):
        import make_csp_zips
        self.assertEqual(self.local_modules(make_csp_zips.AGENT_FILES), set())


if __name__ == "__main__":
    unittest.main()
