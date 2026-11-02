"""Contrato da corretora com canal/sessão simulados; nenhuma rede ou ordem real."""
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from test_gestao import carregar_robo


class BrokerExecutionTest(unittest.TestCase):
    def setUp(self):
        self.robo = carregar_robo({"EXECUTAR_ORDENS": "true", "CONTA": "REAL"})
        self.robo.PRAZO_ENVIO_DIGITAL = 0.005
        self.robo.PRAZO_CONSULTA_API = 0.02
        self.banca = self.robo.BANCAS[0]
        self.api = MagicMock()
        self.api.check_connect.return_value = True
        self.api.get_balance_mode.return_value = "PRACTICE"
        self.api.get_balance.return_value = 600
        self.api.get_all_ACTIVES_OPCODE.return_value = {"EURUSD-OTC": 76}
        self.canal = SimpleNamespace(digital_option_placed_id={}, order_async={},
                                    timesync=SimpleNamespace(server_timestamp=time.time()))
        self.canal.place_digital_option_v2 = MagicMock(side_effect=self.aceitar)
        self.api.api = self.canal
        self.robo.api = self.api
        self.disponibilidade = patch.object(self.robo, "ativo_digital_disponivel", return_value=True)
        self.payout = patch.object(self.robo, "obter_payout_digital", return_value=0.8)
        self.disponibilidade_mock = self.disponibilidade.start()
        self.payout_mock = self.payout.start()
        self.addCleanup(self.disponibilidade.stop)
        self.addCleanup(self.payout.stop)

    def aceitar(self, instrumento, ativo_id, valor):
        self.canal.digital_option_placed_id["request-1"] = 12345
        return "request-1"

    def enviar(self, valor=6, expiracao=1, direcao="call"):
        return self.robo.executar_ordem(self.banca, "EURUSD-OTC", valor, direcao, expiracao)

    def test_v2_aceita_so_com_id_confirmado_sem_wrapper_bloqueante(self):
        self.assertEqual(self.enviar(), (True, 12345))
        self.canal.place_digital_option_v2.assert_called_once()
        instrumento, ativo_id, valor = self.canal.place_digital_option_v2.call_args.args
        self.assertTrue(instrumento.startswith("do76A"))
        self.assertTrue(instrumento.endswith("00T1MCSPT"))
        self.assertEqual((ativo_id, valor), ("76", 6))
        self.api.buy_digital_spot_v2.assert_not_called()
        self.api.buy_digital_spot.assert_not_called()
        self.api.buy.assert_not_called()
        self.assertEqual(self.robo.stats["ordens_aceitas"], 1)
        self.assertEqual(self.robo.stats["ordens_recusadas"], 0)

    def test_rejeicao_iq_confirmada_preserva_codigo_seguro(self):
        def rejeitar(*args):
            self.canal.digital_option_placed_id["request-1"] = {
                "code": "error_place_digital_order", "message": "rejected",
                "session": "segredo-que-nao-pode-aparecer"}
            return "request-1"
        self.canal.place_digital_option_v2.side_effect = rejeitar
        self.assertEqual(self.enviar(), (False, "error_place_digital_order:rejected"))
        self.assertEqual(self.robo.stats["ordens_recusadas"], 1)
        self.assertNotIn("segredo-que-nao-pode-aparecer", str(self.robo.log.mock_calls))

    def test_timeout_preserva_request_e_aceitacao_tardia_nao_reenvia(self):
        self.canal.place_digital_option_v2.side_effect = None
        self.canal.place_digital_option_v2.return_value = "request-1"
        self.assertIsNone(self.enviar()[0])
        self.assertIsNone(self.robo.consultar_confirmacao_envio(self.banca)[0])
        self.canal.digital_option_placed_id["request-1"] = 12345
        self.assertEqual(self.robo.consultar_confirmacao_envio(self.banca), (True, 12345))
        self.assertIsNone(self.robo.consultar_confirmacao_envio(self.banca)[0])
        self.canal.place_digital_option_v2.assert_called_once()
        self.assertEqual(self.robo.stats["ordens_aceitas"], 1)

    def test_recusa_tardia_comprovada_nao_gera_ordem_duplicada(self):
        self.canal.place_digital_option_v2.side_effect = None
        self.canal.place_digital_option_v2.return_value = "request-1"
        self.assertIsNone(self.enviar()[0])
        self.canal.digital_option_placed_id["request-1"] = {
            "code": "error_place_digital_order", "message": "rejected"}
        self.assertEqual(self.robo.consultar_confirmacao_envio(self.banca),
                         (False, "error_place_digital_order:rejected"))
        self.canal.place_digital_option_v2.assert_called_once()

    def test_excecao_apos_chamada_raw_permanece_indeterminada_sem_repetir(self):
        self.canal.place_digital_option_v2.side_effect = RuntimeError("senha=session-secreta")
        self.assertIsNone(self.enviar()[0])
        for _ in range(3):
            self.assertIsNone(self.robo.consultar_confirmacao_envio(self.banca)[0])
        self.canal.place_digital_option_v2.assert_called_once()
        self.assertEqual(self.robo.stats["ordens_recusadas"], 0)
        self.assertNotIn("session-secreta", str(self.robo.log.mock_calls))

    def test_excecao_wrapper_tambem_nunca_dispara_nova_tentativa(self):
        self.api.api = SimpleNamespace()
        self.api.buy_digital_spot_v2.side_effect = RuntimeError("segredo")
        self.assertIsNone(self.enviar()[0])
        for _ in range(3):
            self.assertIsNone(self.robo.consultar_confirmacao_envio(self.banca)[0])
        self.api.buy_digital_spot_v2.assert_called_once()

    def test_wrapper_bloqueado_tem_uma_chamada_e_confirma_resposta_tardia(self):
        liberar = threading.Event()
        self.addCleanup(liberar.set)
        self.api.api = SimpleNamespace()
        def compra(*args):
            liberar.wait(2)
            return True, 23456
        self.api.buy_digital_spot_v2.side_effect = compra
        self.assertIsNone(self.enviar()[0])
        self.assertIsNone(self.robo.consultar_confirmacao_envio(self.banca)[0])
        self.api.buy_digital_spot_v2.assert_called_once()
        liberar.set()
        time.sleep(0.01)
        self.assertEqual(self.robo.consultar_confirmacao_envio(self.banca), (True, 23456))
        self.api.buy_digital_spot_v2.assert_called_once()

    def test_request_id_invalido_tardio_nunca_reenvia(self):
        liberar = threading.Event()
        self.addCleanup(liberar.set)
        def enviar(*args):
            liberar.wait(2)
            return None
        self.canal.place_digital_option_v2.side_effect = enviar
        self.assertIsNone(self.enviar()[0])
        liberar.set()
        time.sleep(.01)
        for _ in range(3):
            self.assertIsNone(self.robo.consultar_confirmacao_envio(self.banca)[0])
        self.canal.place_digital_option_v2.assert_called_once()

    def test_request_id_reutilizado_nao_confunde_recusa_velha_com_novo_envio(self):
        self.canal.digital_option_placed_id["request-1"] = {
            "code": "error_place_digital_order", "message": "rejected"}
        self.canal.place_digital_option_v2.side_effect = None
        self.canal.place_digital_option_v2.return_value = "request-1"
        self.assertIsNone(self.enviar()[0])
        self.assertIsNone(self.robo.consultar_confirmacao_envio(self.banca)[0])
        self.assertEqual(self.robo.stats["ordens_recusadas"], 0)
        self.canal.place_digital_option_v2.assert_called_once()

    def test_lock_de_sessao_impede_reconnect_alterar_conta_durante_envio(self):
        self.robo.PRAZO_ENVIO_DIGITAL = .5
        iniciou = threading.Event()
        liberar = threading.Event()
        reconectou = threading.Event()
        self.addCleanup(liberar.set)
        modo = {"valor": "PRACTICE"}
        self.api.get_balance_mode.side_effect = lambda: modo["valor"]
        observados = []
        def enviar(*args):
            observados.append(modo["valor"])
            iniciou.set()
            liberar.wait(2)
            observados.append(modo["valor"])
            self.canal.digital_option_placed_id["request-1"] = 12345
            return "request-1"
        self.canal.place_digital_option_v2.side_effect = enviar
        outro = MagicMock()
        def conectar():
            modo["valor"] = "REAL"
            reconectou.set()
            return True, None
        outro.connect.side_effect = conectar
        resultados = []
        thread_envio = threading.Thread(target=lambda: resultados.append(self.enviar()))
        thread_envio.start()
        self.assertTrue(iniciou.wait(1))
        thread_connect = threading.Thread(target=lambda: self.robo._conectar_na_sessao(outro))
        thread_connect.start()
        self.assertFalse(reconectou.wait(.02))
        liberar.set()
        thread_envio.join(1)
        thread_connect.join(1)
        self.assertEqual(observados, ["PRACTICE", "PRACTICE"])
        self.assertEqual(resultados, [(True, 12345)])
        self.assertTrue(reconectou.is_set())

    def test_troca_de_cliente_antes_da_invocacao_e_recusa_sem_envio(self):
        outro = MagicMock()
        def substituir(cliente, funcao):
            self.robo.api = outro
            return self.original_enviar(cliente, funcao)
        self.original_enviar = self.robo._enviar_na_sessao_practice
        with patch.object(self.robo, "_enviar_na_sessao_practice", side_effect=substituir):
            self.assertEqual(self.enviar(), (False, "practice_ou_sessao_alterada_antes_do_envio"))
        self.canal.place_digital_option_v2.assert_not_called()
        self.assertFalse(any("ORDEM ENVIADA" in str(c.args[0])
                             for c in self.robo.log.info.call_args_list))
        self.assertTrue(any("ORDEM NAO ENVIADA" in str(c.args[0])
                            for c in self.robo.log.warning.call_args_list))

    def test_dicionario_desconhecido_ou_id_falso_nao_prova_aceitacao_nem_recusa(self):
        for resposta in ({"unexpected": 123}, (True, None), (True, False),
                         ("false", 123), (True, {"id": 123}), True, None):
            with self.subTest(resposta=resposta):
                self.assertIsNone(self.robo._normalizar_confirmacao(resposta)[0])
        self.assertEqual(self.robo._normalizar_confirmacao((True, "123")), (True, "123"))

    def test_conta_real_ou_modo_nao_confirmado_impede_envio(self):
        for modo in ("REAL", None, "TOURNAMENT"):
            with self.subTest(modo=modo):
                self.api.get_balance_mode.return_value = modo
                self.assertEqual(self.enviar(), (False, "sem_conexao"))
        self.canal.place_digital_option_v2.assert_not_called()
        self.assertTrue(all(c.args == ("PRACTICE",) for c in self.api.change_balance.call_args_list))

    def test_revalidacao_do_modo_imediatamente_antes_do_envio(self):
        self.api.get_balance_mode.side_effect = ["PRACTICE"] * 5 + ["REAL"]
        self.assertEqual(self.enviar(), (False, "practice_nao_confirmada_antes_do_envio"))
        self.canal.place_digital_option_v2.assert_not_called()

    def test_ativo_sem_id_ou_sessao_com_relogio_antigo_nao_envia(self):
        self.api.get_all_ACTIVES_OPCODE.return_value = {}
        self.assertEqual(self.enviar(), (False, "ativo_sem_id_confirmado"))
        self.api.get_all_ACTIVES_OPCODE.return_value = {"EURUSD-OTC": 76}
        self.canal.timesync.server_timestamp = time.time() - 600
        self.assertEqual(self.enviar(), (False, "relogio_da_sessao_desatualizado"))
        self.canal.place_digital_option_v2.assert_not_called()

    def test_m1_m5_m15_e_put_preservam_protocolo_e_janela_segura(self):
        for expiracao in (1, 5, 15):
            with self.subTest(expiracao=expiracao):
                self.robo._envios_digitais.clear()
                self.canal.digital_option_placed_id.clear()
                self.assertEqual(self.enviar(expiracao=expiracao, direcao="put"), (True, 12345))
                instrumento = self.canal.place_digital_option_v2.call_args.args[0]
                self.assertTrue(instrumento.endswith(f"00T{expiracao}MPSPT"))

    def test_preflight_disponibilidade_e_payout_saldo_expiracao(self):
        for disponivel, payout, motivo in ((None, .8, "disponibilidade_digital_nao_confirmada"),
                                           (True, None, "payout_digital_nao_confirmado"),
                                           (True, 0, "payout_digital_indisponivel")):
            with self.subTest(motivo=motivo):
                self.disponibilidade_mock.return_value = disponivel
                self.payout_mock.return_value = payout
                self.assertEqual(self.enviar(), (False, motivo))
        self.assertEqual(self.enviar(expiracao=2), (False, "expiracao_digital_invalida"))
        self.assertEqual(self.enviar(valor=601), (False, "saldo_insuficiente"))
        self.canal.place_digital_option_v2.assert_not_called()

    def test_saldo_indisponivel_nao_reutiliza_cache(self):
        self.robo.ultimo_saldo_practice = 600
        self.api.get_balance.return_value = float("nan")
        self.assertIsNone(self.robo.obter_saldo_atual())
        self.assertEqual(self.enviar(), (False, "saldo_indisponivel"))
        self.canal.place_digital_option_v2.assert_not_called()

    def test_timeout_de_consulta_nao_sequestra_api_lock_nem_cria_workers_repetidos(self):
        liberar = threading.Event()
        self.addCleanup(liberar.set)
        funcao = MagicMock(side_effect=lambda: liberar.wait(2))
        self.assertEqual(self.robo.chamada_api_limitada("travada", funcao, timeout=.001)[0], "timeout")
        self.assertEqual(self.robo.chamada_api_limitada("travada", funcao, timeout=.001)[0], "timeout")
        funcao.assert_called_once()
        self.assertTrue(self.robo.api_lock.acquire(blocking=False))
        self.robo.api_lock.release()

    def test_catalogo_digital_direto_preserva_otc_e_rejeita_catalogo_de_sessao_antiga(self):
        self.disponibilidade.stop()
        self.api.get_digital_underlying_list_data.return_value = {"underlying": [
            {"underlying": "EURUSD-OTC", "schedule": [{"open": time.time()-10, "close": time.time()+100}]},
            {"underlying": "GOOGLE-OTC", "schedule": [{"open": 0, "close": 1}]},
        ]}
        self.assertTrue(self.robo.ativo_digital_disponivel("EURUSD-OTC"))
        self.assertFalse(self.robo.ativo_digital_disponivel("GOOGLE-OTC"))
        self.api.get_all_open_time.assert_not_called()
        self.api.get_digital_underlying_list_data.assert_called_once()
        self.robo._disponibilidade_cache["sessao"] = -1
        self.api.get_digital_underlying_list_data.return_value = None
        self.assertIsNone(self.robo.ativo_digital_disponivel("EURUSD-OTC"))

    def test_payout_quotes_spt_por_periodo_usa_menor_lado_e_nao_getter_sem_handler(self):
        self.payout.stop()
        self.canal.instrument_quites_generated_data = {"EURUSD-OTC": {
            60: {"do76A20260101D010100T1MCSPT": 82, "do76A20260101D010100T1MPSPT": 76},
            300: {"do76A20260101D010500T5MCSPT": 70, "do76A20260101D010500T5MPSPT": 72}}}
        self.canal.instrument_quites_generated_timestamp = {"EURUSD-OTC": {
            60: time.time()+90, 300: time.time()+240}}
        self.canal.unsubscribe_instrument_quites_generated = MagicMock()
        self.assertEqual(self.robo.obter_payout_digital("EURUSD-OTC", 1), .76)
        self.assertEqual(self.robo.obter_payout_digital("EURUSD-OTC", 5), .70)
        self.api.subscribe_strike_list.assert_any_call("EURUSD-OTC", 1)
        self.api.subscribe_strike_list.assert_any_call("EURUSD-OTC", 5)
        self.canal.unsubscribe_instrument_quites_generated.assert_any_call("EURUSD-OTC", 1)
        self.canal.unsubscribe_instrument_quites_generated.assert_any_call("EURUSD-OTC", 5)
        self.assertIn(60, self.canal.instrument_quites_generated_data["EURUSD-OTC"])
        self.api.get_digital_payout.assert_not_called()

    def test_payout_quotes_vencidas_ou_sem_dois_lados_fica_nao_confirmado(self):
        self.payout.stop()
        self.robo.PRAZO_CONSULTA_API = .001
        self.canal.instrument_quites_generated_data = {"EURUSD-OTC": {60: {"CSPT": 80, "PSPT": 80}}}
        self.canal.instrument_quites_generated_timestamp = {"EURUSD-OTC": {60: time.time()-1}}
        self.canal.unsubscribe_instrument_quites_generated = MagicMock()
        self.assertIsNone(self.robo.obter_payout_digital("EURUSD-OTC", 1))
        self.canal.instrument_quites_generated_timestamp["EURUSD-OTC"][60] = time.time()+90
        self.canal.instrument_quites_generated_data["EURUSD-OTC"][60] = {"CSPT": 80}
        self.assertIsNone(self.robo.obter_payout_digital("EURUSD-OTC", 1))

    def test_payout_observado_por_sessao_sem_inventar_valor(self):
        self.payout.stop()
        self.api.get_digital_payout.return_value = 75
        self.assertEqual(self.robo.obter_payout_digital("EURUSD-OTC", 1), .75)
        self.assertEqual(self.robo.obter_payout_digital("EURUSD-OTC", 1), .75)
        self.api.get_digital_payout.assert_called_once_with("EURUSD-OTC", seconds=2)
        for valor in (None, float("nan"), -1, 101, True):
            self.robo._payout_cache.clear()
            self.api.get_digital_payout.return_value = valor
            self.assertIsNone(self.robo.obter_payout_digital("EURUSD-OTC", 1))

    def test_resultado_raw_win_loss_draw_sem_check_win_bloqueante(self):
        for lucro, esperado in ((4.8, "win"), (-6, "loss"), (0, "draw")):
            with self.subTest(resultado=esperado):
                self.canal.order_async[12345] = {"position-changed": {"msg": {
                    "status": "closed", "close_reason": "default", "pnl_realized": lucro}}}
                self.assertEqual(self.robo.aguardar_resultado(12345), (esperado, lucro))
        self.api.check_win_digital_v2.assert_not_called()

    def test_resultado_preserva_buffer_original_apos_troca_de_sessao(self):
        self.assertEqual(self.enviar(), (True, 12345))
        self.canal.order_async[12345] = {"position-changed": {"msg": {
            "status": "closed", "close_reason": "default", "pnl_realized": 4.8}}}
        outra = MagicMock()
        outra.check_connect.return_value = True
        outra.api = SimpleNamespace(order_async={})
        self.robo.api = outra
        self.assertEqual(self.robo.aguardar_resultado(12345), ("win", 4.8))

    def test_resultado_expirado_usa_lucro_liquido_e_timeout_nao_vira_draw(self):
        self.canal.order_async[12345] = {"position-changed": {"msg": {
            "status": "closed", "close_reason": "expired", "close_profit": 10.8, "invest": 6}}}
        resultado, lucro = self.robo.aguardar_resultado(12345)
        self.assertEqual(resultado, "win")
        self.assertAlmostEqual(lucro, 4.8)
        self.robo.PRAZO_RESULTADO_DIGITAL = .001
        with patch.object(self.robo.time, "sleep"):
            self.assertEqual(self.robo.aguardar_resultado(999), ("timeout", 0))


if __name__ == "__main__":
    unittest.main()
