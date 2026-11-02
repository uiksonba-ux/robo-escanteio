"""Offline Android-shaped settings -> robot -> authenticated monitor acceptance.

The real Flask routes and robot functions run, but the worker, IQ Option,
Telegram and HTTP transport are stubbed. No trading process is started.
"""
import copy
import importlib.util
import itertools
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.parse import urlparse

import monitor_test_app as service
import practice_band_config as protocol


CONFIG_PATH = "/api/v1/practice/faixas"
CONFIG_URL = "https://baskaraido-monitor-test.onrender.com" + CONFIG_PATH
APP_TOKEN = "offline-app-token"
ROBOT_TOKEN = "offline-robot-token"
DEFAULT_CONFIG = {
    "schema_version": 1, "conta": "PRACTICE", "enabled": True,
    "faixas": [{"min": low, "max": high, "gales": gales, "mult": 2.0}
               for low, high, gales in [(0, 49, 4), (50, 59, 3), (60, 69, 2),
                                         (70, 79, 1), (80, 100, 0)]],
}


def isolated_robot(variables):
    """Reuse the regression suite's checked worker/IQ/Telegram isolation."""
    path = Path(__file__).parent / "tests" / "test_gestao.py"
    spec = importlib.util.spec_from_file_location("practice_e2e_robot_loader", path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return helper.carregar_robo(variables)


class TransportResponse:
    def __init__(self, response):
        self.status = response.status_code
        self.body = response.get_data()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        return self.body[:limit]


class PracticeEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.client = service.app.test_client()
        self.headers = {"Authorization": "Bearer " + APP_TOKEN}
        self.variables = {"MONITOR_API_TOKEN": APP_TOKEN,
                          "MONITOR_ROBOT_TOKEN": ROBOT_TOKEN,
                          "MONITOR_UPSTREAM_URL": "",
                          "PRACTICE_FAIXAS_URL": CONFIG_URL}
        env = patch.dict(os.environ, self.variables)
        env.start()
        self.addCleanup(env.stop)
        state = {"_config": copy.deepcopy(DEFAULT_CONFIG), "_revision": 1,
                 "_telemetry": None, "_received_at": None, "_applied": None,
                 "_sessions": {}}
        for name, value in state.items():
            isolated = patch.object(service, name, value)
            isolated.start()
            self.addCleanup(isolated.stop)
        self.robot = isolated_robot(self.variables)
        self.robot.api = MagicMock()
        self.robot.api.get_balance.return_value = 1000
        order_ids = itertools.count(1001)
        self.orders = []

        def order(banca, ativo, value, direction, expiration):
            self.orders.append((banca, value))
            return True, next(order_ids)

        for stub in [patch.object(self.robot, "garantir_practice", return_value=True),
                     patch.object(self.robot, "executar_ordem", side_effect=order),
                     patch.object(self.robot, "telegram"),
                     patch.object(protocol, "urlopen", side_effect=self.transport)]:
            stub.start()
            self.addCleanup(stub.stop)

    def transport(self, request, timeout):
        """Exercise the server authentication/validation without any socket."""
        address = urlparse(request.full_url)
        self.assertEqual(address.scheme, "https")
        self.assertEqual(address.netloc, protocol.ALLOWED_HOST)
        self.assertIn(address.path, {CONFIG_PATH, "/api/v1/practice/telemetry"})
        self.assertEqual(timeout, 3)
        response = self.client.open(address.path, method=request.get_method(),
                                    headers=dict(request.header_items()), data=request.data)
        if response.status_code != 200:
            raise HTTPError(request.full_url, response.status_code, "offline API failure", {}, None)
        return TransportResponse(response)

    def config(self):
        response = self.client.get(CONFIG_PATH, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        return response.json

    def save(self, config):
        before = self.config()
        body = {**copy.deepcopy(config), "expected_revision": before["revision"],
                "expected_config_id": before["config_id"]}
        response = self.client.put(CONFIG_PATH, json=body, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        return response.json

    def open_order(self, banca, level=0):
        ok, order_id, context = self.robot.enviar_ordem_gerida(
            banca, "OFFLINE-TEST", "call", 1, nivel=level)
        self.assertTrue(ok)
        return order_id, context

    def test_settings_change_freezes_open_operation_and_updates_monitor(self):
        first, context = self.open_order("BANCA 1")
        original = self.config()
        self.assertEqual((context["gales"], context["multiplicador"]), (4, 2.0))
        self.assertEqual(context["config_id"], original["config_id"])
        changed = copy.deepcopy(DEFAULT_CONFIG)
        changed["faixas"][0].update(gales=1, mult=3.0)
        saved = self.save(changed)
        self.assertFalse(saved["aplicada_no_robo"])
        self.assertGreater(saved["revision"], original["revision"])

        self.robot.registrar_resultado_ordem(first, "loss", -10)
        self.robot.registrar_loss("BANCA 1")
        _, gale = self.open_order("BANCA 1", level=1)
        self.assertEqual((gale["gales"], gale["multiplicador"]), (4, 2.0))
        self.assertEqual(gale["config_id"], original["config_id"])
        self.assertEqual(self.orders[-1], ("BANCA 1", 20.0))

        second, fresh = self.open_order("BANCA 2")
        self.assertEqual((fresh["gales"], fresh["multiplicador"]), (1, 3.0))
        self.assertEqual(fresh["config_revision"], saved["revision"])
        self.assertEqual(fresh["config_id"], saved["config_id"])
        self.robot.registrar_resultado_ordem(second, "win", 8)
        self.robot.registrar_win("BANCA 2", 0)
        self.robot.finalizar_operacao_global("BANCA 2")
        self.assertTrue(self.robot.sincronizar_monitor_practice())
        self.assertTrue(self.config()["aplicada_no_robo"])

        response = self.client.get("/api/v1/monitor", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        data = response.json
        self.assertEqual(data["conta"], "PRACTICE")
        self.assertEqual(data["estatisticas"], {"wins": 1, "losses": 1, "assertividade": 50.0})
        self.assertEqual(set(data["bancas"]), {f"BANCA {i}" for i in range(1, 11)})
        self.assertEqual(data["bancas"]["BANCA 1"]["losses"], 1)
        self.assertEqual(data["bancas"]["BANCA 2"]["wins"], 1)
        self.assertTrue(all(bank["saldo"] is None for bank in data["bancas"].values()))
        self.assertEqual(data["config_id"], saved["config_id"])
        self.assertFalse(data["stale"])
        self.assertNotIn(APP_TOKEN, response.get_data(as_text=True))
        self.assertNotIn(ROBOT_TOKEN, response.get_data(as_text=True))
        # A telemetry refresh never mutates an operation already accepted.
        self.assertEqual(self.robot.operacoes_ativas["BANCA 1"]["config_id"], original["config_id"])

    def test_all_five_remote_bands_at_boundaries(self):
        changed = copy.deepcopy(DEFAULT_CONFIG)
        for index, row in enumerate(changed["faixas"]):
            row.update(gales=9-index, mult=1.25+index)
        saved = self.save(changed)
        snapshot = protocol.read_remote_snapshot(CONFIG_URL, ROBOT_TOKEN)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.config_id, saved["config_id"])
        for wins, index in [(0, 0), (49, 0), (50, 1), (59, 1), (60, 2), (69, 2),
                            (70, 3), (79, 3), (80, 4), (100, 4)]:
            with self.subTest(wins=wins):
                self.assertEqual(self.robot.gestao_sincronizada_practice(
                    wins, 100-wins, snapshot.config), (9-index, 1.25+index))

    def test_wrong_robot_token_cannot_fetch_or_publish(self):
        self.assertIsNone(protocol.read_remote_snapshot(CONFIG_URL, "wrong-token"))
        self.assertIsNone(self.robot.config_practice_remota)
        with patch.object(self.robot, "credenciais_monitor_practice",
                          return_value=(CONFIG_URL, "wrong-token")):
            self.assertFalse(self.robot.sincronizar_monitor_practice())
        data = self.client.get("/api/v1/monitor", headers=self.headers).json
        self.assertEqual(data["origem"], "ambiente_de_teste_sem_dados_reais")
        self.assertFalse(self.config()["aplicada_no_robo"])


if __name__ == "__main__":
    unittest.main()
