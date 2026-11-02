"""Offline endpoint security and end-to-end settings/telemetry relay checks."""
import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
from uuid import uuid4

import monitor_test_app as service


class PracticeServerTests(unittest.TestCase):
    def setUp(self):
        self.client = service.app.test_client()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = patch.dict(os.environ, {"MONITOR_API_TOKEN": "app-test-secret",
                                          "MONITOR_CONTROL_TOKEN": "control-test-secret",
                                          "MONITOR_CONTROL_DB": self.directory.name + "/control.sqlite",
                                          "MONITOR_ROBOT_TOKEN": "robot-test-secret",
                                          "MONITOR_UPSTREAM_URL": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.app_auth = {"Authorization": "Bearer app-test-secret"}
        self.robot_auth = {"Authorization": "Bearer robot-test-secret"}
        self.control_auth = {"Authorization": "Bearer control-test-secret"}
        config = copy.deepcopy(service._config)
        for name, value in {"_config": config, "_revision": 1, "_telemetry": None,
                            "_received_at": None, "_applied": None, "_sessions": {}}.items():
            patcher = patch.object(service, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.config = {key: copy.deepcopy(service._config[key]) for key in service._CORE_KEYS}
        self.telemetry = {
            "schema_version": 1, "conta": "PRACTICE", "modo": "somente_leitura",
            "origem": "robo_practice_memoria_volatil", "robot_session": str(uuid4()),
            "sequence": 1, "atualizado_em": datetime.now(timezone.utc).isoformat(),
            "estatisticas": {"wins": 8, "losses": 2, "assertividade": -999},
            "bancas": {f"BANCA {i}": {"wins": 8 if i == 1 else 0, "losses": 2 if i == 1 else 0, "saldo": None}
                       for i in range(1, 11)}}

    def get_config(self, auth=None):
        return self.client.get("/api/v1/practice/faixas", headers=auth or self.app_auth)

    def publish(self, data=None, auth=None):
        return self.client.put("/api/v1/practice/telemetry", json=data or self.telemetry,
                               headers=auth or self.robot_auth)

    def command(self, config, before=None):
        before = before or self.get_config().json
        return {**config, "expected_revision": before["revision"], "expected_config_id": before["config_id"],
                "command_id": str(uuid4()), "expires_at": (datetime.now(timezone.utc)+timedelta(seconds=90)).isoformat()}

    def test_roles_fail_closed_and_tokens_not_echoed(self):
        self.assertEqual(self.publish(auth=self.app_auth).status_code, 401)
        self.assertEqual(self.client.get("/api/v1/monitor", headers=self.robot_auth).status_code, 401)
        self.assertEqual(self.get_config(self.robot_auth).status_code, 200)
        self.assertEqual(self.client.put("/api/v1/practice/faixas", json=self.config,
                                        headers=self.robot_auth).status_code, 401)
        with patch.dict(os.environ, {"MONITOR_ROBOT_TOKEN": ""}):
            response = self.publish()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("robot-test-secret", response.get_data(as_text=True))
        response = self.get_config({"Authorization": "Bearer não-ASCII"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_complete_round_trip_applies_only_after_matching_ack(self):
        before = self.get_config().json
        self.config["faixas"][0]["gales"] = 5
        response = self.client.put("/api/v1/practice/faixas", headers=self.control_auth, json=self.command(self.config, before))
        self.assertEqual(response.status_code, 202)
        saved = response.json
        self.assertGreater(saved["revision"], before["revision"])
        self.assertFalse(saved["aplicada_no_robo"])
        # Telemetry describing a previously opened order does not acknowledge new settings.
        self.telemetry.update(config_revision=before["revision"], config_id=before["config_id"])
        self.assertEqual(self.publish().status_code, 200)
        self.assertFalse(self.get_config().json["aplicada_no_robo"])
        self.telemetry.update(sequence=2, config_revision=saved["revision"], config_id=saved["config_id"])
        self.assertEqual(self.publish().status_code, 200)
        applied = self.get_config().json
        self.assertTrue(applied["aplicada_no_robo"])
        self.assertEqual(applied["aplicada_config_id"], saved["config_id"])
        data = self.client.get("/api/v1/monitor", headers=self.app_auth).json
        self.assertEqual(data["estatisticas"]["assertividade"], 80.0)
        self.assertEqual(len(data["bancas"]), 10)
        self.assertEqual(data["bancas"]["BANCA 1"]["wins"], 8)
        self.assertEqual(data["bancas"]["BANCA 1"]["assertividade"], 80.0)
        self.assertIsNone(data["bancas"]["BANCA 1"]["saldo"])
        self.assertFalse(data["stale"])

    def test_conflicts_and_restart_hash_guard(self):
        before = self.get_config().json
        changed = copy.deepcopy(self.config)
        changed["faixas"][0]["gales"] = 6
        self.client.put("/api/v1/practice/faixas", headers=self.control_auth, json=self.command(changed))
        service._revision = before["revision"]  # simulate revision reuse after a process restart
        response = self.client.put("/api/v1/practice/faixas", headers=self.control_auth, json=self.command(self.config, before))
        self.assertEqual(response.status_code, 409)
        self.telemetry.update(config_revision=before["revision"], config_id=before["config_id"])
        self.publish()
        self.assertFalse(self.get_config().json["aplicada_no_robo"])

    def test_idempotent_save_preserves_revision(self):
        before = self.get_config().json
        after = self.client.put("/api/v1/practice/faixas", headers=self.control_auth, json=self.command(self.config)).json
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["config_id"], before["config_id"])

    def test_reject_real_partial_nonfinite_and_boolean_counters(self):
        invalid = [dict(self.telemetry, conta="REAL"), dict(self.telemetry, schema_version=True),
                   dict(self.telemetry, bancas={}),
                   dict(self.telemetry, estatisticas={"wins": True, "losses": 0}),
                   dict(self.telemetry, config_revision=True, config_id="0" * 64),
                   dict(self.telemetry, estatisticas={"wins": 1, "losses": 2}),
                   dict(self.telemetry, estatisticas={"wins": 2**31, "losses": 2})]
        saldo = copy.deepcopy(self.telemetry)
        saldo["bancas"]["BANCA 1"]["saldo"] = float("inf")
        invalid.append(saldo)
        for data in invalid:
            with self.subTest(data=data):
                self.assertEqual(self.publish(data).status_code, 400)
        for value in (float("nan"), float("inf"), True, 10**400):
            config = copy.deepcopy(self.config)
            config["faixas"][0]["mult"] = value
            self.assertEqual(self.client.put("/api/v1/practice/faixas", headers=self.control_auth,
                                            json=self.command(config)).status_code, 400)
        self.assertEqual(self.client.put("/api/v1/practice/faixas", headers=self.control_auth,
                                        data="x" * 4097).status_code, 413)

    def test_reject_out_of_order_and_report_stale_data(self):
        stale = dict(self.telemetry, sequence=1,
                     atualizado_em=(datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat())
        self.assertEqual(self.publish(stale).status_code, 200)
        response = self.client.get("/api/v1/monitor", headers=self.app_auth)
        self.assertTrue(response.json["stale"])
        self.telemetry["sequence"] = 2
        self.assertEqual(self.publish().status_code, 200)
        self.assertEqual(self.publish().status_code, 409)
        replay = dict(stale, robot_session=str(uuid4()), sequence=1)
        self.assertEqual(self.publish(replay).status_code, 409)

    def test_whitelist_strips_unknown_secret_fields(self):
        self.telemetry.update(token="should-never-relay", credentials={"password": "hidden"})
        self.assertEqual(self.publish().status_code, 200)
        response = self.client.get("/api/v1/monitor", headers=self.app_auth)
        self.assertNotIn("should-never-relay", response.get_data(as_text=True))
        self.assertNotIn("credentials", response.json)

    def test_upstream_allowlist_and_redirect_block(self):
        with patch.dict(os.environ, {"MONITOR_UPSTREAM_ALLOWED_HOSTS": "example.com,127.0.0.1,localhost"}):
            for url in ("https://evil.example/api/v1/monitor", "https://127.0.0.1/api/v1/monitor",
                        "https://localhost/api/v1/monitor", "https://example.com:444/api/v1/monitor",
                        "https://secret@example.com/api/v1/monitor"):
                self.assertFalse(service._upstream_url(url))
            self.assertTrue(service._upstream_url("https://example.com/api/v1/monitor"))
        with self.assertRaises(ValueError):
            service._NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://evil.example")


if __name__ == "__main__":
    unittest.main()

