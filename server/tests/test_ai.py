"""AI drafting: context selection, privacy, the model call, telemetry and the metrics. The model is always a fake: no network."""
import json
import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import db
from campaigns import operators as ops, service
from campaigns.ai import context, drafting, knowledge, metrics, redaction, telemetry
from tests.helpers import make_test_app
from tests.test_campaigns import base_fields, csp

GOOD = {"subject": "Happy {occasion}, {first_name}!", "body": "Dear {name},\n\nWishing you a joyful {occasion}. You do not yet have {missing_devices}. "
                                                              "Please speak to {rm_name} if you would like to know more.\n\nTeam Eko",
        "used_context_ids": ["style.voice", "device.GPS", "policy.compliance", "not-a-real-chunk"]}


class FakeResponse:
    def __init__(self, payload=None, text=None, stop="end_turn", fallback=False):
        body = text if text is not None else json.dumps(payload if payload is not None else GOOD)
        self.content = [SimpleNamespace(type="text", text=body)]
        self.usage = SimpleNamespace(input_tokens=900, output_tokens=140, cache_read_input_tokens=0,
                                     iterations=[SimpleNamespace(type="fallback_message")] if fallback else None)
        self.stop_reason, self.model, self._request_id = stop, "claude-opus-5-5", "req_test_123"


class FakeClient:
    """Stands in for anthropic.Anthropic(): records every request, returns a canned reply or raises."""
    def __init__(self, response=None, error=None):
        self.calls, self.response, self.error = [], response, error
        outer = self

        class Messages:
            def create(self, **kw):
                outer.calls.append(kw)
                if outer.error:
                    raise outer.error
                return outer.response or FakeResponse()
        self.beta = SimpleNamespace(messages=Messages())

    def sent_text(self):
        """Every string the model would have received."""
        out = []
        for kw in self.calls:
            out.append(kw.get("system", ""))
            out += [m["content"] for m in kw["messages"]]
        return "\n".join(out)


class AiCase(unittest.TestCase):
    def setUp(self):
        self.app, self.path = make_test_app()
        self.conn = db.get_connection()
        self.addCleanup(self.conn.close)
        ops.import_operators(self.conn, {"csps": [
            csp("1AB23456", name="Sunita Verma", email="sunita.verma@mail.example.com", gps=False),
            csp("9ZX77001", name="Mohan Das Kumar", email="mohan@mail.example.com", gps=False)]}, "t", "t.json")
        for r in self.conn.execute("SELECT csp_code FROM campaign_operators").fetchall():
            self.conn.execute("UPDATE campaign_operators SET mobile=? WHERE csp_code=?", ("9876543210" if r[0] == "1AB23456" else "9123456780", r[0]))
        self.conn.commit()
        self.fields = {"occasion": "Dhanteras / Diwali", "devices": ["GPS"], "audience_match": "any", "include_dormant": False}

    def gen(self, client=None, brief="", excluded=(), actor="tester", replaces=None):
        client = client or FakeClient()
        out = drafting.generate(self.conn, self.fields, brief, excluded, actor, client=client, replaces=replaces)
        return out, client

    def row(self, gid):
        return telemetry.get_generation(self.conn, gid)


class ContextSelection(AiCase):
    def plan(self, **kw):
        return context.plan(self.conn, self.fields["occasion"], kw.pop("devices", self.fields["devices"]), kw.pop("brief", ""), **kw)

    def ids(self, plan, included=True):
        return [c["id"] for c in plan["chunks"] if c["included"] is included]

    def test_the_rules_the_occasion_and_only_the_selected_devices_are_always_in(self):
        p = self.plan(devices=["GPS", "BIOMETRIC"])
        for required in ("policy.compliance", "policy.format", "audience.rule", "occasion.name", "device.GPS", "device.BIOMETRIC"):
            self.assertIn(required, self.ids(p))
        self.assertNotIn("device.MATM", [c["id"] for c in p["chunks"]])            # not even a candidate

    def test_a_person_can_switch_off_optional_chunks_but_never_the_rules(self):
        p = self.plan(excluded_ids={"style.voice", "policy.compliance", "device.GPS"})
        self.assertNotIn("style.voice", self.ids(p))
        self.assertEqual(next(c for c in p["chunks"] if c["id"] == "style.voice")["reason"], "switched off by the person drafting")
        self.assertIn("policy.compliance", self.ids(p))                            # locked
        self.assertIn("device.GPS", self.ids(p))

    def test_the_token_budget_is_respected_and_the_reason_is_recorded(self):
        p = self.plan(token_budget=300)
        self.assertLessEqual(sum(c["tokens"] for c in p["chunks"] if c["included"] and not c["locked"]) + sum(c["tokens"] for c in p["chunks"] if c["locked"]),
                             max(p["tokens_used"], 1) + 1)
        dropped = [c for c in p["chunks"] if not c["included"]]
        self.assertTrue(dropped)
        self.assertTrue(all(c["reason"] for c in dropped))
        self.assertTrue(any("token budget" in c["reason"] for c in dropped))
        big = self.plan(token_budget=20000)
        self.assertGreaterEqual(len(self.ids(big)), len(self.ids(p)))

    def test_relevant_earlier_campaigns_are_used_as_examples_and_scrubbed(self):
        cid = service.create_campaign(self.conn, service.validate_fields(base_fields(
            occasion="Dhanteras / Diwali", body="Dear {name}, hello Sunita Verma (sunita.verma@mail.example.com), call 9876543210. You lack {missing_devices}.")), "t")
        self.conn.execute("UPDATE email_campaigns SET status='completed', launched_at='2026-09-01T00:00:00+00:00' WHERE id=?", (cid,))
        other = service.create_campaign(self.conn, service.validate_fields(base_fields(name="Other", occasion="Holi", devices=["PRINTER"])), "t")
        self.conn.execute("UPDATE email_campaigns SET status='completed' WHERE id=?", (other,))
        self.conn.commit()
        p = context.plan(self.conn, "Dhanteras / Diwali", ["GPS"], "", known=redaction.load_known(self.conn))
        by = {c["id"]: c for c in p["chunks"]}
        same, diff = by[f"example.campaign{cid}"], by[f"example.campaign{other}"]
        self.assertTrue(same["included"])
        self.assertGreater(same["score"], diff["score"])
        for secret in ("Sunita Verma", "sunita.verma@mail.example.com", "9876543210"):
            self.assertNotIn(secret, same["text"])
        self.assertIn("campaign #", same["source"])

    def test_draft_campaigns_are_never_used_as_examples(self):
        service.create_campaign(self.conn, service.validate_fields(base_fields()), "t")
        self.conn.commit()
        self.assertFalse([c for c in self.plan()["chunks"] if c["kind"] == "example"])


class Privacy(AiCase):
    SECRETS = ("Sunita Verma", "sunita.verma@mail.example.com", "1AB23456", "9876543210", "Mohan Das Kumar", "mohan@mail.example.com", "9ZX77001", "9123456780")

    def test_nothing_personal_is_ever_sent_to_the_model(self):
        brief = ("Write it for Sunita Verma (sunita.verma@mail.example.com, 9876543210, code 1ab23456), "
                 "maybe mention Mohan Das Kumar, mohan@mail.example.com, +91 91234 56780, 9ZX77001 and https://x.example/t?tok=SECRETTOKEN")
        out, client = self.gen(brief=brief)
        sent = client.sent_text()
        for secret in self.SECRETS + ("SECRETTOKEN",):
            self.assertNotIn(secret.lower(), sent.lower(), secret)
        row = self.row(out["generation_id"])
        stored = (row["system_prompt"] or "") + (row["user_prompt"] or "") + (row["brief_redacted"] or "")
        for secret in self.SECRETS:
            self.assertNotIn(secret.lower(), stored.lower(), secret)
        self.assertGreaterEqual(row["redaction"]["emails"], 2)
        self.assertNotIn("sunita", json.dumps(row["redaction"]).lower())             # the report holds counts, never values

    def test_the_prompt_is_built_from_non_personal_chunks_only(self):
        _, client = self.gen()
        sent = client.sent_text()
        self.assertIn("<context>", sent)
        self.assertIn('id="device.GPS"', sent)
        self.assertNotIn("@mail.example.com", sent)

    def test_a_last_check_refuses_to_send_if_anything_personal_slips_into_the_prompt(self):
        leaky = [dict(c) for c in knowledge.FIXED_CHUNKS]
        leaky[0]["text"] += " Contact 1AB23456 for details."
        client = FakeClient()
        with mock.patch.object(knowledge, "FIXED_CHUNKS", leaky), self.assertRaises(drafting.DraftError) as cm:
            drafting.generate(self.conn, self.fields, "", (), "tester", client=client)
        self.assertIn("Blocked", str(cm.exception))
        self.assertEqual(client.calls, [])                                           # the model was never called
        self.assertEqual(self.row(cm.exception.generation_id)["status"], "error")

    def test_a_reply_containing_personal_data_is_discarded(self):
        bad = dict(GOOD, body="Dear {name},\n\nWrite to sunita.verma@mail.example.com about {missing_devices}.")
        with self.assertRaises(drafting.DraftError) as cm:
            self.gen(client=FakeClient(FakeResponse(bad)))
        self.assertIn("personal data", str(cm.exception))
        self.assertEqual(self.row(cm.exception.generation_id)["status"], "invalid_output")


class ModelCall(AiCase):
    def test_the_request_uses_the_documented_shape(self):
        out, client = self.gen(brief="Keep it short")
        kw = client.calls[0]
        self.assertEqual(kw["model"], "claude-opus-5-5")
        self.assertEqual(kw["betas"], ["server-side-fallback-2026-07-01"])
        self.assertEqual(kw["fallbacks"], "default")
        self.assertEqual(kw["output_config"]["effort"], "low")
        self.assertEqual(kw["output_config"]["format"]["type"], "json_schema")
        self.assertFalse(kw["output_config"]["format"]["schema"]["additionalProperties"])
        self.assertEqual(kw["system"], knowledge.SYSTEM_PROMPT)
        self.assertEqual([m["role"] for m in kw["messages"]], ["user"])
        self.assertGreaterEqual(kw["max_tokens"], 4000)
        for forbidden in ("temperature", "top_p", "top_k", "budget_tokens", "tool_choice"):
            self.assertNotIn(forbidden, kw)

    def test_the_model_can_be_overridden_by_the_environment(self):
        with mock.patch.dict(os.environ, {"CAMPAIGN_AI_MODEL": "claude-sonnet-5-5"}):
            _, client = self.gen()
        self.assertEqual(client.calls[0]["model"], "claude-sonnet-5-5")

    def test_a_good_reply_is_recorded_as_one_atomic_block(self):
        out, _ = self.gen(brief="warm tone")
        r = self.row(out["generation_id"])
        self.assertEqual((r["status"], r["model"], r["prompt_version"], r["request_id"]), ("ok", "claude-opus-5-5", "festival-draft-v1", "req_test_123"))
        self.assertEqual((r["input_tokens"], r["output_tokens"]), (900, 140))
        self.assertEqual(r["output_subject"], GOOD["subject"])
        self.assertEqual(r["output_body"], GOOD["body"])
        self.assertTrue(r["system_prompt"] and r["user_prompt"] and r["context"])
        self.assertEqual(r["brief_redacted"], "warm tone")
        self.assertAlmostEqual(r["est_cost_usd"], (900 * 4 + 140 * 20) / 1e6, places=6)
        self.assertEqual({c["id"] for c in r["context"] if c["used"]}, {"style.voice", "device.GPS", "policy.compliance"})
        self.assertNotIn("not-a-real-chunk", r["cited"])                              # made-up citations are dropped
        for c in r["context"]:
            self.assertTrue({"id", "included", "reason", "score", "tokens", "source", "text"} <= set(c))
        self.assertEqual(out["tokens_used_est"], r["context_tokens_est"])

    def test_every_failure_is_recorded_and_explained(self):
        class RateLimitError(Exception):
            pass
        cases = [(FakeClient(error=RateLimitError("429")), "error", "busy"), (FakeClient(error=ConnectionError("down")), "error", "AI service"),
                 (FakeClient(FakeResponse(stop="refusal")), "refused", "declined"), (FakeClient(FakeResponse(text="not json")), "invalid_output", "could not be used"),
                 (FakeClient(FakeResponse(dict(GOOD, body="Hi {secret_field}"))), "invalid_output", "Unknown placeholder"),
                 (FakeClient(FakeResponse(dict(GOOD, subject="   "))), "invalid_output", "subject")]
        for client, status, text in cases:
            with self.assertRaises(drafting.DraftError) as cm:
                self.gen(client=client)
            self.assertIn(text, str(cm.exception))
            self.assertEqual(self.row(cm.exception.generation_id)["status"], status)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ai_generations WHERE status<>'ok'").fetchone()[0], len(cases))

    def test_the_assistant_is_off_until_a_key_is_configured(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            self.assertFalse(drafting.enabled())
            with self.assertRaises(drafting.AiUnavailable):
                drafting.generate(self.conn, self.fields, "", (), "t")
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test", "CAMPAIGN_AI_ENABLED": "0"}):
            self.assertFalse(drafting.enabled())
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            self.assertTrue(drafting.enabled())
            self.assertNotIn("sk-test", json.dumps(drafting.settings_summary()))

    def test_a_fallback_is_noticed_and_stored(self):
        out, _ = self.gen(client=FakeClient(FakeResponse(fallback=True)))
        self.assertTrue(self.row(out["generation_id"])["fallback_used"])

    def test_regenerating_marks_the_earlier_draft_as_replaced(self):
        first, _ = self.gen()
        second, _ = self.gen(replaces=first["generation_id"])
        ev = self.conn.execute("SELECT event, detail FROM ai_feedback WHERE generation_id=?", (first["generation_id"],)).fetchall()
        self.assertEqual([e["event"] for e in ev], ["regenerated"])
        self.assertIn(str(second["generation_id"]), ev[0]["detail"])


class ReviewFlags(unittest.TestCase):
    def kinds(self, body, subject="Hello"):
        return {f["kind"] for f in drafting.review_flags(subject, body)}

    def test_risky_wording_is_highlighted(self):
        self.assertIn("amount", self.kinds("Get it for just Rs. 8,500 today."))
        self.assertIn("amount", self.kinds("It costs ₹500."))
        self.assertIn("security", self.kinds("Share your OTP with us."))
        self.assertIn("promise", self.kinds("A guaranteed result."))
        self.assertIn("earnings", self.kinds("You can earn more commission."))
        self.assertIn("offer", self.kinds("A special discount awaits."))
        self.assertIn("pressure", self.kinds("Hurry, last chance!"))
        self.assertIn("bank", self.kinds("Works with SBI accounts."))

    def test_a_clean_draft_has_no_flags_and_flags_carry_the_sentence_and_a_reason(self):
        self.assertEqual(drafting.review_flags(GOOD["subject"], GOOD["body"]), [])
        f = drafting.review_flags("Hi", "First line.\nThis is guaranteed to work.")[0]
        self.assertEqual((f["severity"], f["kind"]), ("high", "promise"))
        self.assertIn("guaranteed", f["sentence"])
        self.assertTrue(f["why"])


class Feedback(AiCase):
    def setUp(self):
        super().setUp()
        self.out, _ = self.gen()
        self.gid = self.out["generation_id"]

    def events(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM ai_feedback WHERE generation_id=? ORDER BY id", (self.gid,)).fetchall()]

    def test_an_untouched_draft_has_distance_zero(self):
        telemetry.log_feedback(self.gid, "draft_saved", "t", 1, GOOD["subject"], GOOD["body"])
        e = self.events()[0]
        self.assertEqual((e["subject_distance"], e["body_distance"], e["edit_ratio"], e["outcome"]), (0, 0, 0.0, "unchanged"))

    def test_a_small_edit_is_measured_exactly(self):
        telemetry.log_feedback(self.gid, "launched", "t", 1, GOOD["subject"], GOOD["body"].replace("joyful", "happy"))
        e = self.events()[0]
        self.assertEqual(e["subject_distance"], 0)
        self.assertEqual(e["body_distance"], 6)                                      # "joyful" -> "happy": exactly 6 character edits
        self.assertEqual(e["outcome"], "edited")
        self.assertTrue(0 < e["edit_ratio"] < 0.1)

    def test_a_full_rewrite_is_recognised(self):
        telemetry.log_feedback(self.gid, "launched", "t", 1, "Completely different", "Nothing like the draft. " * 12)
        self.assertEqual(self.events()[0]["outcome"], "rewritten")

    def test_events_without_text_carry_no_distance(self):
        telemetry.log_feedback(self.gid, "previewed", "t", 1)
        e = self.events()[0]
        self.assertEqual((e["event"], e["edit_ratio"], e["outcome"]), ("previewed", None, None))

    def test_history_is_append_only_each_save_is_its_own_row(self):
        for t in ("a", "b", "c"):
            telemetry.log_feedback(self.gid, "draft_saved", "t", 1, GOOD["subject"], GOOD["body"] + t)
        self.assertEqual(len(self.events()), 3)

    def test_bad_input_is_ignored_never_raised(self):
        self.assertIsNone(telemetry.log_feedback(None, "launched", "t"))
        self.assertIsNone(telemetry.log_feedback(self.gid, "nonsense", "t"))
        telemetry.log_feedback(999999, "launched", "t", 1, "x", "y")                   # unknown generation: silently nothing
        self.assertEqual(self.events(), [])

    def test_a_telemetry_failure_never_breaks_the_caller(self):
        self.conn.execute("DROP TABLE ai_feedback")
        self.conn.commit()
        telemetry.log_feedback(self.gid, "launched", "t", 1, "x", "y")                 # the write fails inside; nothing escapes

    def test_the_campaign_link_is_set_once(self):
        telemetry.link_campaign(self.gid, 7)
        telemetry.link_campaign(self.gid, 8)
        self.assertEqual(self.row(self.gid)["campaign_id"], 7)

    def test_in_production_mode_feedback_is_queued_and_written_off_the_request_path(self):
        with mock.patch.dict(os.environ, {"CSP_MONITOR_TELEMETRY_SYNC": "0"}):
            t = time.time()
            how = telemetry.log_feedback(self.gid, "draft_saved", "t", 1, GOOD["subject"], GOOD["body"] * 40)   # long text: the distance work is not inline
            returned_in = time.time() - t
            self.assertEqual(how, "queued")
            self.assertLess(returned_in, 0.5)
            self.assertTrue(telemetry.drain(10))
        self.assertEqual(len(self.events()), 1)

    def test_a_full_queue_falls_back_to_writing_inline_instead_of_losing_the_event(self):
        import queue
        with mock.patch.dict(os.environ, {"CSP_MONITOR_TELEMETRY_SYNC": "0"}), mock.patch.object(telemetry._queue, "put_nowait", side_effect=queue.Full):
            self.assertEqual(telemetry.log_feedback(self.gid, "previewed", "t", 1), "inline-overflow")
        self.assertEqual([e["event"] for e in self.events()], ["previewed"])


class Metrics(AiCase):
    def make(self, outcomes, campaign_base=100, tokens=(900, 140), week_offsets=None):
        """One generation per outcome ('unchanged' | 'edited' | 'rewritten' | None=never launched), launched like a real one."""
        ids = []
        for i, outcome in enumerate(outcomes):
            when = (datetime.now(timezone.utc) - timedelta(days=(week_offsets[i] if week_offsets else 1))).isoformat(timespec="seconds")
            gid = telemetry.record_generation(self.conn, {"created_at": when, "status": "ok", "model": "claude-opus-5-5", "output_subject": "S", "output_body": "B" * 100,
                                                          "input_tokens": tokens[0], "output_tokens": tokens[1], "latency_ms": 1000, "est_cost_usd": 0.01, "campaign_id": campaign_base + i,
                                                          "context": [{"id": "policy.x", "included": True, "locked": True}, {"id": "style.voice", "included": True, "locked": False},
                                                                      {"id": "style.length", "included": True, "locked": False}],
                                                          "cited_context": ["style.voice"]})
            self.conn.commit()                                   # as generate() does, so the background writer (another connection) can see it
            ids.append(gid)
            final_body = {"unchanged": "B" * 100, "edited": "B" * 90 + "X" * 5, "rewritten": "Z" * 100, None: None}[outcome]
            if final_body is not None:
                telemetry.log_feedback(gid, "launched", "t", campaign_base + i, "S", final_body)
        self.conn.commit()
        return ids

    def test_too_little_data_says_so_instead_of_showing_a_number(self):
        self.make(["unchanged", "edited"])
        m = metrics.compute(self.conn)
        self.assertFalse(m["enough_data"])
        self.assertEqual((m["launched"], m["status"]["acceptance"], m["status"]["delta"]), (2, None, None))

    def test_implicit_acceptance_and_correction_delta(self):
        self.make(["unchanged"] * 5 + ["edited"])
        m = metrics.compute(self.conn)
        self.assertTrue(m["enough_data"])
        self.assertEqual((m["launched"], m["acceptance_rate"]), (6, 83.3))
        self.assertTrue(m["status"]["acceptance"])                                    # 83.3 > 75
        self.assertEqual(m["outcomes_at_launch"], {"unchanged": 5, "edited": 1, "rewritten": 0})
        self.assertEqual(m["avg_distance_chars"], round(10 / 6, 2))                   # one launch changed 10 characters (5 replaced, 5 removed)
        self.assertTrue(m["status"]["delta"])
        self.assertEqual(m["light_edit_rate"], 83.3)                                  # the edited one is 10% of the text: more than a light edit

    def test_a_poor_acceptance_rate_is_reported_as_missing_the_target(self):
        self.make(["edited"] * 3 + ["rewritten"] * 3 + ["unchanged"])
        m = metrics.compute(self.conn)
        self.assertEqual(m["acceptance_rate"], 14.3)
        self.assertFalse(m["status"]["acceptance"])
        self.assertFalse(m["status"]["delta"])

    def test_task_completion_counts_every_failed_attempt(self):
        self.make(["unchanged"] * 5)
        for status in ("error", "refused"):
            telemetry.record_generation(self.conn, {"status": status, "error": "x"})
        self.conn.commit()
        m = metrics.compute(self.conn)
        self.assertEqual((m["generations"], m["ok"], m["failed"], m["completion_rate"]), (7, 5, 2, 71.4))
        self.assertEqual(m["failures_by_status"], {"error": 1, "refused": 1})
        self.assertFalse(m["status"]["completion"])

    def test_context_efficiency_precision_and_cost(self):
        self.make(["unchanged"] * 5)
        m = metrics.compute(self.conn)
        self.assertEqual((m["avg_input_tokens"], m["avg_output_tokens"], m["tokens_per_launched_campaign"]), (900.0, 140.0, 1040.0))
        self.assertEqual(m["retrieval_precision_optional"], 50.0)                     # 1 of 2 optional chunks cited
        self.assertAlmostEqual(m["est_cost_usd"], 0.05)
        self.assertEqual(m["avg_latency_ms"], 1000.0)

    def test_regenerations_and_edits_are_counted_as_interventions(self):
        ids = self.make(["unchanged"] * 5)
        telemetry.log_feedback(ids[0], "regenerated", "t")
        telemetry.log_feedback(ids[1], "draft_saved", "t", 1, "S", "B" * 90 + "Q" * 5)
        telemetry.log_feedback(ids[2], "draft_saved", "t", 1, "S", "B" * 100)         # saved unchanged: not an intervention
        self.conn.commit()
        m = metrics.compute(self.conn)
        self.assertEqual((m["regenerations"], m["edits_saved"], m["hitl_per_launched"]), (1, 1, 0.4))

    def test_weekly_trends_follow_the_data(self):
        self.make(["edited", "edited", "unchanged", "unchanged", "unchanged", "unchanged"], week_offsets=[50, 43, 36, 29, 22, 15])
        m = metrics.compute(self.conn)
        self.assertGreaterEqual(len(m["weekly"]), 4)
        self.assertEqual(m["trend_edit_ratio"], "down")                               # edits fell over the weeks: improving
        self.assertEqual({w["week"] for w in m["weekly"]}, {w["week"] for w in m["weekly"]})

    def test_an_empty_system_computes_without_error(self):
        m = metrics.compute(self.conn)
        self.assertEqual((m["generations"], m["acceptance_rate"], m["completion_rate"]), (0, None, None))
        self.assertEqual(metrics.recent_generations(self.conn), [])

    def test_recent_generations_show_the_outcome(self):
        self.make(["unchanged", "edited", None])
        rows = metrics.recent_generations(self.conn)
        self.assertEqual([r["launched_outcome"] for r in rows], [None, "edited", "unchanged"])


if __name__ == "__main__":
    unittest.main()
