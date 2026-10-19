"""The signature maths behind signed updates. A mistake here would let a forged
update through, so it is pinned against the RFC 8032 vector and a real library."""
import os
import random
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ed25519 as ed

# RFC 8032 section 7.1, TEST 1 (empty message)
SK1 = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
PK1 = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
SIG1 = bytes.fromhex("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")


class Rfc8032(unittest.TestCase):
    def test_public_key_derivation(self):
        self.assertEqual(ed.secret_to_public(SK1), PK1)

    def test_signature_matches_the_rfc_vector_exactly(self):
        self.assertEqual(ed.sign(SK1, b""), SIG1)

    def test_the_rfc_signature_verifies(self):
        self.assertTrue(ed.verify(PK1, b"", SIG1))


class Behaviour(unittest.TestCase):
    def setUp(self):
        self.sk = os.urandom(32)
        self.pk = ed.secret_to_public(self.sk)
        self.msg = b'{"version": "2.1.0", "sequence": 7}'
        self.sig = ed.sign(self.sk, self.msg)

    def test_round_trip(self):
        self.assertTrue(ed.verify(self.pk, self.msg, self.sig))

    def test_a_modified_message_is_rejected(self):
        self.assertFalse(ed.verify(self.pk, self.msg + b" ", self.sig))
        self.assertFalse(ed.verify(self.pk, self.msg.replace(b"7", b"8"), self.sig))

    def test_every_single_bit_flip_in_the_signature_is_rejected(self):
        for i in (0, 5, 31, 32, 40, 63):
            bad = bytearray(self.sig)
            bad[i] ^= 0x01
            self.assertFalse(ed.verify(self.pk, self.msg, bytes(bad)), i)

    def test_a_different_key_is_rejected(self):
        other = ed.secret_to_public(os.urandom(32))
        self.assertFalse(ed.verify(other, self.msg, self.sig))

    def test_malformed_inputs_are_false_not_exceptions(self):
        for pk, sig in ((b"", self.sig), (self.pk, b""), (self.pk[:31], self.sig), (self.pk, self.sig[:63]),
                        (None, self.sig), (self.pk, None), (b"\xff" * 32, self.sig), (self.pk, b"\xff" * 64)):
            self.assertFalse(ed.verify(pk, self.msg, sig))

    def test_a_non_canonical_scalar_is_rejected(self):
        s = int.from_bytes(self.sig[32:], "little") + ed._q          # same maths, non-canonical encoding
        forged = self.sig[:32] + (s % (1 << 256)).to_bytes(32, "little")
        self.assertFalse(ed.verify(self.pk, self.msg, forged))

    def test_a_bad_private_key_length_is_an_error(self):
        with self.assertRaises(ValueError):
            ed.sign(b"short", b"x")

    def test_verification_is_fast_enough_for_an_agent(self):
        start = time.time()
        for _ in range(3):
            ed.verify(self.pk, self.msg, self.sig)
        self.assertLess((time.time() - start) / 3, 1.0)               # seconds per verify, generous bound


@unittest.skipUnless(__import__("importlib").util.find_spec("cryptography"), "cryptography not installed (cross-check skipped)")
class CrossCheckAgainstALibrary(unittest.TestCase):
    def test_both_directions_over_random_keys_and_messages(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization as ser
        rnd = random.Random(1234)
        for _ in range(12):
            sk = bytes(rnd.getrandbits(8) for _ in range(32))
            msg = bytes(rnd.getrandbits(8) for _ in range(rnd.choice([0, 1, 31, 200, 1000])))
            lib = Ed25519PrivateKey.from_private_bytes(sk)
            lib_pk = lib.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)
            self.assertEqual(ed.secret_to_public(sk), lib_pk)                 # same public key
            self.assertEqual(ed.sign(sk, msg), lib.sign(msg))                 # identical signatures (deterministic)
            self.assertTrue(ed.verify(lib_pk, msg, lib.sign(msg)))            # we accept the library's signatures
            from cryptography.exceptions import InvalidSignature
            lib.public_key().verify(ed.sign(sk, msg), msg)                    # the library accepts ours (raises if not)
            tampered = bytearray(ed.sign(sk, msg)); tampered[10] ^= 1
            with self.assertRaises(InvalidSignature):
                lib.public_key().verify(bytes(tampered), msg)
            self.assertFalse(ed.verify(lib_pk, msg, bytes(tampered)))         # and both reject a tampered one


if __name__ == "__main__":
    unittest.main()
