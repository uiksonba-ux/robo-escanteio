"""Fluxo completo contra corretora em memória; nenhuma conexão externa."""

import types
import time
import unittest
from unittest.mock import MagicMock, patch

from test_gestao import carregar_robo


class SignalFlowTest(unittest.TestCase):
    def executar_ciclo(self, resultado, gales):
        robo = carregar_robo({"EXECUTAR_ORDENS": "true"})
        cliente = MagicMock()
        cliente.check_connect.return_value = True
        cliente.get_balance_mode.return_value = "PRACTICE"
        cliente.get_balance.return_value = 600
        cliente.get_all_ACTIVES_OPCODE.return_value = {"EURUSD-OTC": 76}
        canal = types.SimpleNamespace(
            digital_option_placed_id={}, order_async={},
            timesync=types.SimpleNamespace(server_timestamp=time.time()))
        envios = []

        def enviar(instrumento, ativo_id, valor):
            self.assertEqual(cliente.get_balance_mode(), "PRACTICE")
            ordem = 900 + len(envios)
            requisicao = "req-" + str(ordem)
            envios.append(valor)
            canal.digital_option_placed_id[requisicao] = ordem
            retorno = valor * 1.8 if resultado == "win" else valor if resultado == "draw" else 0
            canal.order_async[ordem] = {"position-changed": {"msg": {
                "status": "closed", "close_reason": "expired",
                "close_profit": retorno, "invest": valor}}}
            return requisicao

        canal.place_digital_option_v2 = MagicMock(side_effect=enviar)
        cliente.api = canal
        robo.api = cliente
        # Histórico define 0 gales ou a faixa inicial de 4, igual à produção.
        if gales == 0:
            robo.stats.update(wins=8, losses=2)
        banca = robo.reservar_banca("EURUSD-OTC", "call", 75, 1, 1)
        with (
            patch.object(robo, "ativo_digital_disponivel", return_value=True),
            patch.object(robo, "obter_payout_digital", return_value=0.8),
            patch.object(robo, "telegram") as telegram,
            patch.object(robo.time, "sleep"),
        ):
            robo.ciclo(banca, "EURUSD-OTC", "call", 75, 1, 1)
        self.assertFalse(robo.ordens_pendentes)
        self.assertFalse(robo.reservas_pendentes)
        self.assertFalse(robo.operacoes_ativas)
        self.assertFalse(robo.estado_frentes[banca]["ocupada"])
        self.assertNotIn("EURUSD-OTC", robo.ativos_em_uso)
        self.assertEqual(robo.stats["ordens_aceitas"], len(envios))
        self.assertTrue(telegram.called)
        cliente.buy.assert_not_called()
        cliente.buy_digital_spot.assert_not_called()
        return robo, envios

    def test_sinal_aceito_win_concilia_lucro_e_libera_banca(self):
        robo, envios = self.executar_ciclo("win", 0)
        self.assertEqual(envios, [6])
        self.assertEqual(robo.resultado_global_centavos, 480)
        self.assertEqual(robo.stats["wins"], 9)

    def test_sinal_aceito_draw_nao_inventa_lucro_ou_prejuizo(self):
        robo, envios = self.executar_ciclo("draw", 0)
        self.assertEqual(envios, [6])
        self.assertEqual(robo.resultado_global_centavos, 0)
        self.assertEqual((robo.stats["wins"], robo.stats["losses"]), (8, 2))

    def test_loss_em_quatro_gales_preserva_x2_e_divida_integral(self):
        robo, envios = self.executar_ciclo("loss", 4)
        self.assertEqual(envios, [6, 12, 24, 48, 96])
        self.assertEqual(robo.resultado_global_centavos, -18600)
        self.assertEqual(robo.prejuizo_global_atual(), 186)
        self.assertEqual(robo.stats["losses"], 1)


if __name__ == "__main__":
    unittest.main()
