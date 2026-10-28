"""Nemotron through NVIDIA's OpenAI-compatible API. The network call is replaced by a fake; nothing real is contacted."""
import json
import os
import unittest
from unittest import mock

import db
from campaigns.ai import drafting, nvidia
from tests.test_ai import GOOD
from tests.test_ai_web import FORM
from tests.test_campaigns_web import WebCase

ENV = {"CAMPAIGN_AI_PROVIDER": "nvidia", "NVIDIA_API_KEY": "nvapi-test-not-real", "ANTHROPIC_API_KEY": "", "CAMPAIGN_AI_MODEL": "nvidia/test-nemotron"}


def reply(content, finish="stop", status=200, hdrs=None):
    body = {"id": "chat-1", "model": "nvidia/test-nemotron", "choices": [{"message": {"content": content}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 800, "completion_tokens": 120}}
    return status, hdrs or {"nvcf-reqid": "req-nv-1"}, json.dumps(body).encode()


class Fake:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, headers, body))
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r


class Unit(unittest.TestCase):
    def test_extracts_json_from_think_blocks_and_fences(self):
        wrapped = "<think>let me plan\n{not this}</think>\n```json\n" + json.dumps(GOOD) + "\n```"
        self.assertEqual(json.loads(nvidia.extract_json_text(wrapped)), GOOD)

    def test_success_is_shaped_like_the_anthropic_response(self):
        fake = Fake(reply(json.dumps(GOOD)))
        with mock.patch.object(nvidia, "_post", fake):
            r = nvidia.create("m", "sys", "user", "nvapi-x")
        self.assertEqual(json.loads(r.content[0].text), GOOD)
        self.assertEqual((r.usage.input_tokens, r.usage.output_tokens, r.stop_reason, r._request_id), (800, 120, "end_turn", "req-nv-1"))
        url, headers, body = fake.calls[0]
        self.assertTrue(url.endswith("/chat/completions"))
        self.assertEqual(headers["Authorization"], "Bearer nvapi-x")
        self.assertEqual(body["messages"][0]["role"], "system")

    def test_retries_without_response_format_when_the_model_rejects_it(self):
        fake = Fake((400, {}, b"{}"), reply(json.dumps(GOOD)))
        with mock.patch.object(nvidia, "_post", fake):
            nvidia.create("m", "s", "u", "k")
        self.assertIn("response_format", fake.calls[0][2])
        self.assertNotIn("response_format", fake.calls[1][2])

    def test_http_errors_map_to_named_errors(self):
        for status, exc in ((401, nvidia.AuthenticationError), (403, nvidia.PermissionDeniedError), (404, nvidia.NotFoundError),
                            (429, nvidia.RateLimitError), (500, nvidia.APIStatusError)):
            with mock.patch.object(nvidia, "_post", Fake((status, {}, b"{}"))):
                with self.assertRaises(exc):
                    nvidia.create("m", "s", "u", "k")

    def test_only_https_base_urls_are_used(self):
        with self.assertRaises(nvidia.APIConnectionError):
            nvidia.create("m", "s", "u", "k", base_url="http://insecure.example")

    def test_length_and_filter_finish_reasons(self):
        with mock.patch.object(nvidia, "_post", Fake(reply("{}", finish="content_filter"))):
            self.assertEqual(nvidia.create("m", "s", "u", "k").stop_reason, "refusal")


class Settings(unittest.TestCase):
    def test_provider_selects_key_model_and_never_leaks_the_key(self):
        with mock.patch.dict(os.environ, ENV):
            s = drafting.settings_summary()
            self.assertTrue(drafting.enabled())
            self.assertEqual((s["provider"], s["model"]), ("nvidia", "nvidia/test-nemotron"))
            self.assertNotIn("nvapi", json.dumps(s))
        with mock.patch.dict(os.environ, {"CAMPAIGN_AI_PROVIDER": "", "ANTHROPIC_API_KEY": "", "NVIDIA_API_KEY": "nvapi-x"}):
            self.assertFalse(drafting.enabled())            # an NVIDIA key alone does not enable the Anthropic provider
            self.assertEqual(drafting.settings_summary()["provider"], "anthropic")


class EndToEnd(WebCase):
    def draft(self, fake, **extra):
        with mock.patch.dict(os.environ, ENV), mock.patch.object(nvidia, "_post", fake):
            return self.post("/api/email-campaigns/ai/draft", dict(FORM, **extra))

    def test_a_draft_goes_through_nvidia_and_is_recorded_with_its_provider(self):
        self._login()
        fake = Fake(reply(json.dumps(GOOD)))
        r = self.draft(fake)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        g = r.get_json()["generation"]
        self.assertEqual(g["subject"], GOOD["subject"])
        row = self.conn.execute("SELECT provider, model, input_tokens, request_id, effort FROM ai_generations WHERE id=?", (g["generation_id"],)).fetchone()
        self.assertEqual(tuple(row), ("nvidia", "nvidia/test-nemotron", 800, "req-nv-1", "n/a"))
        sent = json.dumps(fake.calls[0][2])
        self.assertIn("ONLY one JSON object", sent)
        self.assertNotIn("nvapi-test-not-real", sent)               # the key travels only in the Authorization header
        self.assertNotIn("9876543210", sent)

    def test_unusable_model_output_is_a_recorded_failure_not_a_crash(self):
        self._login()
        for text in ("Sure! Here is a draft.", json.dumps({"subject": "Hi", "body": "Hello {bogus}"})):
            r = self.draft(Fake(reply(text)))
            self.assertEqual(r.status_code, 502)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) n FROM ai_generations WHERE status='invalid_output' AND provider='nvidia'").fetchone()["n"], 2)

    def test_auth_failure_shows_a_safe_message(self):
        self._login()
        r = self.draft(Fake((401, {}, b"{}")))
        self.assertEqual(r.status_code, 502)
        self.assertIn("rejected the server's key", r.get_json()["error"])
        self.assertNotIn("nvapi", r.get_data(as_text=True))

    def test_not_configured_message_names_the_right_variable(self):
        self._login()
        with mock.patch.dict(os.environ, dict(ENV, NVIDIA_API_KEY="")):
            r = self.post("/api/email-campaigns/ai/draft", FORM)
        self.assertEqual(r.status_code, 503)


if __name__ == "__main__":
    unittest.main()
