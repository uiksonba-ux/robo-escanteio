"""Regressões: dívida/recuperação pendente não monopoliza sinais normais.

Toda conexão e envio são simulados. O worker/Telegram permanecem isolados pelo
loader compartilhado; nenhum teste conhece credenciais ou acessa a corretora.
"""

import itertools
import unittest
from unittest.mock import MagicMock, patch

from test_gestao import carregar_robo


class RecoverySchedulingTest(unittest.TestCase):
    def setUp(self):
        self.robo = carregar_robo()
        self.robo.api = MagicMock()
        self.robo.api.get_balance_mode.return_value = "PRACTICE"
        self.robo.api.get_balance.return_value = 600
        self.ids = itertools.count(501)
        self.envios = []
        for remendo in (
            patch.object(self.robo, "garantir_practice", return_value=True),
            patch.object(self.robo, "obter_payout_digital", return_value=0.8),
            patch.object(self.robo, "executar_ordem", side_effect=self.comprar),
            patch.object(self.robo, "telegram"),
        ):
            remendo.start()
            self.addCleanup(remendo.stop)

    def comprar(self, banca, ativo, valor, direcao, expiracao):
        self.envios.append((banca, ativo, valor, direcao, expiracao))
        return True, next(self.ids)

    def enviar(self, indice=0, nivel=0):
        return self.robo.enviar_ordem_gerida(
            self.robo.BANCAS[indice], f"TEST-{indice}-OTC", "call", 1, nivel)

    def test_divida_maior_que_saldo_permite_normal_e_preserva_100_porcento(self):
        self.robo.resultado_global_centavos = -100000
        ok, ordem, contexto = self.enviar()
        self.assertTrue(ok)
        self.assertEqual(contexto["base"], 6)
        self.assertEqual(contexto["entrada"], 6)
        self.assertEqual(contexto["prejuizo_alocado"], 0)
        self.assertEqual(self.robo.resultado_global_centavos, -100000)
        self.assertEqual(self.robo.RECUPERACAO_PERCENTUAL, 1)
        self.assertIsNone(self.robo.recuperacao_em_curso)
        self.assertEqual(self.robo.ordens_pendentes[ordem]["valor"], 6)
        self.assertTrue(any("RECUPERACAO ADIADA" in c.args[0]
                            for c in self.robo.log.warning.call_args_list))
        # Só o lucro confirmado reduz a dívida; nenhuma recuperação é inventada.
        self.robo.registrar_resultado_ordem(ordem, "win", 4.8)
        self.robo.finalizar_operacao_global(self.robo.BANCAS[0])
        self.assertEqual(self.robo.resultado_global_centavos, -99520)
        ok, _, contexto = self.enviar(1)
        self.assertTrue(ok)
        self.assertEqual(contexto["entrada"], 6)
        self.assertEqual(contexto["prejuizo_alocado"], 0)

    def test_recuperacao_com_resultado_pendente_permite_normal_de_outra_banca(self):
        self.robo.resultado_global_centavos = -1000
        ok, recuperacao, contexto = self.enviar()
        self.assertTrue(ok)
        self.assertEqual(contexto["entrada"], 18.5)
        self.assertEqual(contexto["prejuizo_alocado"], 10)
        ok, normal, contexto = self.enviar(1)
        self.assertTrue(ok)
        self.assertEqual(contexto["entrada"], 6)
        self.assertEqual(contexto["prejuizo_alocado"], 0)
        self.assertEqual(self.robo.recuperacao_em_curso, self.robo.BANCAS[0])
        self.assertIn(recuperacao, self.robo.ordens_pendentes)
        self.assertIn(normal, self.robo.ordens_pendentes)
        self.assertEqual(self.robo.resultado_global_centavos, -1000)
        self.assertIsNone(self.robo.finalizar_operacao_global(self.robo.BANCAS[0]))
        self.assertIn(recuperacao, self.robo.ordens_pendentes)

    def test_recuperacao_adiada_respeita_reservas_e_limite_do_saldo(self):
        self.robo.api.get_balance.return_value = 10
        self.robo.resultado_global_centavos = -10000
        for indice in range(5):
            ok, _, contexto = self.enviar(indice)
            self.assertTrue(ok)
            self.assertEqual(contexto["entrada"], 2)
        ok, motivo, _ = self.enviar(5)
        self.assertFalse(ok)
        self.assertEqual(motivo, "saldo_insuficiente")
        self.assertEqual(sum(o["valor"] for o in self.robo.ordens_pendentes.values()), 10)
        self.assertEqual(len(self.envios), 5)
        self.assertEqual(self.robo.resultado_global_centavos, -10000)

    def test_sem_saldo_para_base_nao_envia_nem_apaga_divida(self):
        self.robo.api.get_balance.return_value = 1.99
        self.robo.resultado_global_centavos = -10000
        ok, motivo, contexto = self.enviar()
        self.assertFalse(ok)
        self.assertEqual(motivo, "saldo_insuficiente")
        self.assertIsNone(contexto)
        self.assertFalse(self.envios)
        self.assertFalse(self.robo.reservas_pendentes)
        self.assertIsNone(self.robo.recuperacao_em_curso)
        self.assertEqual(self.robo.resultado_global_centavos, -10000)

    def test_gale_normal_adiado_mantem_x2_e_nao_muda_divida_sem_liquidacao(self):
        self.robo.resultado_global_centavos = -100000
        ok, inicial, contexto = self.enviar()
        self.assertTrue(ok)
        self.assertEqual(contexto["gales"], 4)
        self.robo.registrar_resultado_ordem(inicial, "loss", -6)
        ok, _, contexto = self.enviar(nivel=1)
        self.assertTrue(ok)
        self.assertEqual(self.envios[-1][2], 12)
        self.assertEqual(contexto["entrada"], 6)
        self.assertEqual(self.robo.resultado_global_centavos, -100000)

    def test_envio_indeterminado_mantem_bloqueio_explicito_sem_reenvio(self):
        self.robo.executar_ordem.side_effect = None
        self.robo.executar_ordem.return_value = (None, "timeout_envio")
        self.robo.resultado_global_centavos = -1000
        ok, _, _ = self.enviar()
        self.assertIsNone(ok)
        ok, motivo, _ = self.enviar(1)
        self.assertFalse(ok)
        self.assertEqual(motivo, "gestao_pausada_envio_indeterminado")
        self.robo.executar_ordem.assert_called_once()
        self.assertEqual(self.robo.resultado_global_centavos, -1000)
        self.assertEqual(self.robo.reservas_pendentes[self.robo.BANCAS[0]], 18.5)

    def test_falha_em_iniciar_thread_libera_banca_e_ativo(self):
        with patch.object(self.robo.threading, "Thread") as thread:
            thread.return_value.start.side_effect = RuntimeError("thread indisponível")
            self.assertFalse(self.robo.disparar_sinal("TEST-OTC", "call", 80, 1, 1))
        self.assertFalse(self.robo.estado_frentes[self.robo.BANCAS[0]]["ocupada"])
        self.assertNotIn("TEST-OTC", self.robo.ativos_em_uso)
        self.assertFalse(self.envios)

    def test_aceitacao_tardia_concilia_mesmo_envio_uma_vez_e_segue_resultado(self):
        banca = self.robo.BANCAS[0]
        self.robo.resultado_global_centavos = -1000
        self.robo.executar_ordem.side_effect = None
        self.robo.executar_ordem.return_value = (None, "timeout_envio")
        self.robo.reservar_banca("TEST-0-OTC", "call", 80, 1, 1)
        with (
            patch.object(self.robo, "consultar_confirmacao_envio", create=True,
                         side_effect=[(None, "pendente"), (True, 501)]) as confirmar,
            patch.object(self.robo, "aguardar_resultado", return_value=("win", 14.8)) as resultado,
            patch.object(self.robo, "texto_saldo", return_value="600.00"),
            patch.object(self.robo.time, "sleep") as sleep,
        ):
            self.robo.ciclo(banca, "TEST-0-OTC", "call", 80, 1, 1)
        self.robo.executar_ordem.assert_called_once()
        self.assertEqual(confirmar.call_count, 2)
        sleep.assert_called_once_with(5)
        resultado.assert_called_once_with(501)
        self.assertEqual(self.robo.entradas_globais_aceitas, 1)
        self.assertEqual(self.robo.resultado_global_centavos, 480)
        self.assertIn(501, self.robo.ordens_liquidadas)
        self.assertIsNone(self.robo.envio_indeterminado)
        self.assertIsNone(self.robo.recuperacao_em_curso)
        self.assertFalse(self.robo.estado_frentes[banca]["ocupada"])

    def test_recusa_tardia_libera_reserva_sem_consumir_posicao_ou_apagar_divida(self):
        banca = self.robo.BANCAS[0]
        self.robo.resultado_global_centavos = -1000
        self.robo.executar_ordem.side_effect = None
        self.robo.executar_ordem.return_value = (None, "timeout_envio")
        self.robo.reservar_banca("TEST-0-OTC", "call", 80, 1, 1)
        with (
            patch.object(self.robo, "consultar_confirmacao_envio", create=True,
                         return_value=(False, "recusa_comprovada")),
            patch.object(self.robo, "aguardar_resultado") as resultado,
        ):
            self.robo.ciclo(banca, "TEST-0-OTC", "call", 80, 1, 1)
        self.robo.executar_ordem.assert_called_once()
        resultado.assert_not_called()
        self.assertEqual(self.robo.entradas_globais_aceitas, 0)
        self.assertEqual(self.robo.resultado_global_centavos, -1000)
        self.assertIsNone(self.robo.envio_indeterminado)
        self.assertIsNone(self.robo.recuperacao_em_curso)
        self.assertFalse(self.robo.reservas_pendentes)
        self.assertFalse(self.robo.estado_frentes[banca]["ocupada"])

    def test_timeout_repetido_preserva_reserva_e_aceitacao_conta_so_uma_vez(self):
        self.robo.resultado_global_centavos = -1000
        self.robo.executar_ordem.side_effect = None
        self.robo.executar_ordem.return_value = (None, "timeout_envio")
        ok, _, _ = self.enviar()
        self.assertIsNone(ok)
        banca = self.robo.BANCAS[0]
        with patch.object(self.robo, "consultar_confirmacao_envio", create=True,
                          side_effect=[(None, "pendente"), (None, "pendente"), (True, 501)]) as confirmar:
            for _ in range(2):
                ok, _, _ = self.robo.reconciliar_envio_gerido(banca)
                self.assertIsNone(ok)
                self.assertEqual(self.robo.reservas_pendentes[banca], 18.5)
                self.assertEqual(self.robo.resultado_global_centavos, -1000)
                self.assertEqual(self.robo.entradas_globais_aceitas, 0)
            ok, ordem, _ = self.robo.reconciliar_envio_gerido(banca)
            self.assertTrue(ok)
            self.assertEqual(ordem, 501)
            self.assertFalse(self.robo.reconciliar_envio_gerido(banca)[0])
        self.assertEqual(confirmar.call_count, 3)
        self.assertEqual(self.robo.entradas_globais_aceitas, 1)
        self.assertEqual(len(self.robo.ordens_pendentes), 1)
        self.robo.executar_ordem.assert_called_once()

    def test_aceitacao_tardia_gale_preserva_contexto_para_proximo_nivel(self):
        ok, inicial, _ = self.enviar()
        self.assertTrue(ok)
        self.robo.registrar_resultado_ordem(inicial, "loss", -6)
        self.robo.executar_ordem.side_effect = None
        self.robo.executar_ordem.return_value = (None, "timeout_envio")
        ok, _, _ = self.enviar(nivel=1)
        self.assertIsNone(ok)
        with patch.object(self.robo, "consultar_confirmacao_envio", create=True,
                          return_value=(True, 502)):
            ok, gale, _ = self.robo.reconciliar_envio_gerido(self.robo.BANCAS[0])
        self.assertTrue(ok)
        self.robo.registrar_resultado_ordem(gale, "loss", -12)
        self.robo.executar_ordem.side_effect = self.comprar
        # O stub sequencial já usou501; avançar o próximo ID após a confirmação502.
        self.ids = itertools.count(503)
        ok, _, contexto = self.enviar(nivel=2)
        self.assertTrue(ok)
        self.assertEqual(contexto["ultimo_nivel"], 2)
        self.assertEqual(self.envios[-1][2], 24)

    def test_payout_real_menor_dimensiona_recuperacao_integral(self):
        self.robo.resultado_global_centavos = -1000
        self.robo.obter_payout_digital.return_value = 0.5
        ok, ordem, contexto = self.enviar()
        self.assertTrue(ok)
        self.assertEqual(contexto["entrada"], 26)
        self.assertEqual(contexto["prejuizo_alocado"], 10)
        self.assertEqual(contexto["payout_real"], 0.5)
        self.assertEqual(contexto["payout_recuperacao"], 0.5)
        self.robo.obter_payout_digital.assert_called_once_with("TEST-0-OTC", 1)
        # Ao payout confirmado, o lucro paga a dívida inteira e o lucro da base.
        self.robo.registrar_resultado_ordem(ordem, "win", 13)
        self.robo.finalizar_operacao_global(self.robo.BANCAS[0])
        self.assertEqual(self.robo.resultado_global_centavos, 300)

    def test_payout_real_maior_nao_sobrescreve_configuracao_conservadora(self):
        self.robo.resultado_global_centavos = -1000
        self.robo.obter_payout_digital.return_value = 0.9
        ok, _, contexto = self.enviar()
        self.assertTrue(ok)
        self.assertEqual(contexto["entrada"], 18.5)
        self.assertEqual(contexto["payout_real"], 0.9)
        self.assertEqual(contexto["payout_recuperacao"], 0.8)

    def test_payout_menor_sem_saldo_para_recuperar_permite_normal(self):
        self.robo.api.get_balance.return_value = 20
        self.robo.resultado_global_centavos = -1000
        self.robo.obter_payout_digital.return_value = 0.5
        ok, _, contexto = self.enviar()
        self.assertTrue(ok)
        self.assertEqual(contexto["entrada"], 2)
        self.assertEqual(contexto["prejuizo_alocado"], 0)
        self.assertEqual(contexto["payout_real"], 0.5)
        self.assertIsNone(contexto["payout_recuperacao"])
        self.assertIsNone(self.robo.recuperacao_em_curso)
        self.assertEqual(self.robo.resultado_global_centavos, -1000)

    def test_payout_nao_confirmado_nao_aloca_divida_a_sinal_normal(self):
        for indice, payout in enumerate((None, 0, float("nan"), float("inf"), -0.5, True)):
            with self.subTest(payout=payout):
                self.robo.resultado_global_centavos = -1000
                self.robo.obter_payout_digital.return_value = payout
                ok, ordem, contexto = self.enviar(indice)
                self.assertTrue(ok)
                self.assertEqual(contexto["entrada"], 6)
                self.assertEqual(contexto["prejuizo_alocado"], 0)
                self.assertIsNone(contexto["payout_recuperacao"])
                self.assertIsNone(self.robo.recuperacao_em_curso)
                self.assertEqual(self.robo.resultado_global_centavos, -1000)
                self.robo.registrar_resultado_ordem(ordem, "draw", 0)
                self.robo.finalizar_operacao_global(self.robo.BANCAS[indice])
        self.assertTrue(any("motivo=payout_nao_confirmado" in c.args[0]
                            for c in self.robo.log.warning.call_args_list))

    def test_calculo_publico_aceita_payout_opcional_e_rejeita_invalido(self):
        self.assertEqual(self.robo.calcular_entrada_recuperacao(10, 600), 18.5)
        self.assertEqual(self.robo.calcular_entrada_recuperacao(10, 600, 0.5), 26)
        for payout in (0, -0.5, float("nan"), float("inf")):
            with self.subTest(payout=payout), self.assertRaises(ValueError):
                self.robo.calcular_entrada_recuperacao(10, 600, payout)


if __name__ == "__main__":
    unittest.main()
