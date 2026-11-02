"""The AI drafting endpoints, the feedback hooks on the campaign routes, and the AI quality pages. Never calls a real AI service."""
import os
from unittest import mock

from campaigns.ai import drafting, telemetry
from tests.test_ai import GOOD, FakeClient, FakeResponse
from tests.test_campaigns import LIVE_ENV, csp
from tests.test_campaigns_web import WebCase

KEY = {"ANTHROPIC_API_KEY": "sk-test-not-real", "CAMPAIGN_AI_ENABLED": "1"}
FORM = {"occasion": "Diwali", "devices": "GPS", "audience_match": "any", "brief": "Warm tone please"}


class AiWeb(WebCase):
    def draft(self, client=None, **extra):
        client = client or FakeClient()
        with mock.patch.dict(os.environ, KEY), mock.patch.object(drafting, "get_client", return_value=client):
            r = self.post("/api/email-campaigns/ai/draft", dict(FORM, **extra))
        return r, client

    def test_ai_endpoints_need_login_and_csrf(self):
        for url in ("/api/email-campaigns/ai/context", "/api/email-campaigns/ai/draft"):
            self.assertIn(self.client.post(url, data=FORM).status_code, (400, 401), url)
        self.assertEqual(self.client.get("/api/email-campaigns/ai/metrics").status_code, 401)
        for url in ("/email-campaigns/ai-quality", "/email-campaigns/ai/1"):
            self.assertEqual(self.client.get(url).status_code, 302, url)
        self._login()
        self.assertEqual(self.client.post("/api/email-campaigns/ai/draft", data=FORM).status_code, 400)   # no CSRF token

    def test_draft_is_503_when_not_configured(self):
        self._login()
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            r = self.post("/api/email-campaigns/ai/draft", FORM)
        self.assertEqual(r.status_code, 503)
        self.assertNotIn("sk-", r.get_data(as_text=True))

    def test_context_preview_is_free_and_makes_no_model_call(self):
        self._login()
        with mock.patch.object(drafting, "get_client", side_effect=AssertionError("no model call")):
            r = self.post("/api/email-campaigns/ai/context", dict(FORM, brief="call 9876543210 or a@b.com"))
        data = r.get_json()
        self.assertTrue(data["ok"])
        self.assertTrue(any(c["locked"] for c in data["chunks"]))
        self.assertNotIn("9876543210", r.get_data(as_text=True))
        self.assertNotIn("a@b.com", r.get_data(as_text=True))
        self.assertNotIn("api_key", r.get_data(as_text=True).lower())

    def test_draft_returns_text_and_records_one_generation(self):
        self._login()
        r, client = self.draft()
        g = r.get_json()["generation"]
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(g["subject"], GOOD["subject"])
        row = self.conn.execute("SELECT status, created_by FROM ai_generations WHERE id=?", (g["generation_id"],)).fetchone()
        self.assertEqual(row["status"], "ok")

    def test_occasion_is_required(self):
        self._login()
        r, _ = self.draft(occasion="")
        self.assertEqual(r.status_code, 400)

    def test_failed_call_is_502_and_still_recorded(self):
        self._login()
        r, _ = self.draft(client=FakeClient(error=RuntimeError("boom")))
        self.assertEqual(r.status_code, 502)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) n FROM ai_generations WHERE status='error'").fetchone()["n"], 1)

    def test_regenerate_logs_the_replaced_draft(self):
        self._login()
        first = self.draft()[0].get_json()["generation"]["generation_id"]
        self.draft(replaces_generation_id=str(first))
        n = self.conn.execute("SELECT COUNT(*) n FROM ai_feedback WHERE generation_id=? AND event='regenerated'", (first,)).fetchone()["n"]
        self.assertEqual(n, 1)

    def test_saving_a_draft_logs_the_edit_distance(self):
        self._login()
        gid = self.draft()[0].get_json()["generation"]["generation_id"]
        self.post("/email-campaigns/new", {"name": "Diwali", "occasion": "Diwali", "devices": "GPS", "audience_match": "any", "description": "",
                                           "subject": GOOD["subject"] + "!", "body": GOOD["body"], "ai_generation_id": str(gid)})
        ev = self.conn.execute("SELECT * FROM ai_feedback WHERE generation_id=? AND event='draft_saved'", (gid,)).fetchone()
        self.assertIsNotNone(ev)
        self.assertEqual(ev["subject_distance"], 1)
        self.assertEqual(ev["body_distance"], 0)
        self.assertEqual(ev["outcome"], "edited")
        linked = self.conn.execute("SELECT campaign_id FROM ai_generations WHERE id=?", (gid,)).fetchone()["campaign_id"]
        self.assertIsNotNone(linked)

    def test_a_forged_generation_id_cannot_break_saving(self):
        self._login()
        r = self.post("/email-campaigns/new", {"name": "Plain", "occasion": "Diwali", "devices": "GPS", "audience_match": "any", "description": "",
                                               "subject": "Hi {first_name}", "body": "Dear {name}", "ai_generation_id": "99999"}, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) n FROM ai_feedback").fetchone()["n"], 0)

    def test_dry_run_and_launch_are_logged_for_ai_campaigns(self):
        self._login()
        self.seed([csp("C1", name="Ramesh Chandra", email="ramesh.c@example.com", matm=True, printer=True, gps=False)])
        r = self.draft()[0]
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        gid = r.get_json()["generation"]["generation_id"]
        self.post("/email-campaigns/new", {"name": "Diwali", "occasion": "Diwali", "devices": "GPS", "audience_match": "any", "description": "",
                                           "subject": GOOD["subject"], "body": GOOD["body"], "ai_generation_id": str(gid)})
        cid = self.conn.execute("SELECT id FROM email_campaigns ORDER BY id DESC LIMIT 1").fetchone()["id"]
        self.post(f"/email-campaigns/{cid}/action", {"action": "dry_run"})
        with mock.patch.dict(os.environ, LIVE_ENV):
            self.post(f"/email-campaigns/{cid}/action", {"action": "launch", "confirm_text": "1"})
        events = [r["event"] for r in self.conn.execute("SELECT event FROM ai_feedback WHERE generation_id=? ORDER BY id", (gid,))]
        self.assertIn("dry_run", events)
        self.assertIn("launched", events)
        launched = self.conn.execute("SELECT outcome FROM ai_feedback WHERE generation_id=? AND event='launched'", (gid,)).fetchone()
        self.assertEqual(launched["outcome"], "unchanged")

    def test_pages_render_and_show_no_secret(self):
        self._login()
        gid = self.draft()[0].get_json()["generation"]["generation_id"]
        telemetry.log_feedback(gid, "draft_saved", "tester", None, GOOD["subject"] + "?", GOOD["body"])
        with mock.patch.dict(os.environ, KEY):
            q = self.client.get("/email-campaigns/ai-quality")
            one = self.client.get(f"/email-campaigns/ai/{gid}")
            form = self.client.get("/email-campaigns/new")
        for r in (q, one, form):
            self.assertEqual(r.status_code, 200)
            self.assertNotIn("sk-test-not-real", r.get_data(as_text=True))
        self.assertIn("Not enough data", q.get_data(as_text=True))
        self.assertIn("<ins>", one.get_data(as_text=True))
        self.assertIn("ai-panel", form.get_data(as_text=True))
        self.assertEqual(self.client.get("/email-campaigns/ai/99999").status_code, 404)
        self.assertTrue(self.client.get("/api/email-campaigns/ai/metrics").get_json()["ok"])

    def test_a_draft_carrying_a_personal_email_is_discarded(self):
        self._login()
        self.seed([csp("C9", name="Sunita Verma", email="sunita.verma@example.com")])
        bad = FakeClient(FakeResponse({"subject": "Hi", "body": "Write to sunita.verma@example.com", "used_context_ids": []}))
        r, _ = self.draft(client=bad)
        self.assertEqual(r.status_code, 502)
