"""Email campaigns: ownership classification, eligibility, safety rules, sending, retries, events, restarts.
No test ever contacts a real mail server: sending goes through a fake transport."""
import json
import os
import smtplib
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import db
from campaigns import eligibility, operators as ops, queries, rendering, sender, service, tracking, worker
from tests.helpers import make_test_app

NOW = datetime(2026, 10, 22, 10, 0, 0, tzinfo=timezone.utc)
LIVE_ENV = {"EMAIL_MODE": "live", "CAMPAIGNS_LIVE_ENABLED": "1", "CAMPAIGN_PUBLIC_BASE_URL": "https://dash.example.org/csp-device-heartbeat",
            "SMTP_HOST": "smtp.invalid", "CAMPAIGN_SEND_DELAY_SECONDS": "0", "EMAIL_MAX_PER_RUN": "25", "EMAIL_FROM": "Eko <csp@example.org>"}


def csp(code, name="Operator", email="op@example.com", matm=True, printer=True, gps=True, txn=100, matm_txn=50, prints=10, active=True,
        rm="RM One", state="Bihar", district="Patna"):
    return {"cspCode": code, "name": name, "email": email, "mobile": "9000000000", "rm": rm, "state": state, "district": district,
            "claimsMatm": matm, "claimsPrinter": printer, "claimsGps": gps, "terminalStatus": "Active" if active else "Inactive",
            "inactivityDays": 1, "txnPrev": txn // 2, "txnThis": txn - txn // 2, "matmTxnPrev": matm_txn // 2, "matmTxnThis": matm_txn - matm_txn // 2,
            "pbPrintsPrev": prints // 2, "pbPrintsThis": prints - prints // 2, "contactable": True}


def base_fields(**kw):
    f = {"name": "Diwali greetings", "description": "d", "occasion": "Dhanteras / Diwali", "devices": ["GPS"], "audience_match": "any",
         "subject": "Happy {occasion}, {first_name}", "body": "Dear {name},\n\nYou do not yet have {missing_devices}.", "link_url": "", "link_label": ""}
    f.update(kw)
    return f


class FakeTransport:
    """Records what would be sent; can be told to fail."""
    def __init__(self, fail=None):
        self.sent, self.opened, self.closed, self.fail = [], 0, 0, fail or {}

    def open(self):
        self.opened += 1
        if self.fail.get("open"):
            raise self.fail["open"]
        return self

    def send(self, to, subject, html, text, headers=None):
        if to in self.fail:
            raise self.fail[to]
        self.sent.append({"to": to, "subject": subject, "html": html, "text": text, "headers": headers or {}})
        return f"<msg-{len(self.sent)}-{to}@test>"

    def close(self):
        self.closed += 1


class CampaignCase(unittest.TestCase):
    def setUp(self):
        self.app, self.path = make_test_app()
        self.conn = db.get_connection()
        self.addCleanup(self.conn.close)

    def load(self, rows, recon=None):
        r = ops.import_operators(self.conn, {"csps": rows, "importedAt": "2026-10-22T00:00:00+00:00"}, "tester", "test.json", recon)
        self.conn.commit()
        return r

    def campaign(self, **kw):
        cid = service.create_campaign(self.conn, service.validate_fields(base_fields(**kw)), "tester", NOW)
        self.conn.commit()
        return cid

    def go_live(self, cid, extra_env=None):
        env = dict(LIVE_ENV, **(extra_env or {}))
        with mock.patch.dict(os.environ, env):
            aud = service.materialize(self.conn, cid, NOW)
            out = service.launch(self.conn, cid, "tester", str(aud["counts"]["eligible"]), now=NOW)
        self.conn.commit()
        return out

    def send_batch(self, cid, transport, env=None, now=NOW, limit=None):
        with mock.patch.dict(os.environ, dict(LIVE_ENV, **(env or {}))):
            r = worker.run_batch(self.conn, cid, transport, limit=limit, now=now, sleep=lambda s: None)
        self.conn.commit()
        return r

    def rec(self, cid, code):
        return self.conn.execute("SELECT * FROM email_campaign_recipients WHERE campaign_id=? AND csp_code=?", (cid, code)).fetchone()

    def agent(self, code, **configured):
        cols = {"microatm_configured": 0, "printer_configured": 0, "gps_configured": 0, "biometric_configured": 0}
        cols.update({f"{k}_configured": v for k, v in configured.items()})
        self.conn.execute(
            "INSERT INTO csps (csp_id, name, last_seen, microatm_configured, printer_configured, gps_configured, biometric_configured) VALUES (?,?,?,?,?,?,?)",
            (code, code, "2026-10-22T00:00:00+00:00", cols["microatm_configured"], cols["printer_configured"], cols["gps_configured"], cols["biometric_configured"]))
        self.conn.commit()
        ops.refresh_ownership(self.conn)
        self.conn.commit()

    def verdicts(self, code):
        return {d: v["verdict"] for d, v in ops.device_map(self.conn).get(code, {}).items()}


class Ownership(CampaignCase):
    def test_operators_with_all_four_devices_are_classified_as_owning_them(self):
        self.load([csp("A1")])
        self.agent("A1", biometric=1)                                   # the sheet has no biometric field: the monitoring agent is the witness
        self.assertEqual(self.verdicts("A1"), {"MATM": "OWNED", "PRINTER": "OWNED", "GPS": "OWNED", "BIOMETRIC": "OWNED"})

    def test_an_operator_with_some_devices_is_not_assumed_to_lack_the_rest(self):
        self.load([csp("B1", matm=True, printer=False, gps=None)])
        v = self.verdicts("B1")
        self.assertEqual((v["MATM"], v["PRINTER"], v["GPS"], v["BIOMETRIC"]), ("OWNED", "GAP_VERIFIED", "UNKNOWN", "UNKNOWN"))

    def test_an_operator_with_no_devices_is_a_confirmed_gap_only_where_ownership_is_confirmed(self):
        self.load([csp("C1", matm=False, printer=False, gps=False, matm_txn=0, prints=0)],
                  recon={"C1:BIOMETRIC": {"ownsDevice": False, "by": "RM", "at": "2026-10-01", "note": "phoned"}})
        self.assertEqual(self.verdicts("C1"), {"MATM": "GAP_VERIFIED", "PRINTER": "GAP_VERIFIED", "GPS": "GAP_VERIFIED", "BIOMETRIC": "GAP_VERIFIED"})

    def test_missing_data_is_unknown_never_absent(self):
        self.load([csp("D1", matm=None, printer=None, gps=None)])
        self.assertEqual(set(self.verdicts("D1").values()), {"UNKNOWN"})

    def test_conflicting_evidence_is_disputed_not_absent(self):
        # the sheet says "No mATM" but 80 mATM transactions were recorded (the email project's own rule)
        self.load([csp("E1", matm=False, matm_txn=80), csp("E2", gps=False)])
        self.assertEqual(self.verdicts("E1")["MATM"], "GAP_DISPUTED")
        self.agent("E2", gps=1)                                         # sheet says no GPS, the agent has one configured
        self.assertEqual(self.verdicts("E2")["GPS"], "GAP_DISPUTED")

    def test_a_human_reconciliation_beats_everything(self):
        self.load([csp("F1", gps=False)])
        self.agent("F1", gps=1)
        self.assertEqual(self.verdicts("F1")["GPS"], "GAP_DISPUTED")
        ops.set_reconciliation(self.conn, "F1", "GPS", False, "rm", "phoned: no dongle")
        self.conn.commit()
        self.assertEqual(self.verdicts("F1")["GPS"], "GAP_VERIFIED")

    def test_the_vendored_engine_matches_the_email_projects_rules(self):
        # Facts taken from the email project's own documentation / config: claims decide, activity needs transactions
        self.load([csp("G1", txn=5)])
        op = self.conn.execute("SELECT activity FROM campaign_operators WHERE csp_code='G1'").fetchone()
        self.assertEqual(op["activity"], "DORMANT")                      # below MIN_TRANSACTIONS_TO_PITCH (20)


class Eligibility(CampaignCase):
    def setUp(self):
        super().setUp()
        self.load([
            csp("OK1", name="Gps Less", gps=False),
            csp("OWN", name="Has Gps", gps=True),
            csp("UNK", name="Unknown Gps", gps=None),
            csp("INACT", name="Inactive", gps=False, active=False),
            csp("NOMAIL", name="No Mail", gps=False, email=""),
            csp("BADMAIL", name="Bad Mail", gps=False, email="not-an-email"),
            csp("OPTOUT", name="Opted Out", gps=False, email="gone@example.com"),
            csp("DUP", name="Same Address A", gps=False, email="shared@example.com"),
            csp("DUP2", name="Same Address B", gps=False, email="shared@example.com"),
        ])
        tracking.suppress(self.conn, "gone@example.com", "OPTOUT", "unsubscribed")
        self.conn.commit()

    def audience(self, **kw):
        cfg = eligibility.clean_config(kw.pop("devices", ["GPS"]), kw.pop("match", "any"), kw.pop("dormant", False))
        return {r["csp_code"]: r for r in eligibility.build_audience(self.conn, cfg)["rows"]}

    def test_only_eligible_operators_are_in_the_audience(self):
        a = self.audience()
        self.assertEqual({c for c, r in a.items() if r["bucket"] == "eligible"}, {"OK1", "DUP"})
        self.assertEqual(a["OWN"]["bucket"], "excluded")
        self.assertEqual(a["UNK"]["bucket"], "review")                   # unknown ownership is flagged, not treated as absent

    def test_invalid_and_opted_out_recipients_are_excluded_with_a_reason(self):
        a = self.audience()
        self.assertIn("No valid email", a["NOMAIL"]["reason"])
        self.assertIn("not valid", a["BADMAIL"]["reason"])
        self.assertIn("opted out", a["OPTOUT"]["reason"])
        self.assertIn("Not an active operator", a["INACT"]["reason"])
        self.assertIn("already used for operator DUP", a["DUP2"]["reason"])

    def test_disputed_ownership_is_held_for_review_never_emailed(self):
        # sheet says no GPS, but the monitoring agent has a GPS configured: conflicting evidence
        self.load([csp("CONF", name="Conflicting", gps=False, email="conf@example.com")])
        self.agent("CONF", gps=1)
        a = self.audience()
        self.assertEqual(self.verdicts("CONF")["GPS"], "GAP_DISPUTED")
        self.assertEqual(a["CONF"]["bucket"], "review")
        self.assertEqual(a["CONF"]["missing"], [])
        self.assertIn("Ownership not confirmed", a["CONF"]["reason"])

    def test_every_decision_records_why(self):
        for r in self.audience().values():
            self.assertTrue(r["reason"])
            self.assertTrue(r["checks"])

    def test_dormant_operators_are_opt_in(self):
        self.load([csp("DORM", gps=False, txn=3)])
        self.assertEqual(self.audience()["DORM"]["bucket"], "excluded")
        self.assertEqual(self.audience(dormant=True)["DORM"]["bucket"], "eligible")

    def test_match_all_needs_every_selected_device_confirmed_absent(self):
        self.load([csp("M1", matm=False, gps=False, matm_txn=0, email="m1@example.com"), csp("M2", matm=True, gps=False, email="m2@example.com")])
        a = self.audience(devices=["MATM", "GPS"], match="all")
        self.assertEqual(a["M1"]["bucket"], "eligible")
        self.assertEqual(a["M2"]["bucket"], "excluded")                  # owns a micro-ATM
        b = self.audience(devices=["MATM", "GPS"], match="any")
        self.assertEqual(b["M2"]["bucket"], "eligible")
        self.assertEqual(b["M2"]["missing"], ["GPS"])                    # only the confirmed-absent device is named

    def test_a_confirmed_gap_plus_an_unknown_one_is_eligible_but_the_unknown_is_not_mentioned(self):
        self.load([csp("N1", matm=False, matm_txn=0, gps=None, email="n1@example.com")])
        r = self.audience(devices=["MATM", "GPS"])["N1"]
        self.assertEqual((r["bucket"], r["missing"], r["review"]), ("eligible", ["MATM"], ["GPS"]))

    def test_an_operator_dropped_from_a_newer_import_is_never_emailed_but_kept(self):
        self.load([csp("OK1", gps=False)])                               # a newer file with only one operator
        a = self.audience()
        self.assertEqual(a["OK1"]["bucket"], "eligible")
        self.assertEqual(a["DUP"]["bucket"], "excluded")
        self.assertIn("No longer in the latest", a["DUP"]["reason"])

    def test_the_festival_audience_does_not_touch_the_device_adoption_state(self):
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse({"outreach", "tokens", "orders"} & tables)       # nothing of the sales campaign's state exists here


class Safety(CampaignCase):
    def setUp(self):
        super().setUp()
        self.load([csp("S1", name="Sita Devi", email="sita@example.com", gps=False), csp("S2", name="Ram", email="ram@example.com", gps=False)])
        self.cid = self.campaign()

    def test_dry_run_never_sends_and_never_changes_a_recipient(self):
        with mock.patch.object(sender, "get_transport", side_effect=AssertionError("a dry run must not open a transport")), \
                mock.patch("smtplib.SMTP", side_effect=AssertionError("no network")), mock.patch("smtplib.SMTP_SSL", side_effect=AssertionError("no network")):
            out = service.dry_run(self.conn, self.cid, "tester", NOW)
        self.conn.commit()
        self.assertEqual(out["would_send"], 2)
        self.assertEqual({r["status"] for r in self.conn.execute("SELECT status FROM email_campaign_recipients")}, {"pending"})
        self.assertEqual({r["outcome"] for r in self.conn.execute("SELECT outcome FROM email_attempts")}, {"dry_run"})
        self.assertEqual(queries.summary(self.conn)["emails_accepted"], 0)

    def test_live_is_refused_unless_every_switch_is_on(self):
        for env in ({}, {"EMAIL_MODE": "live"}, {"EMAIL_MODE": "live", "CAMPAIGNS_LIVE_ENABLED": "1"}):
            with mock.patch.dict(os.environ, env, clear=True), self.assertRaises(service.CampaignError) as cm:
                service.launch(self.conn, self.cid, "tester", "2", now=NOW)
            self.assertIn("not switched on", str(cm.exception))
        self.assertEqual(service.get_campaign(self.conn, self.cid)["status"], "draft")

    def test_live_needs_the_typed_recipient_count(self):
        with mock.patch.dict(os.environ, LIVE_ENV):
            for wrong in ("", "yes", "3", "1"):
                with self.assertRaises(service.CampaignError) as cm:
                    service.launch(self.conn, self.cid, "tester", wrong, now=NOW)
                self.assertIn("type the number of recipients (2)", str(cm.exception))
        self.assertEqual(service.get_campaign(self.conn, self.cid)["status"], "draft")

    def test_launching_twice_does_not_queue_twice(self):
        self.go_live(self.cid)
        with mock.patch.dict(os.environ, LIVE_ENV), self.assertRaises(service.CampaignError):
            service.launch(self.conn, self.cid, "tester", "2", now=NOW)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM email_campaign_recipients WHERE status='queued'").fetchone()[0], 2)

    def test_launch_records_who_when_and_the_exact_configuration(self):
        self.go_live(self.cid)
        c = service.get_campaign(self.conn, self.cid)
        snap = json.loads(c["config_snapshot_json"])
        self.assertEqual((c["status"], c["launched_by"], c["mode"]), ("queued", "tester", "live"))
        self.assertEqual((snap["config"]["devices"], snap["confirmed_recipients"]), (["GPS"], 2))
        audit = [r["action"] for r in self.conn.execute("SELECT action FROM admin_audit_log")]
        self.assertIn("campaign_launched", audit)

    def test_a_launched_campaign_cannot_be_edited_or_re_audienced(self):
        self.go_live(self.cid)
        with self.assertRaises(service.CampaignError):
            service.update_campaign(self.conn, self.cid, service.validate_fields(base_fields(name="changed")), "tester")
        with self.assertRaises(service.CampaignError):
            service.materialize(self.conn, self.cid)

    def test_an_unknown_placeholder_or_markup_is_rejected_or_escaped(self):
        with self.assertRaises(rendering.TemplateError):
            service.validate_fields(base_fields(body="Hello {secret_field}"))
        with self.assertRaises(rendering.TemplateError):
            service.validate_fields(base_fields(link_url="http://insecure.example"))
        with self.assertRaises(rendering.TemplateError):
            service.validate_fields(base_fields(link_url="javascript:alert(1)"))
        c = service.get_campaign(self.conn, self.cid)
        out = rendering.render(dict(c, body="Hi {name}\n\n<script>alert(1)</script>"), rendering.context_for(c, "A <b>B</b>", "X", "RM", ["GPS dongle"]), "T", "https://x.org")
        self.assertNotIn("<script>", out["html"])
        self.assertNotIn("\n", out["subject"])

    def test_a_test_email_is_not_sent_in_dry_mode_and_is_never_a_recipient(self):
        t = FakeTransport()
        with mock.patch.dict(os.environ, {"EMAIL_MODE": "dry"}):
            out = service.send_test(self.conn, self.cid, "me@example.com", "tester", transport=t)
        self.assertFalse(out["sent"])
        self.assertEqual(t.sent, [])
        with mock.patch.dict(os.environ, LIVE_ENV):
            out = service.send_test(self.conn, self.cid, "me@example.com", "tester", transport=t)
        self.assertTrue(out["sent"])
        self.assertEqual((len(t.sent), t.sent[0]["subject"][:6]), (1, "[TEST]"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM email_campaign_recipients WHERE status='accepted'").fetchone()[0], 0)
        with self.assertRaises(service.CampaignError):
            service.send_test(self.conn, self.cid, "a@x.com, b@x.com", "tester", transport=t)


class Sending(CampaignCase):
    def setUp(self):
        super().setUp()
        self.load([csp(f"R{i}", name=f"Op {i}", email=f"op{i}@example.com", gps=False) for i in range(1, 6)])
        self.cid = self.campaign()
        self.go_live(self.cid)

    def test_sends_once_records_acceptance_and_never_claims_delivery(self):
        t = FakeTransport()
        r = self.send_batch(self.cid, t)
        self.assertEqual((r["sent"], len(t.sent)), (5, 5))
        row = self.rec(self.cid, "R1")
        self.assertEqual((row["status"], row["delivery_status"]), ("accepted", "not_confirmed"))
        self.assertTrue(row["smtp_message_id"] and row["body_html"] and row["accepted_at"])
        self.assertEqual(queries.display_status(row), "Accepted by mail server")
        s = queries.summary(self.conn)
        self.assertEqual((s["emails_accepted"], s["delivered_confirmed"], s["opened"], s["clicked"]), (5, 0, 0, 0))
        self.assertEqual(service.get_campaign(self.conn, self.cid)["status"], "completed")

    def test_a_message_another_worker_took_in_the_meantime_is_not_sent_again(self):
        class Racing(FakeTransport):
            """While we send the first message, 'another worker' accepts the second one."""
            def __init__(self, path):
                super().__init__()
                self.path, self.done = path, False

            def send(inner, to, subject, html, text, headers=None):
                if not inner.done:
                    inner.done = True
                    other = db.get_connection()
                    other.execute("UPDATE email_campaign_recipients SET status='accepted' WHERE csp_code='R2'")
                    other.commit()
                    other.close()
                return super().send(to, subject, html, text, headers)
        t = Racing(self.path)
        self.send_batch(self.cid, t)
        self.assertNotIn("op2@example.com", [m["to"] for m in t.sent])
        self.assertEqual(len(t.sent), 4)

    def test_running_again_never_resends_an_accepted_message(self):
        t = FakeTransport()
        self.send_batch(self.cid, t)
        self.send_batch(self.cid, t)
        self.assertEqual(len(t.sent), 5)
        self.assertEqual(len({m["to"] for m in t.sent}), 5)

    def test_the_per_run_cap_is_respected(self):
        t = FakeTransport()
        self.send_batch(self.cid, t, env={"EMAIL_MAX_PER_RUN": "2"})
        self.assertEqual(len(t.sent), 2)
        self.assertEqual(service.get_campaign(self.conn, self.cid)["status"], "sending")
        self.send_batch(self.cid, t, env={"EMAIL_MAX_PER_RUN": "2"})
        self.send_batch(self.cid, t, env={"EMAIL_MAX_PER_RUN": "2"})
        self.assertEqual(len(t.sent), 5)
        self.assertEqual(service.get_campaign(self.conn, self.cid)["status"], "completed")

    def test_every_message_carries_unsubscribe_headers_and_link(self):
        t = FakeTransport()
        self.send_batch(self.cid, t)
        m = t.sent[0]
        self.assertIn("/c/u/", m["headers"]["List-Unsubscribe"])
        self.assertEqual(m["headers"]["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")
        self.assertIn("/c/u/", m["text"])

    def test_a_transient_failure_is_retried_later_then_succeeds_without_duplicates(self):
        t = FakeTransport(fail={"op1@example.com": smtplib.SMTPServerDisconnected("timeout")})
        r = self.send_batch(self.cid, t)
        self.assertEqual((r["sent"], r["retry_later"]), (4, 1))
        row = self.rec(self.cid, "R1")
        self.assertEqual((row["status"], row["attempts"]), ("queued", 1))
        self.assertTrue(row["next_attempt_at"])
        t.fail = {}
        self.send_batch(self.cid, t, now=NOW + timedelta(hours=2))
        row = self.rec(self.cid, "R1")
        self.assertEqual((row["status"], row["attempts"]), ("accepted", 2))
        self.assertEqual(len([m for m in t.sent if m["to"] == "op1@example.com"]), 1)
        outcomes = [a["outcome"] for a in self.conn.execute("SELECT outcome FROM email_attempts WHERE recipient_id=? ORDER BY id", (row["id"],))]
        self.assertEqual(outcomes, ["transient_failure", "accepted"])     # the failed attempt is kept, not erased

    def test_retries_stop_after_the_limit_and_are_recorded_as_exhausted(self):
        t = FakeTransport(fail={"op1@example.com": smtplib.SMTPServerDisconnected("down")})
        when = NOW
        for _ in range(4):
            self.send_batch(self.cid, t, now=when, env={"CAMPAIGN_MAX_ATTEMPTS": "3"})
            when += timedelta(days=1)
        row = self.rec(self.cid, "R1")
        self.assertEqual((row["status"], row["failure_kind"], row["attempts"]), ("failed", "transient_exhausted", 3))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM email_attempts WHERE recipient_id=?", (row["id"],)).fetchone()[0], 3)

    def test_a_permanent_failure_is_recorded_at_once_and_not_retried(self):
        err = smtplib.SMTPRecipientsRefused({"op2@example.com": (550, b"no such user")})
        t = FakeTransport(fail={"op2@example.com": err})
        self.send_batch(self.cid, t)
        row = self.rec(self.cid, "R2")
        self.assertEqual((row["status"], row["failure_kind"], row["attempts"]), ("failed", "permanent", 1))
        self.assertIn("550", row["last_error"])
        self.assertEqual(service.retry_failed(self.conn, self.cid, "tester")["requeued"], 0)   # permanent failures are not retried

    def test_retry_only_requeues_exhausted_failures_never_accepted_messages(self):
        t = FakeTransport(fail={"op1@example.com": smtplib.SMTPServerDisconnected("down")})
        when = NOW
        for _ in range(4):
            self.send_batch(self.cid, t, now=when, env={"CAMPAIGN_MAX_ATTEMPTS": "2"})
            when += timedelta(days=1)
        self.assertEqual(self.rec(self.cid, "R1")["status"], "failed")
        t.fail = {}
        out = service.retry_failed(self.conn, self.cid, "tester")
        self.conn.commit()
        self.assertEqual(out["requeued"], 1)
        self.send_batch(self.cid, t, now=when + timedelta(days=1))
        self.assertEqual(len(t.sent), 5)
        self.assertEqual(len({m["to"] for m in t.sent}), 5)                 # nobody got two

    def test_a_bad_login_pauses_the_campaign_without_burning_attempts(self):
        t = FakeTransport(fail={"open": smtplib.SMTPAuthenticationError(535, b"bad credentials")})
        r = self.send_batch(self.cid, t)
        self.assertIn("refused our login", r["paused"])
        self.assertEqual(service.get_campaign(self.conn, self.cid)["status"], "paused")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM email_campaign_recipients WHERE status='queued' AND attempts=0").fetchone()[0], 5)

    def test_an_opt_out_after_launch_stops_that_message(self):
        tracking.suppress(self.conn, "op3@example.com", "R3", "unsubscribed")
        self.conn.commit()
        t = FakeTransport()
        r = self.send_batch(self.cid, t)
        self.assertEqual((r["sent"], r["skipped_opted_out"]), (4, 1))
        self.assertNotIn("op3@example.com", [m["to"] for m in t.sent])
        self.assertEqual(self.rec(self.cid, "R3")["status"], "cancelled")

    def test_pausing_stops_sending_and_cancelling_cancels_unsent_messages(self):
        service.pause(self.conn, self.cid, "tester")
        self.conn.commit()
        t = FakeTransport()
        self.assertIn("skipped", self.send_batch(self.cid, t))
        self.assertEqual(t.sent, [])
        n = service.cancel(self.conn, self.cid, "tester")
        self.conn.commit()
        self.assertEqual(n, 5)
        self.assertEqual(service.get_campaign(self.conn, self.cid)["status"], "cancelled")

    def test_if_live_sending_is_switched_off_the_worker_pauses_instead_of_sending(self):
        t = FakeTransport()
        with mock.patch.dict(os.environ, {"EMAIL_MODE": "dry"}):
            r = worker.run_batch(self.conn, self.cid, t, now=NOW, sleep=lambda s: None)
        self.assertIn("switched off", r["paused"])
        self.assertEqual(t.sent, [])


class Events(CampaignCase):
    def setUp(self):
        super().setUp()
        self.load([csp("V1", email="v1@example.com", gps=False), csp("V2", email="v2@example.com", gps=False)])
        self.cid = self.campaign()
        self.go_live(self.cid)
        self.send_batch(self.cid, FakeTransport())
        self.r1, self.r2 = self.rec(self.cid, "V1"), self.rec(self.cid, "V2")

    def ingest(self, *events):
        out = tracking.ingest_provider_events(self.conn, list(events), NOW)
        self.conn.commit()
        return out

    def test_a_delivery_event_updates_only_the_matching_message(self):
        out = self.ingest({"message_id": self.r1["smtp_message_id"], "event": "delivered", "timestamp": "2026-10-22T10:05:00+00:00", "id": "e1"})
        self.assertEqual(out["matched"], 1)
        self.assertEqual(self.rec(self.cid, "V1")["delivery_status"], "delivered")
        self.assertEqual(self.rec(self.cid, "V2")["delivery_status"], "not_confirmed")
        self.assertEqual(queries.display_status(self.rec(self.cid, "V1")), "Delivered (confirmed)")

    def test_the_same_event_twice_is_applied_once(self):
        ev = {"message_id": self.r1["smtp_message_id"], "event": "delivered", "id": "dup-1", "timestamp": "2026-10-22T10:05:00+00:00"}
        self.ingest(ev)
        self.assertEqual(self.ingest(ev)["duplicate"], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM email_events WHERE recipient_id=? AND kind='delivered'", (self.r1["id"],)).fetchone()[0], 1)

    def test_a_hard_bounce_marks_the_message_and_stops_future_mail_to_that_address(self):
        self.ingest({"message_id": self.r2["smtp_message_id"], "event": "bounced", "type": "hard", "id": "b1"})
        self.assertEqual(self.rec(self.cid, "V2")["delivery_status"], "bounced")
        self.assertTrue(tracking.is_suppressed(self.conn, "v2@example.com"))

    def test_a_soft_bounce_is_recorded_but_does_not_suppress(self):
        self.ingest({"message_id": self.r2["smtp_message_id"], "event": "bounced", "type": "soft", "id": "b2"})
        self.assertEqual(self.rec(self.cid, "V2")["delivery_status"], "bounced")
        self.assertFalse(tracking.is_suppressed(self.conn, "v2@example.com"))

    def test_a_late_delivered_event_cannot_undo_a_bounce(self):
        self.ingest({"message_id": self.r2["smtp_message_id"], "event": "bounced", "type": "hard", "id": "b1"},
                    {"message_id": self.r2["smtp_message_id"], "event": "delivered", "id": "d1"})
        self.assertEqual(self.rec(self.cid, "V2")["delivery_status"], "bounced")

    def test_unknown_message_ids_and_junk_are_ignored_safely(self):
        out = self.ingest({"message_id": "<nobody@test>", "event": "delivered"}, {"event": "delivered"}, "junk", {"message_id": "x", "event": "weird"})
        self.assertEqual((out["matched"], out["unmatched"], out["ignored"]), (0, 1, 3))

    def test_opens_and_clicks_are_counted_separately_and_never_imply_delivery(self):
        self.assertTrue(tracking.track_open(self.conn, self.r1["token"], NOW))
        self.assertTrue(tracking.track_open(self.conn, self.r1["token"], NOW))
        self.conn.commit()
        row = self.rec(self.cid, "V1")
        self.assertEqual((row["open_count"], row["delivery_status"]), (2, "not_confirmed"))
        self.assertEqual(queries.summary(self.conn)["opened"], 1)
        self.assertEqual(queries.summary(self.conn)["delivered_confirmed"], 0)
        self.assertFalse(tracking.track_open(self.conn, "no-such-token"))

    def test_unsubscribing_opts_the_address_out_and_cancels_unsent_mail(self):
        self.load([csp("V3", email="v1@example.com", gps=False)] + [csp("V1", email="v1@example.com", gps=False)])   # (same address, new campaign below)
        c2 = self.campaign(name="Second")
        self.go_live(c2)
        token = self.rec(self.cid, "V1")["token"]
        out = tracking.unsubscribe(self.conn, token, NOW)
        self.conn.commit()
        self.assertEqual(out["email_masked"], "v***@example.com")
        self.assertTrue(tracking.is_suppressed(self.conn, "v1@example.com"))
        self.assertEqual(self.rec(c2, "V1")["status"], "cancelled")
        again = tracking.unsubscribe(self.conn, token, NOW)
        self.assertTrue(again["already"])
        self.assertIsNone(tracking.unsubscribe(self.conn, "bogus", NOW))


class Persistence(CampaignCase):
    def test_reimporting_operator_data_never_erases_campaign_history(self):
        self.load([csp("P1", email="p1@example.com", gps=False), csp("P2", email="p2@example.com", gps=False)])
        cid = self.campaign()
        self.go_live(cid)
        self.send_batch(cid, FakeTransport())
        before = [dict(r) for r in self.conn.execute("SELECT * FROM email_campaign_recipients ORDER BY id")]
        # a re-import: P1's email changed, P2 vanished from the sheet
        self.load([csp("P1", email="new-p1@example.com", name="Renamed", gps=False)])
        after = [dict(r) for r in self.conn.execute("SELECT * FROM email_campaign_recipients ORDER BY id")]
        self.assertEqual(before, after)
        self.assertEqual(self.rec(cid, "P1")["email_at_send"], "p1@example.com")        # what was actually sent to
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM campaign_operators").fetchone()[0], 2)   # P2 kept, flagged
        self.assertEqual(self.conn.execute("SELECT present_in_latest FROM campaign_operators WHERE csp_code='P2'").fetchone()[0], 0)

    def test_restart_marks_a_half_sent_message_for_review_and_never_resends_it(self):
        self.load([csp("Q1", email="q1@example.com", gps=False), csp("Q2", email="q2@example.com", gps=False)])
        cid = self.campaign()
        self.go_live(cid)
        self.conn.execute("UPDATE email_campaign_recipients SET status='sending', attempts=1 WHERE csp_code='Q1' AND campaign_id=?", (cid,))
        self.conn.execute("UPDATE email_campaigns SET status='sending' WHERE id=?", (cid,))
        self.conn.commit()
        self.assertEqual(worker.recover_after_restart(self.conn), 1)
        self.conn.commit()
        self.assertEqual(self.rec(cid, "Q1")["status"], "unknown_outcome")
        t = FakeTransport()
        self.send_batch(cid, t)
        self.assertEqual([m["to"] for m in t.sent], ["q2@example.com"])               # Q1 was NOT sent again
        self.assertEqual(queries.summary(self.conn)["needs_review"], 1)

    def test_a_scheduled_campaign_starts_once_even_if_the_scheduler_runs_twice(self):
        self.load([csp("T1", email="t1@example.com", gps=False)])
        cid = self.campaign()
        with mock.patch.dict(os.environ, LIVE_ENV):
            out = service.launch(self.conn, cid, "tester", "1", schedule_at=(NOW + timedelta(hours=1)).isoformat(), now=NOW)
        self.conn.commit()
        self.assertEqual(out["status"], "scheduled")
        t = FakeTransport()
        factory = lambda: db.get_connection()
        with mock.patch.dict(os.environ, LIVE_ENV):
            self.assertEqual(worker.tick(factory, lambda: t, now=NOW, sleep=lambda s: None), {})          # not due yet: nothing happens
            due = NOW + timedelta(hours=2)
            worker.tick(factory, lambda: t, now=due, sleep=lambda s: None)
            worker.tick(factory, lambda: t, now=due, sleep=lambda s: None)                                # restart / second worker
        self.assertEqual(len(t.sent), 1)

    def test_scheduling_needs_a_future_time(self):
        self.load([csp("U1", email="u1@example.com", gps=False)])
        cid = self.campaign()
        with mock.patch.dict(os.environ, LIVE_ENV), self.assertRaises(service.CampaignError):
            service.launch(self.conn, cid, "tester", "1", schedule_at=(NOW - timedelta(hours=1)).isoformat(), now=NOW)


class Import(CampaignCase):
    def test_a_malformed_or_ambiguous_file_is_handled_safely(self):
        with self.assertRaises(ValueError):
            ops.import_operators(self.conn, {"nope": 1}, "tester")
        rows = [csp("X1"), csp("X1", name="twin"), {"cspCode": "X2"}, "junk", csp("")]
        out = self.load(rows)
        self.assertEqual(out["accepted"], 0)                          # X1 twice = ambiguous identity; X2 lacks counts; blanks skipped
        self.assertEqual(out["skipped_total"], 5)

    def test_codes_are_normalised_so_the_monitoring_agent_and_the_sheet_agree(self):
        self.load([csp(" 1ab 234 ", gps=False)])
        self.assertEqual(self.conn.execute("SELECT csp_code FROM campaign_operators").fetchone()[0], "1AB234")
        self.agent("1ab234", gps=1)
        self.assertEqual(self.verdicts("1AB234")["GPS"], "GAP_DISPUTED")


class History(CampaignCase):
    def setUp(self):
        super().setUp()
        self.load([csp(f"H{i:02d}", name=f"Person {i}", email=f"h{i}@example.com", gps=False, state="Bihar" if i % 2 else "Assam", rm="Asha" if i < 5 else "Ravi")
                   for i in range(1, 31)])
        self.c1 = self.campaign(name="Diwali", occasion="Diwali")
        self.go_live(self.c1)
        self.send_batch(self.c1, FakeTransport(fail={"h3@example.com": smtplib.SMTPRecipientsRefused({"h3@example.com": (550, b"x")})}), env={"EMAIL_MAX_PER_RUN": "30"})
        self.c2 = self.campaign(name="Holi", occasion="Holi", devices=["PRINTER", "GPS"], audience_match="any")
        self.conn.commit()

    def test_the_history_lists_only_real_message_records_with_masked_emails(self):
        out = queries.list_messages(self.conn)
        self.assertEqual(out["total"], 30)
        self.assertTrue(all("***" in r["email_masked"] for r in out["rows"]))
        self.assertNotIn("@example.com", json.dumps(out["rows"]).replace("***@example.com", ""))

    def test_pagination_search_and_filters(self):
        p1 = queries.list_messages(self.conn, {}, 1, 10)
        self.assertEqual((len(p1["rows"]), p1["pages"], p1["total"]), (10, 3, 30))
        self.assertEqual(len({r["id"] for r in queries.list_messages(self.conn, {}, 3, 10)["rows"]} & {r["id"] for r in p1["rows"]}), 0)
        self.assertEqual(queries.list_messages(self.conn, {"q": "person 7"})["total"], 1)
        self.assertEqual(queries.list_messages(self.conn, {"q": "H07"})["total"], 1)
        self.assertEqual(queries.list_messages(self.conn, {"q": "h12@example"})["total"], 1)
        self.assertEqual(queries.list_messages(self.conn, {"status": "failed"})["total"], 1)
        self.assertEqual(queries.list_messages(self.conn, {"status": "accepted"})["total"], 29)
        self.assertEqual(queries.list_messages(self.conn, {"status": "delivered"})["total"], 0)
        self.assertEqual(queries.list_messages(self.conn, {"device": "GPS"})["total"], 30)
        self.assertEqual(queries.list_messages(self.conn, {"device": "PRINTER"})["total"], 0)
        self.assertEqual(queries.list_messages(self.conn, {"rm": "asha"})["total"], 4)
        self.assertEqual(queries.list_messages(self.conn, {"location": "assam"})["total"], 15)
        self.assertEqual(queries.list_messages(self.conn, {"occasion": "Diwali"})["total"], 30)
        self.assertEqual(queries.list_messages(self.conn, {"occasion": "Holi"})["total"], 0)
        self.assertEqual(queries.list_messages(self.conn, {"campaign_id": str(self.c1)})["total"], 30)
        self.assertEqual(queries.list_messages(self.conn, {"date_from": "2999-01-01"})["total"], 0)
        self.assertEqual(queries.list_messages(self.conn, {"date_from": "2026-10-22", "date_to": "2026-10-22"})["total"], 30)
        self.assertEqual(queries.list_messages(self.conn, {"date_from": "2026-10-23"})["total"], 0)

    def test_hostile_filter_text_is_just_text(self):
        self.assertEqual(queries.list_messages(self.conn, {"q": "'; DROP TABLE email_events; --"})["total"], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM email_events").fetchone()[0] > 0, True)

    def test_message_detail_shows_attempts_events_and_the_reason(self):
        rid = self.rec(self.c1, "H01")["id"]
        d = queries.message_detail(self.conn, rid)
        self.assertEqual((d["csp_code"], d["status"]), ("H01", "Accepted by mail server"))
        self.assertEqual([a["outcome"] for a in d["attempts"]], ["accepted"])
        self.assertEqual([e["kind"] for e in d["events"]], ["accepted"])
        self.assertIn("Confirmed without", d["reason"])
        self.assertTrue(d["body_html"] and d["smtp_message_id"])
        failed = queries.message_detail(self.conn, self.rec(self.c1, "H03")["id"])
        self.assertEqual((failed["status"], failed["failure_kind"]), ("Failed", "permanent"))
        self.assertIn("550", failed["last_error"])
        self.assertIsNone(queries.message_detail(self.conn, 10 ** 9))

    def test_summary_numbers_come_from_the_records(self):
        s = queries.summary(self.conn)
        self.assertEqual((s["emails_accepted"], s["failed"], s["campaigns"], s["campaigns_launched"], s["unique_operators_contacted"]), (29, 1, 2, 1, 29))
        self.assertEqual((s["delivered_confirmed"], s["pending_queued"]), (0, 0))
        self.assertEqual(s["eligible_operators"], 30)

    def test_progress(self):
        p = queries.progress(self.conn, self.c1)
        self.assertEqual((p["sendable"], p["done"], p["percent"]), (30, 30, 100))


class SmtpTransportBuild(unittest.TestCase):
    """The real transport's message building and error classification, against a fake SMTP connection (no network)."""
    class FakeServer:
        def __init__(self, refused=None):
            self.messages, self.refused, self.quit_called = [], refused or {}, False

        def send_message(self, msg):
            self.messages.append(msg)
            return self.refused

        def quit(self):
            self.quit_called = True

    def transport(self, server):
        t = sender.SmtpTransport()
        t.server = server
        return t

    def test_the_message_carries_the_right_headers_and_both_bodies(self):
        srv = self.FakeServer()
        with mock.patch.dict(os.environ, {"EMAIL_FROM": "Eko <csp@example.org>", "EMAIL_REPLY_TO": "help@example.org"}):
            mid = self.transport(srv).send("to@example.com", "Hello", "<p>hi</p>", "hi", {"List-Unsubscribe": "<https://x/u>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"})
        m = srv.messages[0]
        self.assertEqual((m["To"], m["Subject"], m["Reply-To"]), ("to@example.com", "Hello", "help@example.org"))
        self.assertEqual(m["Message-ID"], mid)
        self.assertTrue(mid.endswith("@example.org>"))
        self.assertEqual(m["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")
        self.assertIn("csp@example.org", m["From"])
        self.assertEqual({p.get_content_type() for p in m.walk() if not p.is_multipart()}, {"text/plain", "text/html"})

    def test_a_subject_with_a_line_break_cannot_inject_headers(self):
        srv = self.FakeServer()
        subject = rendering.render({"subject": "Hello\r\nBcc: attacker@example.com", "body": "b", "occasion": "x", "link_url": ""},
                                   rendering.context_for({"occasion": "x"}, "N", "C", "R", []), "T", "https://x")["subject"]
        self.transport(srv).send("to@example.com", subject, "<p>x</p>", "x", {})
        self.assertIsNone(srv.messages[0]["Bcc"])
        self.assertNotIn("\n", srv.messages[0]["Subject"])

    def test_a_refused_recipient_raises_and_is_classified(self):
        srv = self.FakeServer(refused={"to@example.com": (550, b"no such user")})
        with self.assertRaises(smtplib.SMTPRecipientsRefused) as cm:
            self.transport(srv).send("to@example.com", "s", "<p>x</p>", "x", {})
        self.assertEqual(sender.classify(cm.exception), "permanent")

    def test_error_classification(self):
        self.assertEqual(sender.classify(smtplib.SMTPRecipientsRefused({"a": (450, b"try later")})), "transient")
        self.assertEqual(sender.classify(smtplib.SMTPResponseException(421, b"busy")), "transient")
        self.assertEqual(sender.classify(smtplib.SMTPDataError(554, b"rejected")), "permanent")
        self.assertEqual(sender.classify(smtplib.SMTPServerDisconnected("x")), "transient")
        self.assertEqual(sender.classify(TimeoutError()), "transient")
        for fatal in (smtplib.SMTPAuthenticationError(535, b"bad"), smtplib.SMTPSenderRefused(550, b"no", "a@b.c")):
            with self.assertRaises(sender.BatchAbort):
                sender.classify(fatal)

    def test_error_text_never_contains_the_address_or_server_message(self):
        text = sender.error_text(smtplib.SMTPRecipientsRefused({"secret.person@example.com": (550, b"user secret.person unknown")}))
        self.assertNotIn("secret", text)
        self.assertIn("550", text)

    def test_the_settings_summary_never_exposes_a_credential(self):
        with mock.patch.dict(os.environ, {"SMTP_PASS": "hunter2-super-secret", "SMTP_USER": "u@example.org", "CAMPAIGN_EVENTS_KEY": "evkey-secret"}):
            dumped = json.dumps(sender.settings_summary())
        for secret in ("hunter2", "evkey-secret", "u@example.org"):
            self.assertNotIn(secret, dumped)


if __name__ == "__main__":
    unittest.main()
