import threading
import unittest
from unittest import mock

# Keep this test isolated if import-time threads are ever added again.
_original_start = threading.Thread.start
threading.Thread.start = lambda self: None
try:
    import robo
finally:
    threading.Thread.start = _original_start


class IndeterminateOrderIsolationTests(unittest.TestCase):
    def setUp(self):
        with robo.estado_lock:
            robo.envios_indeterminados.clear()
            robo.ativos_em_uso.clear()
            for banca in robo.BANCAS:
                robo.estado_frentes[banca].update(
                    ocupada=False,
                    ativo=None,
                    ciclo_em_execucao=False,
                    envio_indeterminado=False,
                )

            # A sent order with unknown confirmation must retain both its bank
            # and asset reservation while reconciliation continues.
            banca = robo.BANCAS[0]
            ativo = "WLDAUD"
            robo.estado_frentes[banca].update(ocupada=True, ativo=ativo)
            robo.ativos_em_uso.add(ativo)
            robo.envios_indeterminados[banca] = {
                "banca": banca,
                "ativo": ativo,
                "nivel": 0,
                "valor": 2.0,
                "motivo": "timeout",
            }
            robo.estado_frentes[banca]["envio_indeterminado"] = True

    def test_new_signals_pause_while_any_order_is_ambiguous(self):
        self.assertIsNone(robo.reservar_banca("TEST0", "call", 80, 1, 1))
        self.assertEqual(robo.estado_frentes[robo.BANCAS[0]]["ativo"], "WLDAUD")
        self.assertIn("WLDAUD", robo.ativos_em_uso)

    def test_unknown_order_asset_cannot_be_reused(self):
        self.assertIsNone(
            robo.reservar_banca("WLDAUD", "put", 90, 1, 1)
        )
        self.assertIn("WLDAUD", robo.ativos_em_uso)

    def test_invalid_confirmed_id_keeps_context_for_reconciliation(self):
        banca = robo.BANCAS[0]
        contexto = {"numero": 1, "entrada": 2.0, "gales": 4}
        with robo.estado_lock:
            robo.envios_indeterminados.clear()
            robo.estado_frentes[banca]["envio_indeterminado"] = False
            robo.ordens_pendentes[12345] = {"banca": "BANCA 0", "valor": 2.0, "nivel": 0}

        ok, motivo, _ = robo.confirmar_ordem_gerida(
            banca, "WLDAUD", 0, 2.0, 12345, contexto
        )

        self.assertIsNone(ok)
        self.assertEqual(motivo, "order_id_invalido_ou_repetido")
        self.assertEqual(robo.envios_indeterminados[banca]["contexto"], contexto)

    def test_raw_v2_missing_request_id_never_reinvokes_buy(self):
        banca = robo.BANCAS[0]
        send = mock.Mock(return_value=(True, 98765))
        client = mock.Mock()
        with robo._envios_digitais_lock:
            robo._envios_digitais.clear()
            robo._envios_digitais[banca] = {
                "falha_envio": False,
                "modo": "raw_v2",
                "request_id": None,
                "request_ids_anteriores": frozenset(),
                "chave": "envio_v2:" + banca,
                "funcao": send,
                "cliente": client,
                "ativo": "R_50",
                "expiracao": 1,
            }

        ok, reason = robo.consultar_confirmacao_envio(banca)

        self.assertIsNone(ok)
        self.assertEqual(reason, "request_id_ausente_conciliacao_no_extrato")
        send.assert_not_called()
        self.assertTrue(robo._envios_digitais[banca]["falha_envio"])
        with robo._envios_digitais_lock:
            robo._envios_digitais.clear()

    def test_statement_match_requires_demo_app_symbol_direction_and_stake(self):
        envio = {
            "cliente": mock.Mock(app_id="12345"),
            "submitted_at": 1_800_000_000,
            "ativo": "R_50",
            "direcao": "call",
            "valor": 0.35,
        }
        rows = [
            {"action_type":"buy", "app_id":"12345", "amount":0.35,
             "transaction_time":1_800_000_001, "shortcode":"CALL_R_50_1HZ",
             "contract_id":9876},
            {"action_type":"buy", "app_id":"99999", "amount":0.35,
             "transaction_time":1_800_000_001, "shortcode":"CALL_R_50_1HZ",
             "contract_id":9877},
            {"action_type":"buy", "app_id":"12345", "amount":0.35,
             "transaction_time":1_800_000_001, "shortcode":"PUT_R_50_1HZ",
             "contract_id":9878},
            {"action_type":"buy", "app_id":"12345", "amount":1.05,
             "transaction_time":1_800_000_001, "shortcode":"CALL_R_50_1HZ",
             "contract_id":9879},
        ]

        self.assertEqual(robo._extrato_corresponde_ao_envio(rows, envio), [9876])

    def test_multiple_statement_matches_remain_ambiguous(self):
        envio = {
            "cliente": mock.Mock(app_id="12345"),
            "submitted_at": 1_800_000_000,
            "ativo": "R_50",
            "direcao": "put",
            "valor": 0.35,
        }
        rows = [
            {"action_type":"buy", "app_id":"12345", "amount":0.35,
             "transaction_time":1_800_000_001, "shortcode":"PUT_R_50_1HZ",
             "contract_id":9876},
            {"action_type":"buy", "app_id":"12345", "amount":0.35,
             "transaction_time":1_800_000_002, "shortcode":"PUT_R_50_1HZ",
             "contract_id":9877},
        ]

        self.assertEqual(len(robo._extrato_corresponde_ao_envio(rows, envio)), 2)

    def test_recovery_is_100_percent_of_bank_deficit(self):
        self.assertEqual(robo.RECUPERACAO_PERCENTUAL, 1.0)
        with mock.patch.object(robo, "obter_meta_lucro_conta", return_value=2.0):
            alvo = robo.calcular_alvo_recuperacao(4.55)
            self.assertEqual(alvo, 4.55)
            self.assertEqual(robo.calcular_entrada_recuperacao(alvo, 0.89, lucro_alvo=2.0), 7.36)
        self.assertEqual(robo.valores_do_ciclo(robo.ENTRADA_BASE_BRL), [20.0])

    def test_requested_base_gale_and_recovery_settings(self):
        self.assertEqual(robo.QTD_BANCAS, 1)
        self.assertEqual(len(robo.BANCAS), 1)
        self.assertEqual(robo.BANCAS, ["BANCA 1"])
        self.assertEqual(set(robo.estado_frentes), set(robo.BANCAS))
        self.assertEqual(robo.ENTRADA_BASE_BRL, 20.0)
        self.assertEqual(robo.MULTIPLICADOR_GALE, 1.0)
        self.assertEqual(robo.MULTIPLICADORES_POR_GALES, {0: 1.0})
        self.assertEqual(
            [robo.multiplicador_por_gales(n) for n in range(3)],
            [1.0] * 3,
        )
        self.assertEqual(robo.GALES_POR_CICLO, 0)
        self.assertEqual(robo.MAX_GALES, 0)
        self.assertEqual(robo.RECUPERACAO_PERCENTUAL, 1.0)
        self.assertEqual(set(robo.TIMEFRAMES), {5, 30})
        self.assertEqual(robo.PRE_STOP_LOSS_BRL, 100.0)
        with mock.patch.object(robo, "obter_limite_pre_stop_loss_conta", return_value=100.0):
            self.assertFalse(robo.pre_stop_loss_atingido(99.64, 0.35))
            self.assertTrue(robo.pre_stop_loss_atingido(99.66, 0.35))
            self.assertTrue(robo.pre_stop_loss_atingido(100.0, 0.0))
        self.assertEqual(
            robo.valores_do_ciclo(robo.ENTRADA_BASE_BRL),
            [20.0],
        )

    def test_finalized_win_clears_bank_deficit_and_recovery(self):
        banca_a = robo.BANCAS[0]
        with robo.estado_lock:
            robo.operacoes_ativas.clear()
            robo.ordens_pendentes.clear()
            robo.operacoes_concluidas.clear()
            robo.recuperacoes_em_curso.clear()
            for banca in robo.BANCAS:
                robo.estado_frentes[banca].update(
                    recuperacao_pendente=0.0,
                    resultado_liquido=0.0,
                    prejuizo_acumulado=0.0,
                    envio_indeterminado=False,
                )
            robo.estado_frentes[banca_a]["resultado_liquido"] = -0.35
            robo.estado_frentes[banca_a]["recuperacao_pendente"] = 0.35
            robo.operacoes_ativas[banca_a] = {
                "primeira_ordem": "TEST-RECOVERY-WIN",
                "resultado_centavos": 50,
                "prejuizo_alocado": 0.35,
            }
        robo.finalizar_operacao_global(banca_a)
        self.assertEqual(robo.prejuizo_banca_atual(banca_a), 0.0)
        self.assertEqual(robo.estado_frentes[banca_a]["prejuizo_acumulado"], 0.0)

    def test_new_loss_updates_bank_deficit_and_100_percent_recovery_debt(self):
        banca_a = robo.BANCAS[0]
        with robo.estado_lock:
            robo.operacoes_ativas.clear()
            robo.ordens_pendentes.clear()
            robo.operacoes_concluidas.clear()
            robo.recuperacoes_em_curso.clear()
            for banca in robo.BANCAS:
                robo.estado_frentes[banca].update(
                    recuperacao_pendente=0.0,
                    resultado_liquido=0.0,
                    prejuizo_acumulado=0.0,
                    envio_indeterminado=False,
                )
            robo.estado_frentes[banca_a]["resultado_liquido"] = -0.35
            robo.estado_frentes[banca_a]["recuperacao_pendente"] = 0.35
            robo.operacoes_ativas[banca_a] = {
                "primeira_ordem": "TEST-DEFERRED-LOSS",
                "resultado_centavos": -35,
                "prejuizo_alocado": 0.0,
            }
        robo.finalizar_operacao_global(banca_a)
        self.assertEqual(robo.estado_frentes[banca_a]["prejuizo_acumulado"], 0.70)
        self.assertEqual(robo.estado_frentes[banca_a]["recuperacao_pendente"], 0.70)
        self.assertEqual(robo.prejuizo_banca_atual(banca_a), 0.70)

    def test_single_bank_reservation_preserves_signal_direction(self):
        with robo.estado_lock:
            robo.envios_indeterminados.clear()
            robo.ativos_em_uso.clear()
            for banca in robo.BANCAS:
                robo.estado_frentes[banca].update(
                    ocupada=False, ativo=None, direcao=None, score=None,
                    timeframe=None, expiracao=None, envio_indeterminado=False,
                )
            robo.proxima_banca_indice = 0
        primeira = robo.reservar_banca("R_50", "call", 82, 5, 5)
        segunda = robo.reservar_banca("BOOM100", "put", 78, 5, 5)
        self.assertEqual(primeira, "BANCA 1")
        self.assertIsNone(segunda)
        self.assertEqual(robo.estado_frentes["BANCA 1"]["direcao"], "call")
        self.assertEqual(robo.estado_frentes["BANCA 1"]["ativo"], "R_50")

    def test_accuracy_uses_only_real_results(self):
        banca = robo.BANCAS[0]
        with robo.estado_lock:
            dados = robo.estado_frentes[banca]
            dados.update(
                wins=0,
                losses=0,
                wins_base_janela=0,
                losses_base_janela=0,
            )

        self.assertEqual(robo.taxa_banca_real(banca), 0.0)
        self.assertEqual(robo.gales_por_assertividade(0.0), 4)

        with robo.estado_lock:
            robo.estado_frentes[banca]["wins"] = 1
        self.assertEqual(robo.taxa_banca_real(banca), 100.0)
        self.assertEqual(robo.gales_por_assertividade(100.0), 0)

        with robo.estado_lock:
            robo.estado_frentes[banca].update(wins=0, losses=1)
        self.assertEqual(robo.taxa_banca_real(banca), 0.0)
        self.assertEqual(robo.gales_por_assertividade(0.0), 4)

    def test_pre_stop_resets_bank_financial_management_and_unpauses(self):
        banca = robo.BANCAS[0]
        fields = (
            "pre_stop_loss_ativo", "pre_stop_loss_count",
            "pre_stop_loss_ultima_perda", "resultado_liquido",
            "prejuizo_acumulado", "recuperacao_pendente",
            "em_recuperacao", "gale_progression_level", "gale_atual",
            "gales_reativos", "entradas_recuperacao_restantes",
            "recuperacao_estendida", "ocupada", "ativo",
        )
        with robo.estado_lock:
            dados = robo.estado_frentes[banca]
            saved = {field: dados[field] for field in fields}
            dados.update(
                pre_stop_loss_ativo=True,
                pre_stop_loss_count=2,
                pre_stop_loss_ultima_perda=0.0,
                resultado_liquido=-100.25,
                prejuizo_acumulado=100.25,
                recuperacao_pendente=100.25,
                em_recuperacao=True,
                gale_progression_level=3,
                gale_atual=3,
                gales_reativos=3,
                entradas_recuperacao_restantes=1,
                recuperacao_estendida=True,
                ocupada=False,
                ativo=None,
            )
            recuperacao_antes = robo.recuperacao_pendente_total()

        try:
            with mock.patch.object(robo, "telegram"):
                self.assertTrue(robo.reiniciar_gestao_banca_apos_pre_stop(banca, 100.25))
            dados = robo.estado_frentes[banca]
            self.assertFalse(dados["pre_stop_loss_ativo"])
            self.assertEqual(dados["pre_stop_loss_count"], 3)
            self.assertEqual(dados["pre_stop_loss_ultima_perda"], 100.25)
            self.assertEqual(dados["resultado_liquido"], 0.0)
            self.assertEqual(dados["prejuizo_acumulado"], 0.0)
            self.assertEqual(dados["recuperacao_pendente"], 0.0)
            self.assertFalse(dados["em_recuperacao"])
            self.assertEqual(dados["gale_progression_level"], 0)
            self.assertEqual(dados["gales_reativos"], 0)
            self.assertEqual(dados["entradas_recuperacao_restantes"], 0)
            self.assertFalse(dados["recuperacao_estendida"])
            self.assertEqual(robo.recuperacao_pendente_total(), 0.0)
            self.assertEqual(recuperacao_antes, 100.25)
        finally:
            with robo.estado_lock:
                robo.estado_frentes[banca].update(saved)

    def test_bank_window_restarts_after_300_global_closed_signals(self):
        banca = robo.BANCAS[0]
        fields = ("wins", "losses", "wins_base_janela", "losses_base_janela")
        with robo.estado_lock:
            saved_banks = {
                name: {field: robo.estado_frentes[name][field] for field in fields}
                for name in robo.BANCAS
            }
            saved_totals = (robo.stats["wins"], robo.stats["losses"])
            saved_global = (
                robo.sinais_fechados_janela_global,
                robo.wins_base_janela_global,
                robo.losses_base_janela_global,
            )
            for name in robo.BANCAS:
                robo.estado_frentes[name].update(
                    wins=0, losses=0, wins_base_janela=0, losses_base_janela=0,
                )
            robo.stats.update(wins=0, losses=0)
            robo.sinais_fechados_janela_global = 0
            robo.wins_base_janela_global = 0
            robo.losses_base_janela_global = 0

        try:
            for _ in range(199):
                robo.registrar_win(banca, 0)
                self.assertFalse(robo.registrar_operacao_fechada_assertividade(banca))
            for _ in range(100):
                robo.registrar_loss(banca)
                self.assertFalse(robo.registrar_operacao_fechada_assertividade(banca))

            self.assertEqual(robo.sinais_fechados_janela_global, 299)
            self.assertAlmostEqual(robo.taxa_banca_real(banca), 66.56, places=2)

            robo.registrar_loss(banca)
            self.assertTrue(robo.registrar_operacao_fechada_assertividade(banca))
            self.assertEqual(robo.sinais_fechados_janela_global, 0)
            self.assertEqual(robo.estado_frentes[banca]["wins_base_janela"], 199)
            self.assertEqual(robo.estado_frentes[banca]["losses_base_janela"], 101)
            self.assertEqual(robo.taxa_banca_real(banca), 0.0)
            self.assertEqual(robo.wins_base_janela_global, robo.stats["wins"])
            self.assertEqual(robo.losses_base_janela_global, robo.stats["losses"])
        finally:
            with robo.estado_lock:
                for name, values in saved_banks.items():
                    robo.estado_frentes[name].update(values)
                robo.stats["wins"], robo.stats["losses"] = saved_totals
                (
                    robo.sinais_fechados_janela_global,
                    robo.wins_base_janela_global,
                    robo.losses_base_janela_global,
                ) = saved_global

    def test_legacy_assertiveness_band_boundaries(self):
        cases = (
            (49.99, 4), (50.0, 3), (59.99, 3),
            (60.0, 2), (69.99, 2), (70.0, 1),
            (79.99, 1), (80.0, 0), (100.0, 0),
        )
        for accuracy, expected_gales in cases:
            with self.subTest(accuracy=accuracy):
                self.assertEqual(robo.gales_por_assertividade(accuracy), expected_gales)

    def test_loss_with_positive_bank_net_does_not_create_recovery_debt(self):
        banca = robo.BANCAS[0]
        with robo.estado_lock:
            robo.operacoes_ativas.clear()
            robo.envios_indeterminados.clear()
            robo.reservas_pendentes.clear()
            robo.ordens_pendentes.clear()
            robo.ordens_liquidadas.clear()
            robo.operacoes_concluidas.clear()
            robo.recuperacoes_em_curso.clear()
            robo.resultado_global_centavos = 0
            for name in robo.BANCAS:
                dados = robo.estado_frentes[name]
                dados.update(
                    wins=0,
                    losses=0,
                    wins_base_janela=0,
                    losses_base_janela=0,
                    resultado_liquido=0.0,
                    prejuizo_acumulado=0.0,
                    recuperacao_pendente=0.0,
                    envio_indeterminado=False,
                    em_recuperacao=False,
                )
            robo.estado_frentes[banca]["wins"] = 1
            robo.estado_frentes[banca]["losses"] = 1
            robo.estado_frentes[banca]["resultado_liquido"] = 0.62

        self.assertEqual(robo.taxa_banca_real(banca), 50.0)
        self.assertEqual(robo.gales_por_assertividade(robo.taxa_banca_real(banca)), 3)

        with robo.estado_lock:
            # A perda reduz o P&L, mas não cria dívida enquanto o saldo líquido segue positivo.
            robo.operacoes_ativas[banca] = {
                "primeira_ordem": "TEST-LOSS-POSITIVE-BANK",
                "resultado_centavos": -35,
                "prejuizo_alocado": 0.0,
            }

        robo.finalizar_operacao_global(banca)
        self.assertEqual(robo.prejuizo_banca_atual(banca), 0.0)
        self.assertEqual(robo.prejuizo_global_atual(), 0.0)
        self.assertEqual(robo.estado_frentes[banca]["recuperacao_pendente"], 0.0)
        self.assertAlmostEqual(robo.estado_frentes[banca]["resultado_liquido"], 0.27)

class FinalizedSignalScoreboardTests(unittest.TestCase):
    def test_loss_is_counted_when_cycle_closes_after_gale_send_failure(self):
        banca = robo.BANCAS[0]
        with robo.estado_lock:
            robo.operacoes_ativas.clear()
            robo.ordens_pendentes.clear()
            robo.ordens_liquidadas.clear()
            robo.operacoes_concluidas.clear()
            robo.historico_operacoes.clear()
            robo.resultado_global_centavos = 0
            robo.stats["wins"] = 0
            robo.stats["losses"] = 0
            for nome in robo.BANCAS:
                robo.estado_frentes[nome].update(
                    wins=0, losses=0, resultado_liquido=0.0,
                    prejuizo_acumulado=0.0, recuperacao_pendente=0.0,
                    envio_indeterminado=False, ultimo_sinal=None,
                )
            robo.operacoes_ativas[banca] = {
                "primeira_ordem": "LOSS-BASE-SEM-GALE",
                "resultado_centavos": -35,
                "prejuizo_alocado": 0.0,
                "ultimo_nivel": 0,
                "numero": 1,
                "ativo": "R_50",
                "entrada": 0.35,
                "gales": 1,
            }

        robo.finalizar_operacao_global(banca)

        self.assertEqual(robo.stats["wins"], 0)
        self.assertEqual(robo.stats["losses"], 1)
        self.assertEqual(robo.estado_frentes[banca]["wins"], 0)
        self.assertEqual(robo.estado_frentes[banca]["losses"], 1)
        self.assertEqual(robo.historico_operacoes[-1]["resultado"], "LOSS")
        self.assertEqual(robo.operacoes_ativas, {})


if __name__ == "__main__":
    unittest.main()
