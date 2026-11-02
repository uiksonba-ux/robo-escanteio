"""Real robot functions with the existing worker, IQ and Telegram isolation."""
import importlib.util
import itertools
import os
from pathlib import Path
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch


def robot(variables=None):
    path = Path(__file__).parent / "tests" / "test_gestao.py"
    spec = importlib.util.spec_from_file_location("recovery_test_helper", path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return helper.carregar_robo(variables)


class RecoveryAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.robot = robot()
        self.robot.api = MagicMock()
        self.robot.api.get_balance.return_value = 1000
        self.robot.api.get_digital_current_profit.return_value = False
        self.ids = itertools.count(1001)
        self.orders = []

        def order(bank, asset, value, direction, expiration):
            self.orders.append((bank, value))
            return True, next(self.ids)

        for stub in (patch.object(self.robot, "garantir_practice", return_value=True),
                     patch.object(self.robot, "executar_ordem", side_effect=order)):
            stub.start()
            self.addCleanup(stub.stop)

    def send(self, bank="BANCA 1", level=0):
        return self.robot.enviar_ordem_gerida(bank, "TEST-OTC", "call", 1, nivel=level)

    def test_observed_payout_is_frozen_with_operation_and_bands_ignore_money(self):
        self.robot.api.get_digital_current_profit.return_value = 60
        self.robot.resultado_global_centavos = -1000
        self.robot.stats.update(wins=60, losses=40)
        ok, first, context = self.send()
        self.assertTrue(ok)
        self.assertEqual(context["entrada"], 26.67)
        self.assertEqual(context["payout"], .60)
        self.assertEqual(context["payout_fonte"], "cache_iq")
        self.assertEqual((context["gales"], context["multiplicador"]), (2, 2))
        self.robot.registrar_resultado_ordem(first, "loss", -26.67)
        self.robot.api.get_digital_current_profit.return_value = 90
        self.robot.stats.update(wins=100, losses=0)
        ok, _, gale = self.send(level=1)
        self.assertTrue(ok)
        self.assertEqual((gale["payout"], gale["gales"], gale["multiplicador"]), (.60, 2, 2))
        self.assertEqual(self.orders[-1][1], 53.34)
        # A different bank uses current global WIN/LOSS, regardless of debt.
        ok, _, fresh = self.send("BANCA 2")
        self.assertTrue(ok)
        self.assertEqual((fresh["prejuizo_alocado"], fresh["gales"]), (0, 0))

    def test_payout_estimate_bounds_do_not_understate_target(self):
        for invalid in (80, 1.1, float("nan"), float("inf")):
            with self.subTest(value=invalid):
                loaded = robot({"PAYOUT_RECUPERACAO": str(invalid)})
                self.assertEqual(loaded.PAYOUT_RECUPERACAO, .80)
                self.assertEqual(loaded.calcular_entrada_recuperacao(10, 1000), 22.50)
        for invalid in (0, -1, 1.1, float("nan")):
            with self.subTest(payout=invalid), self.assertRaises(ValueError):
                self.robot.calcular_entrada_recuperacao(10, 1000, invalid)

    def test_insufficient_balance_preserves_debt_for_next_eligible_bank(self):
        self.robot.resultado_global_centavos = -1000
        self.robot.api.get_balance.return_value = 10
        ok, reason, _ = self.send()
        self.assertFalse(ok)
        self.assertEqual(reason, "saldo_insuficiente")
        self.assertFalse(self.orders)
        self.assertEqual(self.robot.resultado_global_centavos, -1000)
        state = self.robot.snapshot_gestao_global()
        self.assertEqual(state["recuperacao_estado"], "saldo_insuficiente")
        self.assertEqual(state["recuperacao_planejada"], 10)
        self.robot.api.get_balance.return_value = 1000
        ok, _, context = self.send("BANCA 2")
        self.assertTrue(ok)
        self.assertEqual(context["prejuizo_alocado"], 10)
        self.assertEqual(self.robot.recuperacao_em_curso, "BANCA 2")

    def test_telemetry_contains_all_banks_active_order_and_recovery_without_secrets(self):
        self.robot.resultado_global_centavos = -1000
        self.assertTrue(self.send()[0])
        snapshot = self.robot.snapshot_monitor_practice()
        self.assertEqual(len(snapshot["bancas"]), 10)
        self.assertEqual(snapshot["saldo_practice"], 1000)
        self.assertEqual(snapshot["operacoes"][0]["valor"], 22.50)
        self.assertEqual(snapshot["operacoes"][0]["banca"], "BANCA 1")
        self.assertEqual(snapshot["gestao"]["recuperacao_estado"], "em_andamento")
        self.assertFalse(snapshot["gestao"]["recuperacao_garantida"])
        self.assertNotIn("IQ_PASSWORD", str(snapshot))


class RobotControlAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.variables = {"EXECUTAR_ORDENS": "true", "PRACTICE_CONTROL_ENABLED": "true",
                          "PRACTICE_FAIXAS_URL": "https://baskaraido-monitor-test.onrender.com/api/v1/practice/faixas",
                          "MONITOR_ROBOT_TOKEN": "offline-robot-token"}
        environment = patch.dict(os.environ, self.variables)
        environment.start()
        self.addCleanup(environment.stop)
        self.robot = robot(self.variables)
        self.robot.api = MagicMock()
        self.robot.api.get_balance.return_value = 1000
        self.robot.api.get_digital_current_profit.return_value = False
        self.ids = itertools.count(1001)
        self.order = patch.object(self.robot, "executar_ordem", side_effect=lambda *args: (True, next(self.ids)))
        self.orders = self.order.start()
        self.addCleanup(self.order.stop)
        for stub in (patch.object(self.robot, "garantir_practice", return_value=True),
                     patch.object(self.robot, "read_remote_snapshot", return_value=None)):
            stub.start()
            self.addCleanup(stub.stop)

    def command(self, revision=1, paused=False, session=None):
        return {"schema_version": 1, "conta": "PRACTICE", "revision": revision,
                "command_id": f"00000000-0000-0000-0000-{revision:012d}",
                "desired_paused": paused,
                "target_session": session or self.robot.sessao_monitor_practice,
                "lease_expires_at": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()}

    def send(self, bank="BANCA 1", level=0):
        return self.robot.enviar_ordem_gerida(bank, "TEST-OTC", "call", 1, nivel=level)

    def test_resume_requires_startup_authorization_and_current_session(self):
        for authorized, session in ((False, None), (True, "different-session")):
            self.robot.EXECUTAR_ORDENS = authorized
            with patch.object(self.robot, "read_remote_control", return_value=self.command(session=session)):
                self.assertFalse(self.send()[0])
                self.assertTrue(self.robot.controle_practice_confirmado["paused"])
        self.orders.assert_not_called()
        self.robot.EXECUTAR_ORDENS = True
        with patch.object(self.robot, "read_remote_control", return_value=self.command()):
            self.assertTrue(self.send()[0])

    def test_network_failure_closes_new_entries_and_removes_ack(self):
        with patch.object(self.robot, "read_remote_control", return_value=self.command()):
            self.assertTrue(self.send()[0])
            self.assertFalse(self.robot.controle_practice_confirmado["paused"])
        with patch.object(self.robot, "read_remote_control", return_value=None):
            self.assertFalse(self.send("BANCA 2")[0])
        self.assertIsNone(self.robot.controle_practice_confirmado)
        self.assertIsNone(self.robot.snapshot_monitor_practice()["control_ack"])
        self.assertEqual(self.orders.call_count, 1)

    def test_pause_leaves_already_accepted_gales_running(self):
        with patch.object(self.robot, "read_remote_control", return_value=self.command()):
            ok, first, context = self.send()
        self.assertTrue(ok)
        self.robot.registrar_resultado_ordem(first, "loss", -10)
        with patch.object(self.robot, "read_remote_control", return_value=self.command(2, True)):
            self.assertFalse(self.send("BANCA 2")[0])
            self.assertTrue(self.send(level=1)[0])
        self.assertTrue(self.robot.controle_practice_confirmado["paused"])
        self.assertEqual(self.orders.call_count, 2)
        self.assertEqual(context["gales"], 4)

    def test_rollback_and_expired_lease_cannot_release_entries(self):
        with patch.object(self.robot, "read_remote_control", return_value=self.command(2)):
            self.robot.carregar_controle_practice()
        self.assertTrue(self.robot.novas_entradas_practice_liberadas())
        with patch.object(self.robot, "read_remote_control", return_value=self.command(1)):
            self.assertIsNone(self.robot.carregar_controle_practice())
        self.assertFalse(self.robot.novas_entradas_practice_liberadas())
        with patch.object(self.robot, "read_remote_control", return_value=self.command(2)):
            self.robot.carregar_controle_practice()
        self.robot.controle_practice_valido_ate = 0
        self.assertFalse(self.robot.novas_entradas_practice_liberadas())
        self.assertIsNone(self.robot.snapshot_monitor_practice()["control_ack"])

    def test_lease_expiring_during_broker_queries_prevents_submission(self):
        def expire_while_reading_balance():
            self.robot.controle_practice_valido_ate = 0
            return 1000

        self.robot.api.get_balance.side_effect = expire_while_reading_balance
        with patch.object(self.robot, "read_remote_control", return_value=self.command()):
            self.assertFalse(self.send()[0])
        self.orders.assert_not_called()
        self.assertFalse(self.robot.reservas_pendentes)

    def test_same_revision_with_another_command_id_fails_closed(self):
        command = self.command()
        with patch.object(self.robot, "read_remote_control", return_value=command):
            self.assertIsNotNone(self.robot.carregar_controle_practice())
        changed = dict(command, command_id="11111111-1111-1111-1111-111111111111")
        with patch.object(self.robot, "read_remote_control", return_value=changed):
            self.assertIsNone(self.robot.carregar_controle_practice())
        self.assertFalse(self.robot.novas_entradas_practice_liberadas())
        self.assertIsNone(self.robot.snapshot_monitor_practice()["control_ack"])

    def test_pause_ack_waits_until_previous_initial_submission_finishes(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        self.robot.controle_practice = self.command()
        self.robot.controle_practice_confirmado = {"revision": 1, "command_id": self.command()["command_id"],
                                                   "paused": False, "authorized": True}
        self.robot.entradas_practice_pausadas = False
        self.robot.controle_practice_valido_ate = self.robot.time.monotonic() + 30

        def in_flight():
            with self.robot.contador_entrada_lock:
                entered.set()
                release.wait(5)

        def pause():
            self.robot.carregar_controle_practice()
            finished.set()

        thread = threading.Thread(target=in_flight)
        thread.start()
        self.assertTrue(entered.wait(5))
        with patch.object(self.robot, "read_remote_control", return_value=self.command(2, True)):
            application = threading.Thread(target=pause)
            application.start()
            self.assertFalse(finished.wait(.05))
            release.set()
            application.join(5)
        thread.join(5)
        self.assertTrue(finished.is_set())
        self.assertTrue(self.robot.controle_practice_confirmado["paused"])


if __name__ == "__main__":
    unittest.main()
