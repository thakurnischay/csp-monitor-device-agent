"""The Email Campaigns tab, its JSON API, the public tracking / unsubscribe links and the provider events endpoint.
Never contacts a real mail server."""
import io
import json
import os
import re
import smtplib
import unittest
from datetime import datetime, timezone
from unittest import mock

import db
from campaigns import operators as ops, service, tracking, worker
from tests.test_campaigns import LIVE_ENV, NOW, FakeTransport, base_fields, csp
from tests.test_routes import RouteTestCase, _extract_csrf


class WebCase(RouteTestCase):
    def setUp(self):
        super().setUp()
        self.conn = db.get_connection()
        self.addCleanup(self.conn.close)

    def seed(self, rows):
        ops.import_operators(self.conn, {"csps": rows, "importedAt": "2026-10-22T00:00:00+00:00"}, "tester", "t.json")
        self.conn.commit()

    def make_campaign(self, **kw):
        cid = service.create_campaign(self.conn, service.validate_fields(base_fields(**kw)), "tester", NOW)
        self.conn.commit()
        return cid

    def launch_and_send(self, cid, transport=None):
        with mock.patch.dict(os.environ, LIVE_ENV):
            n = service.materialize(self.conn, cid, NOW)["counts"]["eligible"]
            service.launch(self.conn, cid, "tester", str(n), now=NOW)
            self.conn.commit()
            worker.run_batch(self.conn, cid, transport or FakeTransport(), now=NOW, sleep=lambda s: None)
            self.conn.commit()

    def token(self, url="/email-campaigns/new"):
        return _extract_csrf(self.client.get(url).get_data(as_text=True))

    def post(self, url, data=None, **kw):
        return self.client.post(url, data=dict({"csrf_token": self.token()}, **(data or {})), **kw)

    def rid(self, cid, code):
        return self.conn.execute("SELECT id FROM email_campaign_recipients WHERE campaign_id=? AND csp_code=?", (cid, code)).fetchone()["id"]


class Access(WebCase):
    def test_every_page_and_json_endpoint_needs_the_admin_login(self):
        for url in ("/email-campaigns", "/email-campaigns/new", "/email-campaigns/1", "/email-campaigns/operators", "/email-campaigns/messages/1"):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 302, url)
            self.assertIn("/login", r.headers["Location"])
        for url in ("/api/email-campaigns/summary", "/api/email-campaigns/campaigns", "/api/email-campaigns/messages",
                    "/api/email-campaigns/messages/1", "/api/email-campaigns/1/audience", "/api/email-campaigns/1/preview", "/api/email-campaigns/1/progress"):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 401, url)
            self.assertFalse(r.get_json()["ok"])

    def test_every_post_needs_the_csrf_token(self):
        self._login()
        for url in ("/email-campaigns/new", "/email-campaigns/1/action", "/email-campaigns/operators/import", "/email-campaigns/operators/reconcile",
                    "/email-campaigns/operators/suppress", "/api/email-campaigns/1/dry_run"):
            self.assertEqual(self.client.post(url, data={}).status_code, 400, url)

    def test_the_new_tab_is_in_the_navigation_and_the_existing_tabs_are_untouched(self):
        self._login()
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("Email Campaigns", page)
        for tab in ("Fleet", "Agent Updates", "Software", "Enrollments", "API Keys", "Audit Log"):
            self.assertIn(tab, page)
        self.assertEqual(self.client.get("/email-campaigns").status_code, 200)


class MainTab(WebCase):
    def test_an_empty_system_shows_clear_empty_states_and_dry_mode(self):
        self._login()
        with mock.patch.dict(os.environ, {"EMAIL_MODE": "dry"}):
            page = self.client.get("/email-campaigns").get_data(as_text=True)
        self.assertIn("No operator data has been imported yet", page)
        self.assertIn("No campaigns yet", page)
        self.assertIn("No emails have been sent yet", page)
        self.assertIn("Dry-run mode", page)
        self.assertIn("EMAIL_MODE is not &#39;live&#39;", page)

    def test_the_tab_shows_real_backend_records_with_masked_addresses(self):
        self.seed([csp("W1", name="Sunita Verma", email="sunita@example.com", gps=False, rm="Asha", state="Bihar"),
                   csp("W2", name="Mohan Das", email="mohan@example.com", gps=False, state="Assam")])
        cid = self.make_campaign(name="Diwali greetings", occasion="Diwali")
        self.launch_and_send(cid, FakeTransport(fail={"mohan@example.com": smtplib.SMTPRecipientsRefused({"mohan@example.com": (550, b"no")})}))
        self._login()
        page = self.client.get("/email-campaigns").get_data(as_text=True)
        for text in ("Sunita Verma", "W1", "Diwali greetings", "Accepted by mail server", "Failed", "Bihar", "Asha", "Confirmed without: GPS dongle", "SMTPRecipientsRefused 550"):
            self.assertIn(text, page)
        self.assertNotIn("sunita@example.com", page)                      # masked in the table
        self.assertIn("s***@example.com", page)
        kpis = dict(re.findall(r'kpi-value">([^<]+)</div><div class="kpi-label">([^<]+)<', page)[i][::-1] for i in range(8))
        self.assertEqual(kpis["Emails sent (accepted by mail server)"], "1")
        self.assertEqual(kpis["Delivered (confirmed)"], "0")
        self.assertEqual(kpis["Failed attempts"], "1")
        self.assertEqual(kpis["Unique operators contacted"], "1")

    def test_delivery_is_shown_only_after_a_provider_confirms_it(self):
        self.seed([csp("W1", email="a@example.com", gps=False)])
        cid = self.make_campaign()
        self.launch_and_send(cid)
        self._login()
        self.assertNotIn("Delivered (confirmed)</span>", self.client.get("/email-campaigns").get_data(as_text=True))
        mid = self.conn.execute("SELECT smtp_message_id FROM email_campaign_recipients").fetchone()[0]
        tracking.ingest_provider_events(self.conn, [{"message_id": mid, "event": "delivered", "id": "e1"}])
        self.conn.commit()
        self.assertIn("Delivered (confirmed)</span>", self.client.get("/email-campaigns").get_data(as_text=True))

    def test_filters_search_and_pagination_work_on_the_page(self):
        self.seed([csp(f"P{i:02d}", name=f"Person {i:02d}", email=f"p{i}@example.com", gps=False, state="Bihar" if i % 2 else "Assam") for i in range(1, 31)])
        cid = self.make_campaign(name="Diwali greetings", occasion="Diwali")
        self.launch_and_send(cid)
        self._login()
        page = self.client.get("/email-campaigns?per_page=10").get_data(as_text=True)
        self.assertIn("Page 1 of 3", page)
        self.assertIn("30 messages", page)
        self.assertIn("page=2", page)
        self.assertIn("Page 3 of 3", self.client.get("/email-campaigns?per_page=10&page=3").get_data(as_text=True))
        one = self.client.get("/email-campaigns?q=Person+07").get_data(as_text=True)
        self.assertIn("1 message", one)
        self.assertIn("Person 07", one)
        self.assertNotIn("Person 08", one)
        self.assertIn("15 messages", self.client.get("/email-campaigns?location=Assam").get_data(as_text=True))
        self.assertIn("0 messages", self.client.get("/email-campaigns?status=failed").get_data(as_text=True))
        self.assertIn("No messages match these filters", self.client.get("/email-campaigns?status=failed").get_data(as_text=True))
        self.assertEqual(self.client.get("/email-campaigns?page=999&per_page=5&date_from=garbage").status_code, 200)

    def test_hostile_query_text_is_escaped_not_run(self):
        self._login()
        page = self.client.get('/email-campaigns?q=%22%3E%3Cscript%3Ealert(1)%3C/script%3E').get_data(as_text=True)
        self.assertNotIn("<script>alert(1)", page)


class CampaignPages(WebCase):
    def setUp(self):
        super().setUp()
        self.seed([csp("A1", name="Eligible One", email="e1@example.com", gps=False), csp("A2", name="Owns Gps", email="e2@example.com", gps=True),
                   csp("A3", name="Unknown Gps", email="e3@example.com", gps=None), csp("A4", name="Opted Out", email="e4@example.com", gps=False)])
        tracking.suppress(self.conn, "e4@example.com", "A4", "unsubscribed")
        self.conn.commit()
        self._login()

    def test_creating_a_campaign_saves_a_draft_and_sends_nothing(self):
        t = FakeTransport()
        with mock.patch.object(worker.sender, "get_transport", return_value=t):
            r = self.post("/email-campaigns/new", {"name": "Holi", "occasion": "Holi", "devices": "GPS", "audience_match": "any",
                                                    "subject": "Happy {occasion}", "body": "Dear {name}, you lack {missing_devices}."}, follow_redirects=True)
        page = r.get_data(as_text=True)
        self.assertIn("Draft saved. Nothing has been sent", page)
        self.assertIn("Holi", page)
        self.assertEqual(t.sent, [])
        self.assertEqual(self.conn.execute("SELECT status FROM email_campaigns").fetchone()[0], "draft")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM email_campaign_recipients WHERE status<>'pending'").fetchone()[0], 0)

    def test_bad_input_is_explained_and_nothing_is_saved(self):
        for data, message in (({"name": "", "occasion": "x", "devices": "GPS", "subject": "s", "body": "b"}, "Enter a campaign name"),
                              ({"name": "n", "occasion": "x", "subject": "s", "body": "b"}, "Choose at least one device"),
                              ({"name": "n", "occasion": "x", "devices": "GPS", "subject": "s", "body": "Hi {nope}"}, "Unknown placeholder"),
                              ({"name": "n", "occasion": "x", "devices": "GPS", "subject": "s", "body": "b", "link_url": "http://x.y"}, "https")):
            page = self.post("/email-campaigns/new", data).get_data(as_text=True)
            self.assertIn(message, page)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM email_campaigns").fetchone()[0], 0)

    def test_the_campaign_page_shows_eligible_review_and_excluded_with_reasons(self):
        cid = self.make_campaign()
        base = f"/email-campaigns/{cid}"
        eligible = self.client.get(base).get_data(as_text=True)
        self.assertIn("Eligible One", eligible)
        self.assertIn("Confirmed without GPS dongle", eligible)
        self.assertNotIn("Owns Gps", eligible)
        self.assertIn("Unknown Gps", self.client.get(base + "?view=review").get_data(as_text=True))
        self.assertIn("Ownership not confirmed for GPS dongle", self.client.get(base + "?view=review").get_data(as_text=True))
        excluded = self.client.get(base + "?view=excluded").get_data(as_text=True)
        self.assertIn("Already owns GPS dongle", excluded)
        self.assertIn("opted out", excluded)

    def test_the_preview_uses_a_sandboxed_frame_and_loads_nothing(self):
        cid = self.make_campaign()
        page = self.client.get(f"/email-campaigns/{cid}").get_data(as_text=True)
        self.assertIn('sandbox=""', page)
        frame = self.client.get(f"/email-campaigns/{cid}/preview.html")
        self.assertEqual(frame.status_code, 200)
        self.assertIn("Eligible One", frame.get_data(as_text=True))
        self.assertIn("img-src 'none'", frame.headers["Content-Security-Policy"])
        self.assertEqual(frame.headers["X-Frame-Options"], "SAMEORIGIN")
        self.assertNotIn("/c/o/", frame.get_data(as_text=True))                                   # no tracking pixel in a preview
        for_other = self.client.get(f"/email-campaigns/{cid}/preview.html?csp_code=A3")
        self.assertIn("Unknown Gps", for_other.get_data(as_text=True))

    def test_other_pages_keep_the_strict_headers(self):
        h = self.client.get("/email-campaigns").headers
        self.assertEqual(h["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", h["Content-Security-Policy"])

    def test_dry_run_through_the_page_sends_nothing_and_reports(self):
        cid = self.make_campaign()
        with mock.patch.object(worker.sender, "get_transport", side_effect=AssertionError("no transport in a dry run")):
            page = self.post(f"/email-campaigns/{cid}/action", {"action": "dry_run"}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("1 email(s) would be sent. Nothing was sent", page)
        self.assertIn("Last dry run", page)

    def test_launch_is_refused_in_dry_mode_and_without_the_typed_number(self):
        cid = self.make_campaign()
        with mock.patch.dict(os.environ, {"EMAIL_MODE": "dry"}):
            page = self.post(f"/email-campaigns/{cid}/action", {"action": "launch", "confirm_text": "1"}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Live sending is not switched on yet", page)
        with mock.patch.dict(os.environ, LIVE_ENV):
            page = self.post(f"/email-campaigns/{cid}/action", {"action": "launch", "confirm_text": "yes"}, follow_redirects=True).get_data(as_text=True)
            self.assertIn("type the number of recipients (1)", page)
        self.assertEqual(self.conn.execute("SELECT status FROM email_campaigns").fetchone()[0], "draft")

    def test_a_confirmed_launch_queues_but_does_not_send_from_the_web_request(self):
        cid = self.make_campaign()
        t = FakeTransport()
        with mock.patch.dict(os.environ, LIVE_ENV), mock.patch.object(worker.sender, "get_transport", return_value=t):
            page = self.post(f"/email-campaigns/{cid}/action", {"action": "launch", "confirm_text": "1"}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Launched: 1 recipients queued", page)
        self.assertEqual(t.sent, [])                                                              # sending belongs to the worker, not the page
        self.assertEqual(self.conn.execute("SELECT status FROM email_campaigns").fetchone()[0], "queued")
        row = self.conn.execute("SELECT * FROM email_audit_dummy", ()) if False else None
        audit = [r["action"] for r in self.conn.execute("SELECT action FROM admin_audit_log")]
        self.assertIn("campaign_launched", audit)

    def test_scheduling_is_done_in_ist_and_stored_as_utc(self):
        cid = self.make_campaign()
        from datetime import timedelta
        day = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
        when = day + "T10:00"                                                                      # 10:00 IST = 04:30 UTC
        with mock.patch.dict(os.environ, LIVE_ENV):
            page = self.post(f"/email-campaigns/{cid}/action", {"action": "launch", "confirm_text": "1", "schedule_at": when}, follow_redirects=True).get_data(as_text=True)
        self.assertIn(f"Scheduled for {day}T04:30:00", page)
        self.assertEqual(self.conn.execute("SELECT status, scheduled_at FROM email_campaigns").fetchone()[1], f"{day}T04:30:00+00:00")

    def test_a_launched_campaign_offers_controls_and_no_edit(self):
        cid = self.make_campaign()
        self.launch_and_send(cid)
        page = self.client.get(f"/email-campaigns/{cid}").get_data(as_text=True)
        self.assertNotIn(f"/email-campaigns/{cid}/edit", page)
        self.assertIn("Configuration recorded at launch", page)
        self.assertIn("Progress:", page)
        edit = self.client.get(f"/email-campaigns/{cid}/edit", follow_redirects=True).get_data(as_text=True)
        self.assertIn("Only a draft campaign can be edited", edit)

    def test_unknown_ids_are_404(self):
        for url in ("/email-campaigns/9999", "/email-campaigns/messages/9999", "/email-campaigns/9999/edit", "/email-campaigns/9999/preview.html"):
            self.assertEqual(self.client.get(url).status_code, 404, url)


class MessageDetail(WebCase):
    def setUp(self):
        super().setUp()
        self.seed([csp("D1", name="Detail One", email="detail@example.com", gps=False)])
        self.cid = self.make_campaign()
        self.launch_and_send(self.cid)
        self.rid = self.rid(self.cid, "D1")
        self._login()

    def test_the_detail_page_has_recipient_content_attempts_events_and_the_why(self):
        mid = self.conn.execute("SELECT smtp_message_id FROM email_campaign_recipients WHERE id=?", (self.rid,)).fetchone()[0]
        tracking.ingest_provider_events(self.conn, [{"message_id": mid, "event": "delivered", "id": "x", "timestamp": "2026-10-22T10:05:00+00:00"}])
        self.conn.commit()
        page = self.client.get(f"/email-campaigns/messages/{self.rid}").get_data(as_text=True)
        for text in ("detail@example.com", "Detail One", "Accepted", mid.strip("<>")[:20], "Confirmed delivered", "Send attempts and retries",
                     "accepted by the mail server", "provider", "Eligibility checks", "Valid email address", "Why this operator was chosen"):
            self.assertIn(text, page)
        self.assertIn(f"/email-campaigns/messages/{self.rid}/body.html", page)

    def test_viewing_a_message_never_counts_as_an_open(self):
        body = self.client.get(f"/email-campaigns/messages/{self.rid}/body.html")
        self.assertEqual(body.status_code, 200)
        self.assertIn("img-src 'none'", body.headers["Content-Security-Policy"])
        self.assertIn("/c/o/", body.get_data(as_text=True))                                       # the pixel is in the stored mail ...
        self.assertEqual(self.conn.execute("SELECT open_count FROM email_campaign_recipients WHERE id=?", (self.rid,)).fetchone()[0], 0)   # ... but never fetched

    def test_the_json_api_matches_the_page(self):
        j = self.client.get(f"/api/email-campaigns/messages/{self.rid}").get_json()
        self.assertTrue(j["ok"])
        self.assertEqual((j["message"]["csp_code"], j["message"]["status_key"]), ("D1", "accepted"))
        self.assertEqual(self.client.get("/api/email-campaigns/messages/99999").status_code, 404)


class JsonApi(WebCase):
    def setUp(self):
        super().setUp()
        self.seed([csp(f"J{i}", name=f"Json {i}", email=f"j{i}@example.com", gps=False) for i in range(1, 8)])
        self.cid = self.make_campaign(name="Json campaign")
        self.launch_and_send(self.cid)
        self._login()

    def test_summary_campaigns_messages_progress_and_audience(self):
        s = self.client.get("/api/email-campaigns/summary").get_json()
        self.assertEqual((s["summary"]["emails_accepted"], s["summary"]["delivered_confirmed"], s["summary"]["campaigns"]), (7, 0, 1))
        self.assertNotIn("password", json.dumps(s).lower())
        c = self.client.get("/api/email-campaigns/campaigns").get_json()["campaigns"]
        self.assertEqual((c[0]["name"], c[0]["accepted"], c[0]["status"]), ("Json campaign", 7, "completed"))
        self.assertNotIn("body", c[0])
        m = self.client.get("/api/email-campaigns/messages?per_page=3&page=2&q=json").get_json()
        self.assertEqual((m["total"], m["pages"], m["page"], len(m["rows"])), (7, 3, 2, 3))
        self.assertTrue(all("***" in r["email_masked"] for r in m["rows"]))
        p = self.client.get(f"/api/email-campaigns/{self.cid}/progress").get_json()
        self.assertEqual((p["status"], p["progress"]["percent"]), ("completed", 100))
        a = self.client.get(f"/api/email-campaigns/{self.cid}/audience?bucket=eligible").get_json()
        self.assertEqual((a["counts"]["eligible"], a["total"]), (7, 7))

    def test_preview_and_actions_over_the_api(self):
        draft = self.make_campaign(name="Another", occasion="Holi")
        pv = self.client.get(f"/api/email-campaigns/{draft}/preview").get_json()
        self.assertTrue(pv["ok"])
        self.assertIn("Json 1", pv["text"])
        dry = self.post(f"/api/email-campaigns/{draft}/dry_run").get_json()
        self.assertTrue(dry["ok"])
        self.assertEqual(dry["data"]["would_send"], 7)
        bad = self.post(f"/api/email-campaigns/{draft}/launch", {"confirm_text": "7"})
        self.assertEqual(bad.status_code, 400)
        self.assertIn("not switched on", bad.get_json()["error"])
        self.assertEqual(self.post(f"/api/email-campaigns/{draft}/nonsense").status_code, 400)
        self.assertEqual(self.client.get("/api/email-campaigns/9999/progress").status_code, 404)


class PublicLinks(WebCase):
    def setUp(self):
        super().setUp()
        self.seed([csp("L1", name="Link One", email="link1@example.com", gps=False), csp("L2", name="Link Two", email="link2@example.com", gps=False)])
        self.cid = self.make_campaign(link_url="https://example.org/offer", link_label="See offer")
        self.launch_and_send(self.cid)
        self.t1 = self.conn.execute("SELECT token FROM email_campaign_recipients WHERE csp_code='L1'").fetchone()[0]
        self.t2 = self.conn.execute("SELECT token FROM email_campaign_recipients WHERE csp_code='L2'").fetchone()[0]

    def rec(self, code):
        return self.conn.execute("SELECT * FROM email_campaign_recipients WHERE csp_code=?", (code,)).fetchone()

    def test_open_pixel_returns_a_gif_to_anyone_and_records_a_signal_only_for_a_sent_message(self):
        r = self.client.get(f"/c/o/{self.t1}.gif")
        self.assertEqual((r.status_code, r.mimetype), (200, "image/gif"))
        self.assertEqual(self.rec("L1")["open_count"], 1)
        self.assertEqual(self.client.get("/c/o/not-a-token.gif").status_code, 200)               # reveals nothing about which tokens exist
        self.assertEqual(self.rec("L2")["open_count"], 0)

    def test_the_click_link_only_ever_goes_to_the_campaigns_own_link(self):
        r = self.client.get(f"/c/l/{self.t1}?next=https://evil.example")
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "https://example.org/offer"))
        self.assertEqual(self.rec("L1")["click_count"], 1)
        self.assertEqual(self.client.get("/c/l/bogus").status_code, 404)

    def test_a_campaign_without_a_link_has_no_click_redirect(self):
        self.conn.execute("UPDATE email_campaigns SET link_url=NULL")
        self.conn.commit()
        self.assertEqual(self.client.get(f"/c/l/{self.t1}").status_code, 404)

    def test_opening_the_unsubscribe_link_does_not_unsubscribe_only_confirming_does(self):
        page = self.client.get(f"/c/u/{self.t1}").get_data(as_text=True)
        self.assertIn("Stop these emails?", page)
        self.assertIn("l***@example.com", page)
        self.assertNotIn("link1@example.com", page)
        self.assertFalse(tracking.is_suppressed(self.conn, "link1@example.com"))
        done = self.client.post(f"/c/u/{self.t1}").get_data(as_text=True)                        # also what a one-click mail client sends
        self.assertIn("You have been unsubscribed", done)
        self.assertTrue(tracking.is_suppressed(self.conn, "link1@example.com"))
        self.assertFalse(tracking.is_suppressed(self.conn, "link2@example.com"))
        self.assertEqual(self.client.get("/c/u/bogus").status_code, 404)
        self.assertEqual(self.client.post("/c/u/bogus").status_code, 404)

    def test_the_public_links_need_no_login_and_no_csrf_but_are_rate_limited(self):
        self.assertEqual(self.client.post(f"/c/u/{self.t2}").status_code, 200)
        from security import campaign_public_limiter
        with mock.patch.object(campaign_public_limiter, "allow", return_value=False):
            self.assertEqual(self.client.get(f"/c/u/{self.t1}").status_code, 429)
            self.assertEqual(self.client.get(f"/c/l/{self.t1}").status_code, 429)
            self.assertEqual(self.client.get(f"/c/o/{self.t1}.gif").status_code, 200)             # still a harmless image
            self.assertEqual(self.rec("L1")["open_count"], 0)

    def test_the_footer_link_in_the_stored_email_points_at_the_configured_dashboard_address(self):
        text = self.rec("L1")["body_text"]
        self.assertIn(f"https://dash.example.org/csp-device-heartbeat/c/u/{self.t1}", text)


class ProviderEvents(WebCase):
    URL = "/api/email-campaigns/events"

    def setUp(self):
        super().setUp()
        self.seed([csp("E1", email="ev1@example.com", gps=False)])
        cid = self.make_campaign()
        self.launch_and_send(cid)
        self.mid = self.conn.execute("SELECT smtp_message_id FROM email_campaign_recipients").fetchone()[0]

    def post_events(self, events, key="secret-key", headers=None):
        h = {"X-Campaign-Events-Key": key} if key is not None else {}
        return self.client.post(self.URL, json={"events": events}, headers=dict(h, **(headers or {})))

    def test_the_endpoint_does_not_exist_until_a_key_is_configured(self):
        with mock.patch.dict(os.environ, {"CAMPAIGN_EVENTS_KEY": ""}):
            self.assertEqual(self.post_events([]).status_code, 404)

    def test_a_wrong_or_missing_key_is_rejected_and_changes_nothing(self):
        with mock.patch.dict(os.environ, {"CAMPAIGN_EVENTS_KEY": "secret-key"}):
            self.assertEqual(self.post_events([{"message_id": self.mid, "event": "delivered"}], key="wrong").status_code, 401)
            self.assertEqual(self.post_events([{"message_id": self.mid, "event": "delivered"}], key=None).status_code, 401)
        self.assertEqual(self.conn.execute("SELECT delivery_status FROM email_campaign_recipients").fetchone()[0], "not_confirmed")

    def test_a_valid_post_updates_exactly_the_matching_message(self):
        with mock.patch.dict(os.environ, {"CAMPAIGN_EVENTS_KEY": "secret-key"}):
            r = self.post_events([{"message_id": self.mid, "event": "delivered", "id": "p1", "timestamp": "2026-10-22T10:09:00+00:00"},
                                  {"message_id": "<unknown@x>", "event": "delivered"}])
        j = r.get_json()
        self.assertEqual((r.status_code, j["matched"], j["unmatched"]), (200, 1, 1))
        self.assertEqual(self.conn.execute("SELECT delivery_status FROM email_campaign_recipients").fetchone()[0], "delivered")

    def test_a_hard_bounce_over_the_api_stops_future_mail_to_that_address(self):
        with mock.patch.dict(os.environ, {"CAMPAIGN_EVENTS_KEY": "secret-key"}):
            self.post_events([{"message_id": self.mid, "event": "bounce", "type": "hard", "id": "b"}])
        self.assertTrue(tracking.is_suppressed(self.conn, "ev1@example.com"))

    def test_junk_bodies_are_a_400_not_a_crash(self):
        with mock.patch.dict(os.environ, {"CAMPAIGN_EVENTS_KEY": "secret-key"}):
            r = self.client.post(self.URL, data="not json", headers={"X-Campaign-Events-Key": "secret-key", "Content-Type": "application/json"})
            self.assertEqual(r.status_code, 400)
            r = self.client.post(self.URL, json={"events": "nope"}, headers={"X-Campaign-Events-Key": "secret-key"})
            self.assertEqual(r.status_code, 400)


class OperatorsPage(WebCase):
    def csps_file(self, rows):
        return (io.BytesIO(json.dumps({"importedAt": "2026-10-22T00:00:00+00:00", "csps": rows}).encode()), "csps.json")

    def test_importing_a_file_larger_than_the_normal_upload_limit_works(self):
        self._login()
        rows = [dict(csp(f"BIG{i:04d}", name="N" * 120, email=f"big{i}@example.com", gps=False), padding="x" * 300) for i in range(1500)]
        data = self.csps_file(rows)
        size = len(data[0].getvalue())
        self.assertGreater(size, 256 * 1024)                                                      # bigger than the app's global cap
        r = self.post("/email-campaigns/operators/import", {"csps_file": data}, content_type="multipart/form-data", follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Imported 1500 operators", r.get_data(as_text=True))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM campaign_operators").fetchone()[0], 1500)

    def test_the_global_cap_still_protects_every_other_upload(self):
        self._login()
        token = self.token()
        r = self.client.post("/email-campaigns/operators/reconcile", data={"csrf_token": token, "note": "x" * (300 * 1024)})
        self.assertEqual(r.status_code, 413)

    def test_bad_files_are_explained(self):
        self._login()
        r = self.post("/email-campaigns/operators/import", {"csps_file": (io.BytesIO(b"not json"), "csps.json")}, content_type="multipart/form-data", follow_redirects=True)
        self.assertIn("not valid JSON", r.get_data(as_text=True))
        r = self.post("/email-campaigns/operators/import", {"csps_file": (io.BytesIO(b'{"hello": 1}'), "csps.json")}, content_type="multipart/form-data", follow_redirects=True)
        self.assertIn("does not look like", r.get_data(as_text=True))
        r = self.post("/email-campaigns/operators/import", {}, content_type="multipart/form-data", follow_redirects=True)
        self.assertIn("Choose the CSP Growth Platform", r.get_data(as_text=True))

    def test_import_with_the_human_decisions_file_and_the_ownership_table(self):
        self._login()
        state = {"reconciliations": {"O1:GPS": {"ownsDevice": False, "by": "RM", "at": "2026-10-01", "note": "phoned"}}}
        r = self.post("/email-campaigns/operators/import", {"csps_file": self.csps_file([csp("O1", gps=None), csp("O2", gps=True)]),
                                                            "state_file": (io.BytesIO(json.dumps(state).encode()), "state.json")},
                      content_type="multipart/form-data", follow_redirects=True)
        page = r.get_data(as_text=True)
        self.assertIn("Imported 2 operators", page)
        self.assertEqual(ops.device_map(self.conn)["O1"]["GPS"]["verdict"], "GAP_VERIFIED")     # the human decision won
        self.assertIn('Only "confirmed missing" counts', page)
        self.assertIn("Human ownership decisions", page)

    def test_recording_a_decision_and_managing_opt_outs(self):
        self._login()
        self.seed([csp("O3", gps=False)])
        r = self.post("/email-campaigns/operators/reconcile", {"csp_code": "o3", "device": "GPS", "owns": "yes", "note": "has one"}, follow_redirects=True)
        self.assertIn("A human decision overrides", r.get_data(as_text=True))
        self.assertEqual(ops.device_map(self.conn)["O3"]["GPS"]["verdict"], "OWNED")
        self.assertIn("not in the operator data", self.post("/email-campaigns/operators/reconcile", {"csp_code": "NOPE", "device": "GPS", "owns": "no"}, follow_redirects=True).get_data(as_text=True))
        self.assertIn("Say whether", self.post("/email-campaigns/operators/reconcile", {"csp_code": "O3", "device": "GPS"}, follow_redirects=True).get_data(as_text=True))
        self.assertIn("never be emailed", self.post("/email-campaigns/operators/suppress", {"email": "Gone@Example.com"}, follow_redirects=True).get_data(as_text=True))
        self.assertTrue(tracking.is_suppressed(self.conn, "gone@example.com"))
        page = self.client.get("/email-campaigns/operators").get_data(as_text=True)
        self.assertIn("g***@example.com", page)
        self.assertNotIn("gone@example.com", page)
        self.assertIn("Enter a valid email", self.post("/email-campaigns/operators/suppress", {"email": "nope"}, follow_redirects=True).get_data(as_text=True))
        rid = self.conn.execute("SELECT rowid FROM email_suppressions WHERE email='gone@example.com'").fetchone()[0]
        self.assertIn("removed from the opt-out list", self.post("/email-campaigns/operators/suppress", {"suppression_id": str(rid), "action": "remove"}, follow_redirects=True).get_data(as_text=True))
        self.assertFalse(tracking.is_suppressed(self.conn, "gone@example.com"))
        audit = [r["action"] for r in self.conn.execute("SELECT action FROM admin_audit_log")]
        for a in ("ownership_reconciled", "email_suppression_added", "email_suppression_removed"):
            self.assertIn(a, audit)


class SubPath(WebCase):
    def test_pages_keep_the_prefix_behind_nginx(self):
        self._login()
        self.seed([csp("Z1", gps=False)])
        h = {"X-Forwarded-Prefix": "/csp-device-heartbeat"}
        for url in ("/email-campaigns", "/email-campaigns/operators", "/email-campaigns/new"):
            html = self.client.get(url, headers=h).get_data(as_text=True)
            local = re.findall(r'(?:href|action|src)="(/[^"]*)"', html)
            bad = [u for u in local if not u.startswith("/csp-device-heartbeat/")]
            self.assertFalse(bad, (url, bad))


if __name__ == "__main__":
    unittest.main()
