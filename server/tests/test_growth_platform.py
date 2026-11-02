"""The CSP Growth Platform merged at /growth ("Device Sales"): login and CSRF from the console, the order page and email
links staying public, its own live switch, and WhatsApp left out. Uses synthetic operators in a temp data folder and
never contacts a mail server."""
import importlib
import json
import os
import shutil
import tempfile
from unittest import mock

from growth_platform.core import config as gconfig, emailer as gemailer, store as gstore
from tests.test_routes import RouteTestCase


def operator(code, *, matm=False, printer=False, email="op@example.com"):
    return {"cspCode": code, "name": f"Operator {code}", "mobile": "9000000000", "email": email, "contactable": True,
            "category": "A", "rm": "RM One", "district": "Patna", "state": "Bihar", "population": "", "terminalStatus": "Active",
            "inactivityDays": 1, "loginDays": 25.0, "txnPrev": 400.0, "txnThis": 420.0, "matmTxnPrev": 0.0, "matmTxnThis": 0.0,
            "pbPrintsPrev": 0.0, "pbPrintsThis": 0.0, "commission": 9000.0, "commissionHistory": [], "claimsMatm": matm,
            "claimsPrinter": printer, "claimsGps": False, "matmInactivityText": "", "matmInactSaysNoDevice": False,
            "totalAccounts": 300.0, "avgBalance": 1500.0}


class GrowthCase(RouteTestCase):
    def setUp(self):
        super().setUp()
        self.data_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.data_dir, True)
        with open(os.path.join(self.data_dir, "csps.json"), "w", encoding="utf-8") as f:
            json.dump({"source": "test", "sheet": "t", "importedAt": "2099-01-01T00:00:00+00:00", "rowCount": 2, "cspCount": 2,
                       "duplicateCodes": [], "csps": [operator("CSP001"), operator("CSP002", matm=True, printer=True)]}, f)
        for name, value in (("DATA_DIR", self.data_dir), ("CSP_FILE", os.path.join(self.data_dir, "csps.json")),
                            ("STATE_FILE", os.path.join(self.data_dir, "state.json"))):
            patcher = mock.patch.object(gstore, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def csrf(self):
        page = self.client.get("/growth/").get_data(as_text=True)
        return page.split('name="csrf-token" content="', 1)[1].split('"', 1)[0]


class Access(GrowthCase):
    def test_dashboard_and_api_need_the_console_login(self):
        r = self.client.get("/growth/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])
        for url in ("/growth/api/overview", "/growth/api/orders", "/growth/api/messages", "/growth/api/csps"):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 401, url)
            self.assertIn("Login required", r.get_json()["error"])

    def test_order_page_and_email_links_stay_public(self):
        self.assertEqual(self.client.get("/growth/order.html").status_code, 200)
        self.assertEqual(self.client.get("/growth/order.js").status_code, 200)
        self.assertEqual(self.client.get("/growth/api/order-page/config").status_code, 200)
        pixel = self.client.get("/growth/pixel/unknown-token.png")
        self.assertEqual((pixel.status_code, pixel.headers["Content-Type"]), (200, "image/png"))
        self.assertEqual(self.client.get("/growth/go/unknown-token").status_code, 302)    # a stale link still lands somewhere

    def test_public_order_submit_needs_no_csrf_token(self):
        r = self.client.post("/growth/api/order-page/submit", json={})
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("CSRF", r.get_json()["error"])

    def test_dashboard_posts_need_the_csrf_token(self):
        self._login()
        r = self.client.post("/growth/api/outreach/MATM", json={})
        self.assertEqual(r.status_code, 400)
        self.assertIn("CSRF", r.get_json()["error"])


class PaymentQr(GrowthCase):
    PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                        "1f15c4890000000d49444154789c63000100000500010d0a2db40000000049454e44ae426082")

    def qr(self):
        return self.client.get("/growth/api/order-page/config").get_json()["qr"]

    def upload(self):
        with open(os.path.join(self.data_dir, "payment-qr.png"), "wb") as f:
            f.write(self.PNG)

    def test_bank_transfer_only_by_default(self):
        with mock.patch.dict(os.environ, {"ORDER_PAGE_QR_ENABLED": ""}):
            self.assertEqual(self.qr(), {"enabled": False, "image": None})
            self.upload()                                              # a QR file on its own does not switch QR on
            self.assertEqual(self.qr(), {"enabled": False, "image": None})

    def test_switched_on_without_a_qr_file_still_shows_no_qr(self):
        with mock.patch.dict(os.environ, {"ORDER_PAGE_QR_ENABLED": "true"}):
            self.assertEqual(self.qr(), {"enabled": False, "image": None})
        self.assertEqual(self.client.get("/growth/payment-qr").status_code, 404)

    def test_switched_on_with_a_qr_file_the_qr_is_offered_and_public(self):
        self.upload()
        with mock.patch.dict(os.environ, {"ORDER_PAGE_QR_ENABLED": "true"}):
            qr = self.qr()
        self.assertTrue(qr["enabled"])
        self.assertTrue(qr["image"].startswith("payment-qr?v="))      # relative to order.html, so the /growth prefix holds
        r = self.client.get("/growth/" + qr["image"])                  # no login: the CSP's browser loads it
        self.assertEqual((r.status_code, r.mimetype, r.data), (200, "image/png", self.PNG))

    def test_a_qr_order_is_accepted(self):
        with mock.patch.object(gstore, "save_state"), mock.patch("growth_platform.core.orderpage.mirror_to_sheet"):
            r = self.client.post("/growth/api/order-page/submit", json={
                "cspCode": "CSP001", "productId": "MATM", "paymentMethod": "qr", "utr": "412345678901",
                "addressLine": "Main Road", "city": "Patna", "pincode": "800001", "deliveryState": "Bihar"})
        self.assertNotIn("Unknown payment method", r.get_data(as_text=True))


class Dashboard(GrowthCase):
    def setUp(self):
        super().setUp()
        self._login()

    def test_shell_carries_the_csrf_token_and_a_link_back_to_the_console(self):
        page = self.client.get("/growth/").get_data(as_text=True)
        self.assertIn('<meta name="csrf-token" content="', page)
        self.assertNotIn("__CONSOLE_URL__", page)
        self.assertIn("&larr; Console", page)

    def test_overview_evaluates_the_operator_data(self):
        r = self.client.get("/growth/api/overview")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["source"]["cspCount"], 2)

    def test_a_drip_run_stays_dry_and_sends_nothing(self):
        with mock.patch.object(gemailer, "send", side_effect=AssertionError("must not send")):
            r = self.client.post("/growth/api/outreach/MATM", json={}, headers={"X-CSRF-Token": self.csrf()})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["mode"], "dry")
        self.assertEqual(r.get_json()["sent"], 0)

    def test_whatsapp_and_courier_tracking_are_not_part_of_the_merge(self):
        for url in ("/growth/api/whatsapp/status", "/growth/api/whatsapp/offer/MATM/plan"):
            self.assertEqual(self.client.get(url).status_code, 404, url)
        r = self.client.post("/growth/api/orders/tracking-check", headers={"X-CSRF-Token": self.csrf()})
        self.assertIn(r.status_code, (404, 405))

    def test_page_allows_its_fonts_but_still_forbids_framing(self):
        csp = self.client.get("/growth/").headers["Content-Security-Policy"]
        self.assertIn("https://fonts.googleapis.com", csp)
        self.assertIn("frame-ancestors 'none'", csp)

    def test_device_sales_is_in_the_console_navigation(self):
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn('href="/growth/"', page)
        self.assertIn("Device Sales", page)


class Configuration(GrowthCase):
    def tearDown(self):
        importlib.reload(gconfig)
        importlib.reload(gemailer)
        super().tearDown()

    def test_festival_live_switch_never_turns_the_drip_live(self):
        with mock.patch.dict(os.environ, {"EMAIL_MODE": "live", "GROWTH_EMAIL_MODE": ""}):
            importlib.reload(gemailer)
            self.assertEqual(gemailer.MODE, "dry")
        with mock.patch.dict(os.environ, {"GROWTH_EMAIL_MODE": "live"}):
            importlib.reload(gemailer)
            self.assertEqual(gemailer.MODE, "live")

    def test_links_use_the_dashboards_public_address(self):
        env = {"CAMPAIGN_PUBLIC_BASE_URL": "https://indev.eko.in/csp-device-heartbeat/", "GROWTH_PUBLIC_BASE_URL": "",
               "ORDER_PAGE_BASE_URL": ""}
        with mock.patch.dict(os.environ, env):
            importlib.reload(gconfig)
            importlib.reload(gemailer)
            self.assertEqual(gconfig.PUBLIC_BASE_URL, "https://indev.eko.in/csp-device-heartbeat/growth")
            self.assertEqual(gconfig.ORDER_PAGE_BASE_URL, "https://indev.eko.in/csp-device-heartbeat/growth")
            self.assertEqual(gemailer.TRACKING_BASE_URL, "https://indev.eko.in/csp-device-heartbeat/growth")
