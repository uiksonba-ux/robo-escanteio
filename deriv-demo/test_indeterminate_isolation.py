import threading
import unittest
from unittest import mock

# robo.py starts its worker when imported. Keep this test isolated from all
# network activity and trading threads.
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

    def test_other_available_bank_can_take_distinct_signal(self):
        reserved = []
        for index in range(len(robo.BANCAS) - 1):
            banca = robo.reservar_banca(
                f"TEST{index}", "call", 80, 1, 1
            )
            self.assertIsNotNone(banca)
            reserved.append(banca)

        self.assertEqual(reserved, robo.BANCAS[1:])
        self.assertEqual(len(set(reserved)), len(robo.BANCAS) - 1)
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
            robo.ordens_pendentes[12345] = {"banca": robo.BANCAS[1], "valor": 2.0, "nivel": 0}

        ok, motivo, _ = robo.confirmar_ordem_gerida(
            banca, "WLDAUD", 0, 2.0, 12345, contexto
        )

        self.assertIsNone(ok)
        self.assertEqual(motivo, "order_id_invalido_ou_repetido")
        self.assertEqual(robo.envios_indeterminados[banca]["contexto"], contexto)

    def test_100_percent_recovery_adds_previous_loss_to_base(self):
        with mock.patch.object(robo, "obter_entrada_base", return_value=1.0):
            alvo = robo.calcular_alvo_recuperacao(1.0)
            self.assertEqual(alvo, 1.0)
            self.assertEqual(robo.calcular_entrada_recuperacao(alvo), 2.0)

    def test_requested_base_gale_and_recovery_settings(self):
        self.assertEqual(robo.QTD_BANCAS, 2)
        self.assertEqual(len(robo.BANCAS), 2)
        self.assertEqual(robo.ENTRADA_BASE_USD, 0.35)
        self.assertEqual(robo.MULTIPLICADOR_GALE, 3.0)
        self.assertEqual(robo.RECUPERACAO_PERCENTUAL, 1.0)
        self.assertEqual(
            robo.valores_do_ciclo(0.35, quantidade_gales=4),
            [0.35, 1.05, 3.15, 9.45, 28.35],
        )

    def test_successful_recovery_clears_only_that_bank_pending_loss(self):
        banca_a, banca_b = robo.BANCAS
        with robo.estado_lock:
            robo.operacoes_ativas.clear()
            robo.ordens_pendentes.clear()
            robo.operacoes_concluidas.clear()
            robo.recuperacoes_em_curso.clear()
            for banca in robo.BANCAS:
                robo.estado_frentes[banca].update(
                    recuperacao_pendente=0.0,
                    envio_indeterminado=False,
                )
            robo.estado_frentes[banca_a]["recuperacao_pendente"] = 0.35
            robo.operacoes_ativas[banca_a] = {
                "primeira_ordem": "TEST-RECOVERY-WIN",
                "resultado_centavos": 12,
                "prejuizo_alocado": 0.35,
            }
        robo.finalizar_operacao_global(banca_a)
        self.assertEqual(robo.prejuizo_banca_atual(banca_a), 0.0)
        self.assertEqual(robo.prejuizo_banca_atual(banca_b), 0.0)

    def test_deferred_recovery_keeps_previous_loss_and_adds_new_loss(self):
        banca_a, banca_b = robo.BANCAS
        with robo.estado_lock:
            robo.operacoes_ativas.clear()
            robo.ordens_pendentes.clear()
            robo.operacoes_concluidas.clear()
            robo.recuperacoes_em_curso.clear()
            for banca in robo.BANCAS:
                robo.estado_frentes[banca].update(
                    recuperacao_pendente=0.0,
                    envio_indeterminado=False,
                )
            robo.estado_frentes[banca_a]["recuperacao_pendente"] = 0.35
            robo.operacoes_ativas[banca_a] = {
                "primeira_ordem": "TEST-DEFERRED-LOSS",
                "resultado_centavos": -35,
                "prejuizo_alocado": 0.0,
                "recuperacao_diferida": True,
            }
        robo.finalizar_operacao_global(banca_a)
        self.assertEqual(robo.prejuizo_banca_atual(banca_a), 0.70)
        self.assertEqual(robo.prejuizo_banca_atual(banca_b), 0.0)

    def test_assertiveness_and_recovery_ledgers_are_independent_per_bank(self):
        banca_a, banca_b = robo.BANCAS
        with robo.estado_lock:
            robo.operacoes_ativas.clear()
            robo.envios_indeterminados.clear()
            robo.reservas_pendentes.clear()
            robo.ordens_pendentes.clear()
            robo.ordens_liquidadas.clear()
            robo.operacoes_concluidas.clear()
            robo.recuperacoes_em_curso.clear()
            robo.resultado_global_centavos = 0
            for banca in robo.BANCAS:
                dados = robo.estado_frentes[banca]
                dados.update(
                    wins=0,
                    losses=0,
                    wins_base_janela=0,
                    losses_base_janela=0,
                    entradas_janela=0,
                    resultado_liquido=0.0,
                    prejuizo_acumulado=0.0,
                    recuperacao_pendente=0.0,
                    envio_indeterminado=False,
                    em_recuperacao=False,
                )
            robo.estado_frentes[banca_a]["wins"] = 1
            robo.estado_frentes[banca_a]["losses"] = 1
            robo.estado_frentes[banca_b]["wins"] = 4
            robo.estado_frentes[banca_b]["resultado_liquido"] = 5.0

        self.assertEqual(robo.taxa_banca_ponderada(banca_a), 50.0)
        self.assertEqual(robo.taxa_banca_ponderada(banca_b), 100.0)
        self.assertEqual(
            robo.gales_por_assertividade(robo.taxa_banca_ponderada(banca_a)),
            3,
        )
        self.assertEqual(
            robo.gales_por_assertividade(robo.taxa_banca_ponderada(banca_b)),
            0,
        )

        with robo.estado_lock:
            # A banca vinha lucrativa, mas uma perda nova gera recuperação própria.
            robo.estado_frentes[banca_a]["resultado_liquido"] = 0.62
            robo.operacoes_ativas[banca_a] = {
                "primeira_ordem": "TEST-LOSS-A",
                "resultado_centavos": -35,
                "prejuizo_alocado": 0.0,
            }

        robo.finalizar_operacao_global(banca_a)
        self.assertEqual(robo.prejuizo_banca_atual(banca_a), 0.35)
        self.assertEqual(robo.prejuizo_banca_atual(banca_b), 0.0)
        self.assertEqual(robo.prejuizo_global_atual(), 0.35)
        self.assertAlmostEqual(robo.estado_frentes[banca_a]["resultado_liquido"], 0.27)

        with mock.patch.object(robo, "obter_entrada_base", return_value=0.35):
            entrada_a = robo.calcular_entrada_recuperacao(
                robo.calcular_alvo_recuperacao(robo.prejuizo_banca_atual(banca_a))
            )
            entrada_b = robo.calcular_entrada_recuperacao(
                robo.calcular_alvo_recuperacao(robo.prejuizo_banca_atual(banca_b))
            )
        self.assertEqual(entrada_a, 0.70)
        self.assertEqual(entrada_b, 0.35)

    def test_successful_recovery_clears_only_that_bank_pending_loss(self):
        banca_a, banca_b = robo.BANCAS
        with robo.estado_lock:
            robo.operacoes_ativas.clear()
            robo.ordens_pendentes.clear()
            robo.operacoes_concluidas.clear()
            robo.recuperacoes_em_curso.clear()
            for banca in robo.BANCAS:
                robo.estado_frentes[banca].update(
                    recuperacao_pendente=0.0,
                    envio_indeterminado=False,
                )
            robo.estado_frentes[banca_a]["recuperacao_pendente"] = 0.35
            robo.operacoes_ativas[banca_a] = {
                "primeira_ordem": "TEST-RECOVERY-WIN",
                "resultado_centavos": 12,
                "prejuizo_alocado": 0.35,
            }
        robo.finalizar_operacao_global(banca_a)
        self.assertEqual(robo.prejuizo_banca_atual(banca_a), 0.0)
        self.assertEqual(robo.prejuizo_banca_atual(banca_b), 0.0)


if __name__ == "__main__":
    unittest.main()
