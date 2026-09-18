"""Unit tests for security.py - API key hashing, CSRF tokens, rate limiter."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from security import (RateLimiter, generate_api_key, hash_api_key,
                      key_suffix, verify_api_key)


class ApiKeyHashingTests(unittest.TestCase):
    def test_generated_key_is_reasonably_high_entropy(self):
        keys = {generate_api_key() for _ in range(50)}
        self.assertEqual(len(keys), 50)  # no collisions in 50 draws

    def test_correct_key_verifies(self):
        key = generate_api_key()
        stored_hash = hash_api_key(key)
        self.assertTrue(verify_api_key(key, stored_hash))

    def test_wrong_key_does_not_verify(self):
        stored_hash = hash_api_key(generate_api_key())
        self.assertFalse(verify_api_key("totally-wrong-key", stored_hash))

    def test_hash_never_equals_plaintext(self):
        key = generate_api_key()
        self.assertNotEqual(hash_api_key(key), key)

    def test_hash_is_deterministic(self):
        key = "fixed-test-key-value"
        self.assertEqual(hash_api_key(key), hash_api_key(key))

    def test_empty_or_none_key_never_verifies(self):
        stored_hash = hash_api_key(generate_api_key())
        self.assertFalse(verify_api_key("", stored_hash))
        self.assertFalse(verify_api_key(None, stored_hash))
        self.assertFalse(verify_api_key("something", ""))
        self.assertFalse(verify_api_key("something", None))

    def test_key_suffix_matches_the_real_ending(self):
        key = "abcdEFGH1234WXYZ"
        self.assertEqual(key_suffix(key), "WXYZ")
        self.assertEqual(key_suffix(key, length=6), "34WXYZ")


class RateLimiterTests(unittest.TestCase):
    def test_allows_up_to_the_limit(self):
        rl = RateLimiter(max_events=3, window_seconds=60)
        self.assertTrue(rl.allow("k"))
        self.assertTrue(rl.allow("k"))
        self.assertTrue(rl.allow("k"))

    def test_blocks_beyond_the_limit(self):
        rl = RateLimiter(max_events=2, window_seconds=60)
        self.assertTrue(rl.allow("k"))
        self.assertTrue(rl.allow("k"))
        self.assertFalse(rl.allow("k"))

    def test_keys_are_independent(self):
        rl = RateLimiter(max_events=1, window_seconds=60)
        self.assertTrue(rl.allow("a"))
        self.assertTrue(rl.allow("b"))  # different key, own budget
        self.assertFalse(rl.allow("a"))

    def test_window_expiry_allows_again(self):
        rl = RateLimiter(max_events=1, window_seconds=0.05)
        self.assertTrue(rl.allow("k"))
        self.assertFalse(rl.allow("k"))
        import time
        time.sleep(0.1)
        self.assertTrue(rl.allow("k"))


if __name__ == "__main__":
    unittest.main()
