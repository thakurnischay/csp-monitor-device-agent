"""Signed releases: building, signing, verifying, and every way a release can be forged or malformed."""
import base64
import copy
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "agent"))
import release_tool as rt            # noqa: E402
import release_manifest as rm        # noqa: E402
import ed25519                       # noqa: E402


def tmpdir(test):
    d = tempfile.mkdtemp()
    test.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
    return d


def sample_files(extra=None):
    f = {"agent.py": b"print('agent')\n", "version.py": b'AGENT_VERSION = "0.0.0"\nSCHEMA_VERSION = 4\n',
         "reporter.py": b"# reporter\n", "installer/wizard.py": b"# wizard\n"}
    f.update(extra or {})
    return f


class Keys:
    def __init__(self, test):
        self.dir = tmpdir(test)
        self.secret = os.urandom(32)
        self.public = ed25519.secret_to_public(self.secret)
        self.keys = {"keys": [{"id": "k1", "public": self.public.hex()}]}

    def build(self, files=None, version="2.1.0", seq=5, **kw):
        out = os.path.join(self.dir, "out%d" % seq)
        return rt.build_release(files or sample_files(), version, seq, "k1", self.secret, out_dir=out, **kw)

    def verify(self, zp, mp, sp, keys=None):
        return rt.verify_release(open(zp, "rb").read(), open(mp, "rb").read(), open(sp).read(), keys or self.keys)


class BuildAndVerify(unittest.TestCase):
    def test_a_built_release_verifies_and_describes_its_files(self):
        k = Keys(self)
        zp, mp, sp = k.build()
        m = k.verify(zp, mp, sp)
        self.assertEqual((m["version"], m["sequence"], m["kind"]), ("2.1.0", 5, "release"))
        zf = zipfile.ZipFile(zp)
        for f in m["files"]:
            self.assertEqual(hashlib.sha256(zf.read(f["path"])).hexdigest(), f["sha256"])
        self.assertEqual(sorted(zf.namelist()), sorted(f["path"] for f in m["files"]))

    def test_version_and_sequence_are_stamped_into_the_packaged_code(self):
        k = Keys(self)
        zp, _, _ = k.build(version="3.4.5", seq=9)
        src = zipfile.ZipFile(zp).read("version.py").decode()
        self.assertIn('AGENT_VERSION = "3.4.5"', src)
        self.assertIn("AGENT_SEQUENCE = 9", src)

    def test_modified_package_is_rejected(self):
        k = Keys(self)
        zp, mp, sp = k.build()
        data = bytearray(open(zp, "rb").read())
        data[len(data) // 2] ^= 0xFF
        open(zp, "wb").write(bytes(data))
        with self.assertRaises(rm.ManifestError) as cm:
            k.verify(zp, mp, sp)
        self.assertIn("hash", str(cm.exception))

    def test_modified_manifest_is_rejected_by_the_signature(self):
        k = Keys(self)
        zp, mp, sp = k.build()
        m = json.loads(open(mp, "rb").read())
        m["sequence"] = 99                                     # try to jump the version ladder
        open(mp, "wb").write(json.dumps(m, indent=2, sort_keys=True).encode())
        with self.assertRaises(rm.ManifestError) as cm:
            k.verify(zp, mp, sp)
        self.assertIn("signature", str(cm.exception))

    def test_signature_from_a_different_key_is_rejected(self):
        k, attacker = Keys(self), Keys(self)
        zp, mp, sp = attacker.build()
        with self.assertRaises(rm.ManifestError):
            k.verify(zp, mp, sp, keys={"keys": [{"id": "k1", "public": k.public.hex()}]})

    def test_unknown_key_id_and_garbage_signature_are_rejected(self):
        k = Keys(self)
        zp, mp, sp = k.build()
        with self.assertRaises(rm.ManifestError):
            k.verify(zp, mp, sp, keys={"keys": []})
        for junk in ("", "not base64!!", base64.b64encode(b"x" * 64).decode()):
            open(sp, "w").write(junk)
            with self.assertRaises(rm.ManifestError):
                k.verify(zp, mp, sp)

    def test_the_package_a_release_ships_never_contains_local_state_or_tests(self):
        files = rt.collect_agent_files()
        for path in files:
            self.assertNotIn("tests/", path)
            self.assertNotIn("wheels", path)
            self.assertNotIn(path.split("/")[-1], rm.PROTECTED_NAMES)
            self.assertTrue(rm.safe_relpath(path), path)
        self.assertIn("agent.py", files)

    def test_a_secret_key_never_ends_up_in_the_package_or_manifest(self):
        k = Keys(self)
        zp, mp, sp = k.build()
        blob = open(zp, "rb").read() + open(mp, "rb").read() + open(sp, "rb").read()
        self.assertNotIn(k.secret, blob)
        self.assertNotIn(k.secret.hex().encode(), blob)


class ManifestValidation(unittest.TestCase):
    def good(self):
        k = Keys(self)
        _, mp, _ = k.build()
        return json.loads(open(mp, "rb").read())

    def check(self, m):
        return rm.parse_manifest(json.dumps(m).encode())

    def bad(self, m, fragment=None):
        with self.assertRaises(rm.ManifestError) as cm:
            self.check(m)
        if fragment:
            self.assertIn(fragment, str(cm.exception))

    def test_good_manifest_passes(self):
        self.assertEqual(self.check(self.good())["version"], "2.1.0")

    def test_unsafe_and_disallowed_paths(self):
        for path in ("../evil.py", "/etc/x.py", "C:/x.py", "a\\b.py", "a/../b.py", "x.exe", "x.dll", "x.pyd", "x.whl",
                     "con.py", "dir/aux.txt", "a//b.py", " a.py", "a.py.", "", "a" * 300 + ".py", "x\x00.py"):
            m = self.good()
            m["files"].append({"path": path, "sha256": "0" * 64, "size": 1})
            self.bad(m)

    def test_protected_data_files_can_never_be_replaced(self):
        for name in ("agent_config.json", "diagnostic_history.json", "agent_state.json", "sub/agent_config.json"):
            m = self.good()
            m["files"].append({"path": name, "sha256": "0" * 64, "size": 1})
            self.bad(m, "own data file")

    def test_structure_errors(self):
        cases = {
            "product": lambda m: m.update(product="other"), "platform": lambda m: m.update(platform="linux"),
            "version": lambda m: m.update(version="latest"), "seq0": lambda m: m.update(sequence=0),
            "seqneg": lambda m: m.update(sequence=-3), "seqstr": lambda m: m.update(sequence="5"),
            "seqbool": lambda m: m.update(sequence=True), "kind": lambda m: m.update(kind="hotfix"),
            "hash": lambda m: m.update(package_sha256="xyz"), "size0": lambda m: m.update(package_size=0),
            "huge": lambda m: m.update(package_size=10 ** 10), "deps": lambda m: m.update(requires_new_dependencies=True),
            "noagent": lambda m: m.update(files=[f for f in m["files"] if f["path"] != "agent.py"]),
            "nofiles": lambda m: m.update(files=[]), "keyid": lambda m: m.update(key_id="bad id!"),
            "notes": lambda m: m.update(notes="x" * 5000),
            "dup": lambda m: m["files"].append(dict(m["files"][0])),
            "bigfile": lambda m: m["files"].append({"path": "big.txt", "sha256": "0" * 64, "size": 10 ** 9}),
        }
        for name, mutate in cases.items():
            m = copy.deepcopy(self.good())
            mutate(m)
            with self.assertRaises(rm.ManifestError, msg=name):
                self.check(m)

    def test_non_json_and_oversize_input(self):
        for raw in (b"", b"not json", b"[]", b'"x"', b"\xff\xfe", b"{" * 10, b"{}" + b" " * (300 * 1024), None):
            with self.assertRaises(rm.ManifestError):
                rm.parse_manifest(raw)

    def test_version_ordering_ignores_suffixes(self):
        self.assertEqual(rm.version_tuple("2.1.0-rollback"), (2, 1, 0))
        self.assertLess(rm.version_tuple("2.0.9"), rm.version_tuple("2.1.0"))
        self.assertEqual(rm.version_tuple("garbage"), (0,))


class SequenceAndRollback(unittest.TestCase):
    def test_sequence_always_rises_and_persists_across_runs(self):
        d = tmpdir(self)
        state = os.path.join(d, "state.json")
        seqs = []
        for _ in range(3):
            s = rt._next_sequence(state)
            rt._write_sequence(state, s)
            seqs.append(s)
        self.assertEqual(seqs, [1, 2, 3])
        self.assertEqual(rt._next_sequence(state, floor=50), 51)          # --min-sequence can only push it up

    def test_cli_build_then_rollback_issues_a_higher_sequence_with_old_code(self):
        k = Keys(self)
        d = tmpdir(self)
        priv = os.path.join(d, "priv.key")
        open(priv, "w").write(k.secret.hex())
        json.dump(k.keys, open(os.path.join(d, "release_public_keys.json"), "w"))
        agent = os.path.join(d, "agent")
        os.makedirs(agent)
        for name, data in sample_files().items():
            os.makedirs(os.path.dirname(os.path.join(agent, name)), exist_ok=True)
            open(os.path.join(agent, name), "wb").write(data)
        out1, out2 = os.path.join(d, "r1"), os.path.join(d, "r2")
        self.assertEqual(rt.main(["build", "--private-key", priv, "--version", "2.0.0", "--out", out1, "--agent-dir", agent]), 0)
        open(os.path.join(agent, "reporter.py"), "wb").write(b"# BROKEN NEW CODE\n")
        self.assertEqual(rt.main(["build", "--private-key", priv, "--version", "2.1.0", "--out", out2, "--agent-dir", agent]), 0)
        out3 = os.path.join(d, "r3")
        self.assertEqual(rt.main(["rollback", "--private-key", priv, "--version", "2.1.1", "--out", out3,
                                  "--base-package", os.path.join(out1, "csp-agent-2.0.0.zip"),
                                  "--base-manifest", os.path.join(out1, "csp-agent-2.0.0.manifest.json")]), 0)
        m = k.verify(*[os.path.join(out3, "csp-agent-2.1.1-rollback" + e) for e in (".zip", ".manifest.json", ".manifest.sig")])
        self.assertEqual((m["kind"], m["version"], m["sequence"]), ("rollback", "2.1.1", 3))      # higher than 2.1.0's 2
        self.assertEqual(zipfile.ZipFile(os.path.join(out3, "csp-agent-2.1.1-rollback.zip")).read("reporter.py"), b"# reporter\n")


class KeygenSafety(unittest.TestCase):
    def test_keygen_writes_a_usable_pair_and_refuses_to_overwrite(self):
        d = tmpdir(self)
        self.assertEqual(rt.main(["keygen", "--out", d, "--key-id", "k1"]), 0)
        secret = bytes.fromhex(open(os.path.join(d, rt.KEY_FILE)).read().strip())
        pub = json.load(open(os.path.join(d, rt.PUBKEY_FILE)))["keys"][0]
        self.assertEqual(pub["public"], ed25519.secret_to_public(secret).hex())
        self.assertEqual(rt.main(["keygen", "--out", d]), 2)                  # no overwrite

    def test_keygen_refuses_to_put_the_private_key_inside_a_git_repository(self):
        inside = os.path.join(ROOT, "tmp_keygen_should_not_exist")
        try:
            self.assertEqual(rt.main(["keygen", "--out", inside]), 2)
            self.assertFalse(os.path.exists(os.path.join(inside, rt.KEY_FILE)))
        finally:
            shutil.rmtree(inside, ignore_errors=True)


class SharedFilesStayIdentical(unittest.TestCase):
    def test_agent_and_server_copies_of_the_shared_modules_are_byte_identical(self):
        for name in ("ed25519.py", "release_manifest.py"):
            a = open(os.path.join(ROOT, "agent", name), "rb").read()
            s = open(os.path.join(ROOT, "server", name), "rb").read()
            self.assertEqual(hashlib.sha256(a).hexdigest(), hashlib.sha256(s).hexdigest(), name)


if __name__ == "__main__":
    unittest.main()
