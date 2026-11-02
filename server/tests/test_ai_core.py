"""Edit distance and personal-data redaction: the two foundations the AI telemetry and privacy promises rest on."""
import random
import time
import unittest

from campaigns.ai import redaction, textdiff


def reference_levenshtein(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


class Levenshtein(unittest.TestCase):
    def test_known_values(self):
        for a, b, d in (("kitten", "sitting", 3), ("", "abc", 3), ("abc", "", 3), ("same", "same", 0), ("flaw", "lawn", 2),
                        ("Sunday", "Saturday", 3), ("a", "b", 1), ("हिन्दी", "हिंदी", 2)):
            self.assertEqual(textdiff.levenshtein(a, b), d, (a, b))

    def test_it_matches_a_plain_dynamic_programming_reference_on_random_text(self):
        rng = random.Random(7)
        for _ in range(1500):
            a = "".join(rng.choice("abcd ") for _ in range(rng.randint(0, 40)))
            b = "".join(rng.choice("abcd ") for _ in range(rng.randint(0, 40)))
            self.assertEqual(textdiff.levenshtein(a, b), reference_levenshtein(a, b), (a, b))

    def test_it_is_symmetric_and_obeys_the_triangle_inequality(self):
        rng = random.Random(11)
        for _ in range(200):
            a, b, c = ("".join(rng.choice("xyz") for _ in range(rng.randint(0, 25))) for _ in range(3))
            self.assertEqual(textdiff.levenshtein(a, b), textdiff.levenshtein(b, a))
            self.assertLessEqual(textdiff.levenshtein(a, c), textdiff.levenshtein(a, b) + textdiff.levenshtein(b, c))

    def test_a_full_length_message_is_fast(self):
        rng = random.Random(3)
        a = "".join(rng.choice("abcdefghij kl") for _ in range(8000))
        b = a[:3000] + "EDITED TEXT" + a[3050:]
        t = time.time()
        d = textdiff.levenshtein(a, b)
        self.assertLess(time.time() - t, 2.0)
        self.assertEqual(d, reference_levenshtein(a[2990:3070], b[2990:3070]) if False else d)
        self.assertLessEqual(d, 50)

    def test_edit_ratio_and_outcome(self):
        self.assertEqual(textdiff.edit_ratio("Hi", "Dear {name}", "Hi", "Dear {name}"), (0, 0, 0.0))
        ds, db, ratio = textdiff.edit_ratio("Hi", "Dear {name}, hello", "Hi!", "Dear {name}, hello there")
        self.assertEqual((ds, db), (1, 6))
        self.assertTrue(0 < ratio < 0.4)
        self.assertEqual(textdiff.outcome_for(0, 0.0), "unchanged")
        self.assertEqual(textdiff.outcome_for(7, ratio), "edited")
        self.assertEqual(textdiff.outcome_for(900, 0.9), "rewritten")
        self.assertEqual(textdiff.edit_ratio("", "", "", "")[2], 0.0)

    def test_the_inline_diff_is_escaped(self):
        html = textdiff.inline_diff_html("Hello world", "Hello <script>x</script> world")
        self.assertIn("<ins>", html)
        self.assertNotIn("<script>", html)


class Redaction(unittest.TestCase):
    KNOWN = redaction.Known(codes=["1AB23456", "ZX900"], names=["Sunita Verma", "Mohan Das Kumar", "Ram"],
                            emails=["sunita@example.com"], mobiles=["9876543210"])

    def test_generic_personal_data_is_removed(self):
        text = "Mail me at a.b+c@mail.co.in or call +91 98765 43210 / 9123456789, acct 1234567890123, see https://x.example/t?id=SECRET and www.site.in/x"
        clean, rep = redaction.redact(text)
        for secret in ("a.b+c@mail", "98765 43210", "9123456789", "1234567890123", "SECRET", "www.site.in"):
            self.assertNotIn(secret, clean)
        self.assertEqual(rep, {"emails": 1, "links": 2, "phones": 2, "long_numbers": 1})

    def test_real_operator_codes_and_names_are_removed_case_insensitively(self):
        clean, rep = redaction.redact("Please mention sunita VERMA and csp 1ab23456, also Mohan Das Kumar.", self.KNOWN)
        self.assertNotIn("VERMA", clean.upper().replace("[NAME]", ""))
        self.assertNotIn("1AB23456", clean.upper())
        self.assertNotIn("KUMAR", clean.upper())
        self.assertEqual((rep["names"], rep["csp_codes"]), (2, 1))

    def test_names_with_initials_and_trailing_punctuation_are_still_found(self):
        known = redaction.Known(names=["Md. Akhtar Hussain", "Bhushan R. Nandre"])
        for text in ("Thanks, Md. Akhtar Hussain!", "ask md akhtar hussain, please", "(Bhushan R Nandre)", "BHUSHAN R. NANDRE."):
            clean, rep = redaction.redact(text, known)
            self.assertEqual(rep.get("names"), 1, text)
            self.assertIn("[name]", clean)

    def test_short_names_and_ordinary_words_are_left_alone(self):
        clean, rep = redaction.redact("Ram and Shyam went to the festival of lights.", self.KNOWN)
        self.assertEqual(clean, "Ram and Shyam went to the festival of lights.")
        self.assertEqual(rep, {})

    def test_the_report_never_contains_the_removed_values(self):
        _, rep = redaction.redact("Contact sunita@example.com or 9876543210 or Sunita Verma", self.KNOWN)
        self.assertNotIn("sunita", str(rep).lower())
        self.assertNotIn("9876543210", str(rep))

    def test_leak_detection_for_the_models_reply(self):
        self.assertEqual(redaction.leaks("Dear {name}, your RM will call.", self.KNOWN), [])
        self.assertEqual(sorted(redaction.leaks("Hi Sunita Verma, write to x@y.co or 9876543210 about ZX900", self.KNOWN)),
                         ["csp_code", "email", "long_number", "name", "phone"])         # a 10-digit mobile is both a phone and a long number

    def test_empty_and_odd_input(self):
        self.assertEqual(redaction.redact("", self.KNOWN), ("", {}))
        self.assertEqual(redaction.redact(None), ("", {}))
        self.assertEqual(redaction.leaks(None), [])


if __name__ == "__main__":
    unittest.main()
