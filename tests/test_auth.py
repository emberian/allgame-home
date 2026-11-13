import json
import time
import unittest
from unittest.mock import patch
from subprocess import CompletedProcess

from claude_resident import auth


class TokemanAuthTests(unittest.TestCase):
    def test_fleet_envelope_filters_services_and_rejected_logins(self):
        rows = [
            {"service": "claude", "name": "good", "limits": [
                {"span": "weekly", "scope": "all",
                 "window": {"utilization": .8, "reset": 123}}],
             "reading": {"detail": {"claude": {"verdict": "allowed_warning"}}}},
            {"service": "claude", "name": "bad", "login": {"refused": True}},
            {"service": "codex", "name": "other"},
        ]
        with patch.object(auth.subprocess, "run", return_value=CompletedProcess(
                [], 0, json.dumps({"accounts": rows}))), \
                patch.object(auth, "_usable_accounts", return_value={"good", "bad"}), \
                patch.object(auth, "_PROBE_CACHE", {"at": 0, "rows": []}):
            found = auth.probe_accounts(0)
        self.assertEqual([r["token_name"] for r in found], ["good", "bad"])
        self.assertEqual(auth._health(found[0]), (0, 123, -.8))
        self.assertEqual(auth._health(found[1])[0], 2)

    def test_fresh_login_precedes_setup_key(self):
        entry = {"key": "setup", "access_token": "login",
                 "expires_at": (time.time() + 3600) * 1000}
        self.assertEqual(auth._token_for(entry), "login")
        entry["expires_at"] = 1
        self.assertEqual(auth._token_for(entry), "setup")
        entry["login_error"] = "refused"
        self.assertIsNone(auth._token_for(entry))

    def test_legacy_probe_still_supported(self):
        row = auth._normalize_probe_row({"account": "old", "verdict": "allowed",
                                         "weekly": {"utilization": .3}})
        self.assertEqual(row["token_name"], "old")
        self.assertEqual(row["quota"]["status"], "allowed")


if __name__ == "__main__":
    unittest.main()
