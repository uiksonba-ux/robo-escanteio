"""Robot transport checks with no broker, Telegram, sockets, or worker startup."""
import copy
from dataclasses import FrozenInstanceError
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

import practice_band_config as cfg
from test_practice_band_integration import BASE

URL = "https://baskaraido-monitor-test.onrender.com/api/v1/practice/faixas"


def versioned_config():
    data = copy.deepcopy(BASE)
    core = {key: data[key] for key in ("schema_version", "conta", "enabled", "faixas")}
    data.update(revision=7, config_id=hashlib.sha256(json.dumps(
        core, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
    return data


def response(data):
    result = MagicMock()
    result.status = 200
    result.read.return_value = json.dumps(data).encode()
    result.__enter__.return_value = result
    return result


def isolated_robot():
    path = Path(__file__).parent / "tests" / "test_gestao.py"
    spec = importlib.util.spec_from_file_location("robot_test_support", path)
    support = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(support)
    return support.carregar_robo()


class PracticeTransportTests(unittest.TestCase):
    def test_huge_integer_multiplier_is_rejected_without_overflow(self):
        data = copy.deepcopy(BASE)
        data["faixas"][0]["mult"] = 10 ** 400
        self.assertIsNone(cfg.validate_config(data))
        with patch.object(cfg, "urlopen", return_value=response(data)):
            self.assertIsNone(cfg.read_remote_snapshot(URL, "robot-test-token"))

    def test_versioned_applied_snapshot_is_valid_and_immutable(self):
        data = versioned_config()
        data["aplicada_no_robo"] = True
        with patch.object(cfg, "urlopen", return_value=response(data)):
            snapshot = cfg.read_remote_snapshot(URL, "robot-test-token")
        self.assertEqual(snapshot.revision, 7)
        self.assertEqual(cfg.select_band(0, 0, snapshot.config, (0, 2)), (4, 2))
        with self.assertRaises(FrozenInstanceError):
            snapshot.revision = 8
        self.assertIsInstance(snapshot.config[1], tuple)

    def test_invalid_revision_or_config_hash_is_rejected(self):
        for change in ({"revision": True}, {"revision": 0}, {"config_id": "bad"},
                       {"revision": None}, {"schema_version": True}):
            with self.subTest(change=change):
                data = dict(versioned_config(), **change)
                with patch.object(cfg, "urlopen", return_value=response(data)):
                    self.assertIsNone(cfg.read_remote_snapshot(URL, "robot-test-token"))

    def test_credentials_never_sent_to_other_urls(self):
        for url in ("http://baskaraido-monitor-test.onrender.com/api/v1/practice/faixas",
                    "https://production.example/api/v1/practice/faixas",
                    URL + "?redirect=evil", URL + "#fragment",
                    URL.replace(".com/", ".com:443/"),
                    URL.replace("https://", "https://user:pass@"),
                    "https://[invalid", URL.replace("/faixas", "/telemetry")):
            with self.subTest(url=url), patch.object(cfg, "urlopen") as network:
                self.assertIsNone(cfg.read_remote_snapshot(url, "robot-test-token"))
                self.assertFalse(cfg.publish_telemetry(url, "robot-test-token",
                                                       {"conta": "PRACTICE"}))
                network.assert_not_called()

    def test_redirect_handler_never_forwards_authorization(self):
        request = cfg.Request(URL, headers={"Authorization": "Bearer robot-test-token"})
        for status in (301, 302, 303, 307, 308):
            self.assertIsNone(cfg._NoRedirect().redirect_request(
                request, None, status, "", {}, "https://another.example"))

    def test_oversized_config_and_network_errors_are_closed(self):
        too_large = response(BASE)
        too_large.read.return_value = b" " * 4097
        with patch.object(cfg, "urlopen", return_value=too_large):
            self.assertIsNone(cfg.read_remote_snapshot(URL, "robot-test-token"))
        with patch.object(cfg, "urlopen", side_effect=OSError("unavailable")):
            self.assertIsNone(cfg.read_remote_snapshot(URL, "robot-test-token"))
            self.assertFalse(cfg.publish_telemetry(URL, "robot-test-token",
                                                   {"conta": "PRACTICE"}))

    def test_telemetry_only_uses_authenticated_practice_endpoint(self):
        payload = {"schema_version": 1, "conta": "PRACTICE",
                   "estatisticas": {"wins": 4, "losses": 1, "assertividade": 80.0}}
        with patch.object(cfg, "urlopen", return_value=response({})) as network:
            self.assertTrue(cfg.publish_telemetry(URL, "robot-test-token", payload))
        req = network.call_args.args[0]
        self.assertEqual(req.full_url,
                         "https://baskaraido-monitor-test.onrender.com/api/v1/practice/telemetry")
        self.assertEqual(req.get_method(), "PUT")
        self.assertEqual(req.get_header("Authorization"), "Bearer robot-test-token")
        self.assertEqual(json.loads(req.data), payload)
        self.assertNotIn(b"robot-test-token", req.data)
        self.assertEqual(network.call_args.kwargs["timeout"], 3)

    def test_invalid_or_nonpractice_telemetry_never_uses_network(self):
        payloads = ({"conta": "REAL"}, {"conta": "PRACTICE", "value": float("nan")},
                    {"conta": "PRACTICE", "oversized": "x" * 65536})
        with patch.object(cfg, "urlopen") as network:
            for payload in payloads:
                self.assertFalse(cfg.publish_telemetry(URL, "robot-test-token", payload))
            self.assertFalse(cfg.publish_telemetry(URL, "", {"conta": "PRACTICE"}))
            network.assert_not_called()


class PracticeRobotObserverTests(unittest.TestCase):
    def test_unicode_authorization_is_rejected_without_exception(self):
        robot = isolated_robot()
        robot.request.headers = {"Authorization": "Bearer \u2603"}
        with patch.dict(os.environ, {"MONITOR_API_TOKEN": "test-only-token"}):
            data, status = robot.monitor_practice()
        self.assertEqual(status, 401)
        self.assertEqual(data, {"erro": "nao_autorizado"})

    def test_nonpractice_guard_prevents_config_and_telemetry_network(self):
        robot = isolated_robot()
        robot.CONTA = "REAL"
        with patch.dict(os.environ, {"PRACTICE_FAIXAS_URL": URL,
                                    "MONITOR_ROBOT_TOKEN": "robot-test-token"}), \
                patch.object(robot, "read_remote_snapshot") as read, \
                patch.object(robot, "publish_telemetry") as publish:
            self.assertIsNone(robot.carregar_config_practice())
            self.assertFalse(robot.sincronizar_monitor_practice())
        read.assert_not_called()
        publish.assert_not_called()

    def test_disabled_integration_uses_no_network(self):
        robot = isolated_robot()
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(robot, "read_remote_snapshot") as read, \
                patch.object(robot, "publish_telemetry") as publish:
            self.assertIsNone(robot.carregar_config_practice())
            self.assertFalse(robot.sincronizar_monitor_practice())
        read.assert_not_called()
        publish.assert_not_called()

    def test_snapshot_updates_totals_and_all_ten_banks(self):
        robot = isolated_robot()
        robot.registrar_win("BANCA 1", 0)
        robot.registrar_loss("BANCA 10")
        snapshot = robot.snapshot_monitor_practice()
        self.assertEqual(snapshot["conta"], "PRACTICE")
        self.assertEqual(snapshot["estatisticas"],
                         {"wins": 1, "losses": 1, "assertividade": 50.0})
        self.assertEqual(len(snapshot["bancas"]), 10)
        self.assertEqual(snapshot["bancas"]["BANCA 1"]["wins"], 1)
        self.assertEqual(snapshot["bancas"]["BANCA 10"]["losses"], 1)
        self.assertTrue(all(row["saldo"] is None for row in snapshot["bancas"].values()))
        self.assertEqual(sum(row["wins"] for row in snapshot["bancas"].values()), 1)


if __name__ == "__main__":
    unittest.main()
