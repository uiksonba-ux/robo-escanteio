"""Scanners usam apenas API simulada; nenhum worker de negociação é iniciado."""

import threading
import time
import json
import inspect
import types
import unittest
from unittest.mock import MagicMock, patch

from test_gestao import carregar_robo


class EncerrarLoop(BaseException):
    pass


class WebSocketAppSimulado:
    """Espelha binding + _callback de websocket-client 0.56.0."""
    def __init__(self):
        self.recebidas = []
        self.on_message = self.processar_original

    def processar_original(self, mensagem):
        self.recebidas.append(mensagem)

    def receber_frame(self, mensagem):
        callback = self.on_message
        if inspect.ismethod(callback):
            callback(mensagem)
        else:
            callback(self, mensagem)


class ScannerLivenessTest(unittest.TestCase):
    def setUp(self):
        self.robo = carregar_robo()
        self.robo.api = MagicMock()

    def test_candles_travados_nao_mantem_api_lock_nem_criam_consultas_duplicadas(self):
        robo = self.robo
        iniciado = threading.Event()
        liberar = threading.Event()

        def consultar(*args):
            iniciado.set()
            liberar.wait(2)
            return []

        robo.api.get_candles.side_effect = consultar
        robo.PRAZO_CONSULTA_API = 0.01
        try:
            with patch.object(robo, "garantir_conexao", return_value=True):
                inicio = time.monotonic()
                self.assertEqual(robo.obter_candles("EURUSD-OTC", 60), [])
                self.assertTrue(iniciado.is_set())
                self.assertEqual(robo.obter_candles("GBPUSD-OTC", 300), [])
                self.assertLess(time.monotonic() - inicio, 0.5)
            robo.api.get_candles.assert_called_once()
            self.assertTrue(robo.api_lock.acquire(blocking=False))
            robo.api_lock.release()
            # O SDK alternativo pode reconectar. Enquanto ainda executa, não
            # pode concorrer com validação PRACTICE/envio sobre o saldo global.
            self.assertFalse(robo._sessao_lock.acquire(blocking=False))
        finally:
            liberar.set()

    def test_resposta_tardia_de_outro_ativo_nao_entra_nos_indicadores(self):
        robo = self.robo
        candles = [{"from": 0, "open": 1, "close": 2, "max": 3, "min": 0}]
        with (
            patch.object(robo, "garantir_conexao", return_value=True),
            patch.object(robo, "chamada_api_limitada", return_value=(
                "ok", (("EURUSD-OTC", 60, 100, 1000), candles))),
        ):
            self.assertEqual(robo.obter_candles("GBPUSD-OTC", 60), [])
        self.assertNotIn("GBPUSD-OTC", robo.ativos_validos)

    def test_candles_fechados_preservam_dados_e_timeframe(self):
        robo = self.robo
        fechados = [{"from": 1000 - (i + 1) * 60,
                     "open": 1, "close": 2, "max": 3, "min": 0}
                    for i in range(60)]
        robo.api.get_candles.return_value = fechados + [
            {"from": 1000, "open": 1, "close": 2, "max": 3, "min": 0}]
        with (
            patch.object(robo, "garantir_conexao", return_value=True),
            patch.object(robo.time, "time", return_value=1000),
        ):
            resultado = robo.obter_candles("EURUSD-OTC", 60)
        self.assertEqual(len(resultado), 60)
        self.assertEqual(resultado[0], {"from": 940, "open": 1.0,
                                       "close": 2.0, "max": 3.0, "min": 0.0})
        self.assertIn("EURUSD-OTC", robo.ativos_validos)
        robo.api.get_candles.assert_called_once_with("EURUSD-OTC", 60, 100, 1000)

    def test_candles_sem_conexao_nao_consultam_api(self):
        with patch.object(self.robo, "garantir_conexao", return_value=False):
            self.assertEqual(self.robo.obter_candles("EURUSD-OTC", 60), [])
        self.robo.api.get_candles.assert_not_called()

    def preparar_canal_raw(self):
        websocket = WebSocketAppSimulado()
        baixo = types.SimpleNamespace(digital_option_placed_id={}, websocket=websocket,
                                      send_websocket_request=MagicMock())
        self.robo.api.api = baixo
        self.robo.api.get_all_ACTIVES_OPCODE.return_value = {"EURUSD-OTC": 76,
                                                           "GBPUSD-OTC": 81}
        self.robo.PRAZO_CONSULTA_API = 0.01
        return baixo, websocket

    def test_canal_raw_correlaciona_callback_real_e_preserva_original(self):
        baixo, websocket = self.preparar_canal_raw()
        candles = [{"from": 900, "open": 1, "close": 2, "max": 3, "min": 0}]

        def enviar(**pedido):
            websocket.receber_frame(json.dumps({"name": "candles",
                "request_id": pedido["request_id"], "msg": {"candles": candles}}))

        baixo.send_websocket_request.side_effect = enviar
        with patch.object(self.robo, "garantir_conexao", return_value=True):
            self.assertEqual(len(self.robo.obter_candles("EURUSD-OTC", 60)), 1)
        self.assertEqual(len(websocket.recebidas), 1)
        self.robo.api.get_candles.assert_not_called()
        chamada = baixo.send_websocket_request.call_args.kwargs
        self.assertEqual(chamada["msg"]["body"]["active_id"], 76)
        self.assertEqual(chamada["msg"]["body"]["size"], 60)
        self.assertTrue(chamada["request_id"].startswith("robo-candles-"))

    def test_raw_sem_request_id_nao_inventa_correlacao_e_pode_tentar_novamente(self):
        baixo, websocket = self.preparar_canal_raw()

        def enviar(**pedido):
            websocket.receber_frame(json.dumps({"name": "candles", "msg": {
                "candles": [{"from": 900, "open": 1, "close": 2, "max": 3, "min": 0}]}}))

        baixo.send_websocket_request.side_effect = enviar
        with patch.object(self.robo, "garantir_conexao", return_value=True):
            self.assertEqual(self.robo.obter_candles("EURUSD-OTC", 60), [])
            self.assertEqual(self.robo.obter_candles("GBPUSD-OTC", 300), [])
        self.assertEqual(baixo.send_websocket_request.call_count, 2)
        self.assertEqual(len(websocket.recebidas), 2)
        self.assertEqual(websocket._robo_candles_receptor["pedidos"], {})

    def test_raw_timeout_nao_impede_novo_ativo_e_resposta_tardia_nao_contamina(self):
        baixo, websocket = self.preparar_canal_raw()
        anterior = []

        def enviar(**pedido):
            if not anterior:
                anterior.append(pedido["request_id"])
                return
            websocket.receber_frame(json.dumps({"name": "candles", "request_id": anterior[0],
                "msg": {"candles": [{"from": 900, "open": 99, "close": 99,
                                      "max": 99, "min": 99}]}}))
            websocket.receber_frame(json.dumps({"name": "candles", "request_id": pedido["request_id"],
                "msg": {"candles": [{"from": 900, "open": 1, "close": 2,
                                      "max": 3, "min": 0}]}}))

        baixo.send_websocket_request.side_effect = enviar
        with patch.object(self.robo, "garantir_conexao", return_value=True):
            self.assertEqual(self.robo.obter_candles("EURUSD-OTC", 60), [])
            resultado = self.robo.obter_candles("GBPUSD-OTC", 300)
        self.assertEqual(resultado[0]["open"], 1)
        self.assertEqual(baixo.send_websocket_request.call_count, 2)
        self.assertEqual(len(websocket.recebidas), 2)
        self.assertEqual(websocket._robo_candles_receptor["pedidos"], {})

    def test_raw_delega_todas_mensagens_ao_callback_original(self):
        baixo, websocket = self.preparar_canal_raw()
        # Instala o receptor sem receber dados nem consultar o SDK bloqueante.
        self.robo._consultar_candles_correlacionados(self.robo.api, "EURUSD-OTC", 60, 100, 1000)
        mensagens = ["json-invalido", json.dumps({"name": "timeSync", "msg": 1000}),
                    json.dumps({"name": "candles", "request_id": "desconhecido",
                                "msg": {"candles": []}})]
        for mensagem in mensagens:
            websocket.receber_frame(mensagem)
        self.assertEqual(websocket.recebidas, mensagens)

    def test_supervisor_detecta_thread_viva_sem_progresso_sem_duplicar(self):
        robo = self.robo
        robo.scanner_threads = {tf: MagicMock() for tf in robo.TIMEFRAMES}
        robo.scanner_atividade = {tf: 0 for tf in robo.TIMEFRAMES}
        for thread in robo.scanner_threads.values():
            thread.is_alive.return_value = True
        with (
            patch.object(robo, "iniciar_scanner") as iniciar,
            patch.object(robo.time, "monotonic", return_value=1000),
            patch.object(robo.time, "sleep", side_effect=EncerrarLoop),
        ):
            with self.assertRaises(EncerrarLoop):
                robo.supervisor()
        iniciar.assert_not_called()
        mensagens = [c.args[0] for c in robo.log.error.call_args_list]
        self.assertEqual(sum("SCANNER SEM PROGRESSO" in m for m in mensagens), 3)

    def test_scanner_registra_progresso_quando_nao_ha_conexao(self):
        robo = self.robo
        with (
            patch.object(robo, "garantir_conexao", return_value=False),
            patch.object(robo.time, "monotonic", return_value=1234),
            patch.object(robo.time, "sleep", side_effect=EncerrarLoop),
        ):
            with self.assertRaises(EncerrarLoop):
                robo.scanner_timeframe(15)
        self.assertEqual(robo.scanner_atividade[15], 1234)


if __name__ == "__main__":
    unittest.main()
