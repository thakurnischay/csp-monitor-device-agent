"""The dashboard works behind nginx under a sub-path: links and redirects keep the prefix; without the header nothing changes."""
import re
import unittest

from tests.helpers import issue_test_key
from tests.test_routes import RouteTestCase

PREFIX = "/csp-device-heartbeat"


class SubPath(RouteTestCase):

    def test_redirect_to_login_keeps_the_prefix(self):
        r = self.client.get("/", headers={"X-Forwarded-Prefix": PREFIX})
        self.assertIn(r.status_code, (301, 302, 303))
        self.assertTrue(r.headers["Location"].startswith(PREFIX + "/"), r.headers["Location"])

    def test_signed_in_pages_links_and_assets_keep_the_prefix(self):
        self._login()
        for page in ("/", "/updates", "/enrollments"):
            self.check_page(page)

    def check_page(self, page):
        r = self.client.get(page, headers={"X-Forwarded-Prefix": PREFIX})
        self.assertEqual(r.status_code, 200, page)
        html = r.get_data(as_text=True)
        local = re.findall(r'(?:href|src|action)="(/[^"]*)"', html)
        self.assertTrue(local, page + " has no local links?")
        self.assertTrue(all(u.startswith(PREFIX + "/") for u in local), (page, [u for u in local if not u.startswith(PREFIX + "/")]))

    def test_without_the_header_nothing_changes(self):
        r = self.client.get("/", headers={})
        self.assertFalse(r.headers["Location"].startswith(PREFIX))
        self._login()
        html = self.client.get("/updates").get_data(as_text=True)
        self.assertFalse(any(u.startswith(PREFIX) for u in re.findall(r'(?:href|src|action)="(/[^"]*)"', html)))

    def test_login_cookie_has_its_own_name_so_other_apps_on_the_domain_cannot_clash(self):
        r = self._login()
        cookies = r.headers.getlist("Set-Cookie")
        self.assertTrue(any(c.startswith("csp_monitor_session=") for c in cookies), cookies)
        self.assertFalse(any(c.startswith("session=") for c in cookies), cookies)

    def test_agent_api_works_through_the_prefix(self):
        key = issue_test_key("SUB-1", "Sub CSP")
        r = self.client.post("/api/report", json={"csp_id": "SUB-1", "agent_version": "2.0.0"},
                             headers={"X-API-Key": key, "X-Forwarded-Prefix": PREFIX})
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
