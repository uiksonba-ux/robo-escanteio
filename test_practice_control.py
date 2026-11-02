"""Offline control contract: durable requests never substitute for robot ACKs."""
import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
from uuid import uuid4

import monitor_test_app as service
import practice_band_config as client
import practice_control_store as store


class PracticeControlTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = patch.dict(os.environ, {"MONITOR_API_TOKEN": "reader", "MONITOR_CONTROL_TOKEN": "controller",
                                          "MONITOR_ROBOT_TOKEN": "robot", "MONITOR_CONTROL_DB": self.directory.name + "/control.sqlite"})
        self.env.start()
        self.addCleanup(self.env.stop)
        for name, value in {"_telemetry": None, "_sessions": {}, "_applied": None, "_received_at": None}.items():
            patcher = patch.object(service, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.http = service.app.test_client()
        self.session = str(uuid4())
        self.sequence = 0

    def request(self, method, path, token="reader", body=None, secure=True):
        return self.http.open("/api/v1/practice/" + path, method=method,
                              headers={"Authorization": "Bearer " + token}, json=body,
                              base_url="https://localhost" if secure else "http://localhost")

    def get_control(self):
        response = self.request("GET", "control")
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def envelope(self, revision):
        return {"schema_version": 1, "conta": "PRACTICE", "expected_revision": revision,
                "command_id": str(uuid4()), "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat()}

    def publish(self, control=None, authorized=True, paused=True, settings=None):
        self.sequence += 1
        payload = {"schema_version": 1, "conta": "PRACTICE", "modo": "somente_leitura",
                   "origem": "robo_practice_memoria_volatil", "robot_session": self.session,
                   "sequence": self.sequence, "atualizado_em": datetime.now(timezone.utc).isoformat(),
                   "estatisticas": {"wins": 0, "losses": 0},
                   "bancas": {f"BANCA {i}": {"wins": 0, "losses": 0, "saldo": None} for i in range(1, 11)}}
        if control:
            payload["control_ack"] = {"revision": control["revision"], "command_id": control["command_id"],
                                      "paused": paused, "authorized": authorized}
        if settings:
            payload.update(config_revision=settings["revision"], config_id=settings["config_id"])
        response = self.request("PUT", "telemetry", "robot", payload)
        self.assertEqual(response.status_code, 200, response.json)
        return payload

    def test_control_requires_https_distinct_tokens_and_database(self):
        self.assertEqual(self.request("GET", "control", secure=False).status_code, 426)
        self.assertEqual(self.http.get("/api/v1/monitor", base_url="http://localhost").status_code, 426)
        self.assertEqual(self.request("PUT", "telemetry", "robot", {}, secure=False).status_code, 426)
        command = {**self.envelope(1), "action": "pause_new_entries"}
        self.assertEqual(self.request("PUT", "control", "reader", command).status_code, 401)
        self.assertEqual(self.request("PUT", "control", "robot", command).status_code, 401)
        with patch.dict(os.environ, {"MONITOR_CONTROL_TOKEN": "reader"}):
            self.assertEqual(self.request("GET", "control").status_code, 503)
        with patch.dict(os.environ, {"MONITOR_CONTROL_DB": ""}):
            self.assertEqual(self.request("GET", "control").status_code, 503)
            self.assertEqual(self.request("PUT", "faixas", "controller", {}).status_code, 503)

    def test_pause_remains_pending_until_matching_current_robot_ack(self):
        state = self.get_control()
        self.assertFalse(state["confirmed"])
        command = {**self.envelope(state["revision"]), "action": "pause_new_entries"}
        saved = self.request("PUT", "control", "controller", command)
        self.assertEqual(saved.status_code, 202)
        self.assertFalse(saved.json["confirmed"])
        self.publish(state)
        self.assertFalse(self.get_control()["confirmed"])
        self.publish(saved.json)
        self.assertTrue(self.get_control()["confirmed"])
        self.assertEqual(self.request("PUT", "control", "controller", command).status_code, 409)
        # Reopening storage or a relay restart retains revisions and replay protection.
        self.assertEqual(store.get_control(service._config)["revision"], saved.json["revision"])
        self.assertEqual(self.request("PUT", "control", "controller", command).status_code, 409)

    def test_resume_needs_preexisting_authorization_and_explicit_session(self):
        state = self.get_control()
        command = {**self.envelope(state["revision"]), "action": "resume_new_entries"}
        self.assertEqual(self.request("PUT", "control", "controller", command).status_code, 409)
        self.publish(state, authorized=False)
        self.assertEqual(self.request("PUT", "control", "controller", command).status_code, 409)
        self.publish(state)
        saved = self.request("PUT", "control", "controller", command)
        self.assertEqual(saved.status_code, 202)
        self.assertEqual(saved.json["target_session"], self.session)
        self.publish(saved.json, paused=False)
        self.assertTrue(self.get_control()["confirmed"])
        self.session, self.sequence = str(uuid4()), 0
        self.publish()
        restarted = self.get_control()
        self.assertTrue(restarted["desired_paused"])
        self.assertFalse(restarted["confirmed"])
        self.assertGreater(restarted["revision"], saved.json["revision"])

    def test_stale_conflicting_and_real_commands_rejected(self):
        state = self.get_control()
        command = {**self.envelope(state["revision"]), "action": "pause_new_entries"}
        for values, code in (({"expected_revision": 0}, 400), ({"expected_revision": 999}, 409),
                             ({"conta": "REAL"}, 400), ({"schema_version": True}, 400),
                             ({"expires_at": (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()}, 400),
                             ({"command_id": "invalid"}, 400)):
            response = self.request("PUT", "control", "controller", {**command, **values})
            self.assertEqual(response.status_code, code, response.json)

    def test_expired_pending_command_cannot_be_confirmed(self):
        state = self.get_control()
        command = {**self.envelope(state["revision"]), "action": "pause_new_entries"}
        saved = self.request("PUT", "control", "controller", command).json
        future = datetime.now(timezone.utc) + timedelta(seconds=100)
        with patch.object(store, "now", return_value=future):
            self.assertEqual(self.get_control()["error"], "comando_expirado")

    def test_settings_only_effective_after_fresh_ack_and_writes_use_control(self):
        before = self.request("GET", "settings").json
        core = {key: copy.deepcopy(before[key]) for key in service._CORE_KEYS}
        core["faixas"][0]["gales"] = 5
        command = {**core, **self.envelope(before["revision"]), "expected_config_id": before["config_id"]}
        self.assertEqual(self.request("PUT", "faixas", "reader", command).status_code, 401)
        saved = self.request("PUT", "settings", "controller", command)
        self.assertEqual(saved.status_code, 202, saved.json)
        self.assertFalse(saved.json["aplicada_no_robo"])
        self.publish(settings=before)
        self.assertFalse(self.request("GET", "settings").json["aplicada_no_robo"])
        self.publish(settings=saved.json)
        effective = self.request("GET", "settings").json
        self.assertTrue(effective["aplicada_no_robo"])
        self.assertEqual(effective["effective_config"]["faixas"][0]["gales"], 5)
        with patch.object(store, "now", return_value=datetime.now(timezone.utc)+timedelta(seconds=46)):
            stale = self.request("GET", "settings").json
        self.assertFalse(stale["aplicada_no_robo"])
        self.assertIsNone(stale["effective_config"])
        self.assertEqual(self.request("PUT", "settings", "controller", command).status_code, 409)

    def test_latest_failure_invalidates_previous_confirmations_immediately(self):
        control = self.get_control()
        settings = self.request("GET", "settings").json
        self.publish(control, settings=settings)
        self.assertTrue(self.get_control()["confirmed"])
        self.assertTrue(self.request("GET", "settings").json["aplicada_no_robo"])
        payload = self.publish()
        self.assertFalse(self.get_control()["confirmed"])
        self.assertFalse(self.request("GET", "settings").json["aplicada_no_robo"])
        self.publish(control, settings=settings)
        payload.update(sequence=self.sequence + 1, atualizado_em=datetime.now(timezone.utc).isoformat(),
                       control_ack=None, control_error="falha_de_conexao")
        self.assertEqual(self.request("PUT", "telemetry", "robot", payload).status_code, 200)
        self.assertFalse(self.get_control()["confirmed"])
        self.assertIsNone(self.get_control()["paused"])

    def test_custom_bounds_cover_all_percentages_without_gaps_or_overlap(self):
        before = self.request("GET", "settings").json
        core = {key: copy.deepcopy(before[key]) for key in service._CORE_KEYS}
        for row, (low, high) in zip(core["faixas"], [(0, 39), (40, 54), (55, 74), (75, 89), (90, 100)]):
            row.update(min=low, max=high)
        command = {**core, **self.envelope(before["revision"]), "expected_config_id": before["config_id"]}
        saved = self.request("PUT", "settings", "controller", command)
        self.assertEqual(saved.status_code, 202)
        parsed = client.validate_config({**saved.json, "aplicada_no_robo": False})
        self.assertEqual(client.select_band(40, 60, parsed, (9, 9)), (3, 2.0))
        self.assertEqual(client.select_band(100, 0, parsed, (9, 9)), (0, 2.0))
        for low, high in ((39, 54), (41, 54), (40, 100), (True, 54)):
            changed = copy.deepcopy(core)
            changed["faixas"][1].update(min=low, max=high)
            self.assertFalse(service._valid_config(changed))
            self.assertIsNone(client.validate_config({**changed, "aplicada_no_robo": False, "persistencia": "sqlite"}))

    def test_replayed_telemetry_rejected_after_relay_memory_loss(self):
        state = self.get_control()
        payload = self.publish(state)
        service._sessions, service._telemetry = {}, None
        self.assertEqual(self.request("PUT", "telemetry", "robot", payload).status_code, 409)


class ControlTransportTests(unittest.TestCase):
    url = "https://baskaraido-monitor-test.onrender.com/api/v1/practice/faixas"

    def response(self, data):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.status = 200
        response.__enter__.return_value.read.return_value = json.dumps(data).encode()
        return response

    def state(self):
        return {"schema_version": 1, "conta": "PRACTICE", "revision": 1, "command_id": str(uuid4()),
                "desired_paused": True, "confirmed": False, "pending": True, "expires_at": None,
                "lease_expires_at": (datetime.now(timezone.utc)+timedelta(seconds=30)).isoformat()}

    def test_transport_failure_is_unknown_and_never_confirmation(self):
        with patch.object(client, "urlopen", side_effect=TimeoutError):
            self.assertIsNone(client.read_remote_control(self.url, "robot"))
        data = self.state()
        with patch.object(client, "urlopen", return_value=self.response(data)):
            self.assertEqual(client.read_remote_control(self.url, "robot"), data)
        for override in ({"conta": "REAL"}, {"revision": True}, {"desired_paused": False},
                         {"lease_expires_at": (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()},
                         {"confirmed": True, "pending": True}):
            with patch.object(client, "urlopen", return_value=self.response({**data, **override})):
                self.assertIsNone(client.read_remote_control(self.url, "robot"))
        with patch.object(client, "urlopen") as opener:
            self.assertIsNone(client.read_remote_control(self.url.replace("https", "http"), "robot"))
            opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
