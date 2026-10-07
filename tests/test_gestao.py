"""Testes da gestão com integrações e threads isoladas, sem ordens reais."""

import importlib.util
import os
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import MagicMock, patch


ROBO_PATH = Path(__file__).resolve().parents[1] / "robo.py"


def carregar_robo(variaveis=None):
    flask = types.ModuleType("flask")
    app = MagicMock()
    app.route.side_effect = lambda *args, **kwargs: lambda func: func
    flask.Flask = MagicMock(return_value=app)
    flask.jsonify = MagicMock(side_effect=lambda dados: dados)

    requests = types.ModuleType("requests")
    requests.post = MagicMock()

    iqoption = types.ModuleType("iqoptionapi")
    stable_api = types.ModuleType("iqoptionapi.stable_api")
    stable_api.IQ_Option = MagicMock()
    iqoption.stable_api = stable_api

    dependencias = {
        "flask": flask,
        "requests": requests,
        "iqoptionapi": iqoption,
        "iqoptionapi.stable_api": stable_api,
    }
    spec = importlib.util.spec_from_file_location("robo_em_teste", ROBO_PATH)
    robo = importlib.util.module_from_spec(spec)

    with (
        patch.dict(os.environ, variaveis or {}, clear=True),
        patch.dict(sys.modules, dependencias),
        patch.object(threading, "Thread") as thread,
        patch("logging.basicConfig"),
        patch("logging.getLogger", return_value=MagicMock()),
    ):
        spec.loader.exec_module(robo)

    thread.assert_called_once()
    thread.return_value.start.assert_called_once_with()
    requests.post.assert_not_called()
    stable_api.IQ_Option.assert_not_called()
    return robo


class GestaoTest(unittest.TestCase):
    def setUp(self):
        self.robo = carregar_robo()
        self.banca = self.robo.BANCAS[0]

    @property
    def estado(self):
        return self.robo.estado_frentes[self.banca]

    def executar_ciclo(self, resultados, ordens=None, erro_saldo=None):
        """Executa somente o ciclo síncrono; substitui toda integração externa."""
        robo = self.robo
        reservado = robo.reservar_banca("EURUSD", "call", 80, 1, 1)
        self.assertEqual(reservado, self.banca)
        if ordens is None:
            ordens = [(True, i + 1) for i in range(len(resultados))]

        with (
            patch.object(robo, "executar_ordem", side_effect=ordens) as executar,
            patch.object(robo, "aguardar_resultado", side_effect=resultados),
            patch.object(robo, "telegram"),
            patch.object(robo.time, "sleep"),
            patch.object(robo, "texto_saldo", return_value="10000.00",
                         side_effect=erro_saldo),
        ):
            robo.ciclo(self.banca, "EURUSD", "call", 80, 1, 1)

        self.assert_banca_livre()
        return [chamada.args[2] for chamada in executar.call_args_list]

    def assert_banca_livre(self):
        self.assertFalse(self.estado["ocupada"])
        self.assertIsNone(self.estado["ativo"])
        self.assertIsNone(self.estado["order_id"])
        self.assertNotIn("EURUSD", self.robo.ativos_em_uso)

    def test_configuracao_padrao_e_conta_practice(self):
        robo = self.robo
        self.assertEqual(robo.ENTRADA_BASE, 10)
        self.assertEqual(robo.MULTIPLICADOR_GALE, 2)
        self.assertEqual(robo.GALES_POR_CICLO, 2)
        self.assertEqual(robo.RECUPERACAO_PERCENTUAL, 0.01)
        self.assertEqual(robo.CONTA, "PRACTICE")
        self.assertEqual(robo.valores_do_ciclo(10), [10, 20, 40])

    def test_variaveis_do_render_configuram_gestao(self):
        robo = carregar_robo({
            "ENTRADA_BASE": "25",
            "MULTIPLICADOR_GALE": "3",
            "MAX_GALES": "1",
            "RECUPERACAO_PERCENTUAL": "0.02",
            "CONTA": "REAL",
        })
        self.assertEqual(robo.ENTRADA_BASE, 25)
        self.assertEqual(robo.MULTIPLICADOR_GALE, 3)
        self.assertEqual(robo.GALES_POR_CICLO, 1)
        self.assertEqual(robo.RECUPERACAO_PERCENTUAL, 0.02)
        self.assertEqual(robo.CONTA, "PRACTICE")
        self.assertEqual(robo.valores_do_ciclo(25), [25, 75])
        gestao = robo.aplicar_loss_ciclo(robo.BANCAS[0], 100)
        self.assertEqual(gestao["entrada"], 27)

    def test_valores_invalidos_usam_padrao(self):
        for invalido in ("inválido", "nan", "inf"):
            with self.subTest(valor=invalido):
                robo = carregar_robo({
                    "ENTRADA_BASE": invalido,
                    "MULTIPLICADOR_GALE": invalido,
                    "MAX_GALES": invalido,
                    "RECUPERACAO_PERCENTUAL": invalido,
                })
                self.assertEqual(robo.ENTRADA_BASE, 10)
                self.assertEqual(robo.MULTIPLICADOR_GALE, 2)
                self.assertEqual(robo.GALES_POR_CICLO, 2)
                self.assertEqual(robo.RECUPERACAO_PERCENTUAL, 0.01)

    def test_entrada_base_independe_do_saldo(self):
        self.robo = carregar_robo({"ENTRADA_PERCENTUAL": "0.99"})
        for saldo in (None, 0, 100, 10000, 1000000):
            with self.subTest(saldo=saldo):
                self.assertEqual(self.robo.obter_entrada_base(saldo), 10)

    def test_status_expoe_entrada_fixa_sem_percentual_legado(self):
        status = self.robo.home()
        health, codigo = self.robo.health()
        self.assertEqual(codigo, 200)
        for dados in (status["gestao"], health):
            self.assertEqual(dados["entrada_base"], 10)
            self.assertEqual(dados["multiplicador_gale"], 2)
            self.assertEqual(dados["gales_por_ciclo"], 2)
            self.assertEqual(dados["recuperacao_percentual"], 0.01)
            self.assertEqual(dados["ciclos"], "ilimitados")
            self.assertNotIn("entrada_percentual", dados)
            self.assertNotIn("entrada_base_percentual", dados)
        self.assertEqual(status["conta"], "PRACTICE")
        self.assertEqual(health["conta"], "PRACTICE")

    def test_loss_completo_e_novo_ciclo_sobre_prejuizo_acumulado(self):
        valores = self.executar_ciclo([
            ("loss", -10), ("loss", -20), ("loss", -40),
        ])
        self.assertEqual(valores, [10, 20, 40])
        self.assertEqual(self.estado["prejuizo_acumulado"], 70)
        self.assertEqual(self.estado["entrada_atual"], 10.70)
        self.assertEqual(self.estado["ciclo_gestao"], 2)
        self.assertTrue(self.estado["em_recuperacao"])

        valores = self.executar_ciclo([
            ("loss", -10.70), ("loss", -21.40), ("loss", -42.80),
        ])
        self.assertEqual(valores, [10.70, 21.40, 42.80])
        self.assertEqual(self.estado["prejuizo_acumulado"], 144.90)
        self.assertEqual(self.estado["entrada_atual"], 11.45)
        self.assertEqual(self.estado["ciclo_gestao"], 3)
        self.assertEqual(self.robo.stats["losses"], 2)

    def test_win_g2_recupera_apenas_lucro_liquido_do_sinal(self):
        self.robo.aplicar_loss_ciclo(self.banca, 70)
        valores = self.executar_ciclo([
            ("loss", -10.70), ("loss", -21.40), ("win", 34.24),
        ])
        self.assertEqual(valores, [10.70, 21.40, 42.80])
        self.assertEqual(self.estado["prejuizo_acumulado"], 67.86)
        self.assertEqual(self.estado["entrada_atual"], 10.68)
        self.assertEqual(self.estado["ciclo_gestao"], 2)
        self.assertTrue(self.estado["em_recuperacao"])
        self.assertEqual(self.robo.stats["win_g2"], 1)
        self.assertEqual(self.robo.stats["losses"], 0)

    def test_win_parcial_continua_e_recuperacao_total_reseta(self):
        self.robo.aplicar_loss_ciclo(self.banca, 70)
        self.executar_ciclo([("win", 8)])
        self.assertEqual(self.estado["prejuizo_acumulado"], 62)
        self.assertEqual(self.estado["entrada_atual"], 10.62)
        self.assertEqual(self.estado["ciclo_gestao"], 2)
        self.assertTrue(self.estado["em_recuperacao"])

        self.executar_ciclo([("win", 65)])
        self.assertEqual(self.estado["prejuizo_acumulado"], 0)
        self.assertEqual(self.estado["entrada_atual"], 10)
        self.assertEqual(self.estado["ciclo_gestao"], 1)
        self.assertFalse(self.estado["em_recuperacao"])

    def test_recuperacao_exata_reseta_e_win_normal_mantem_base(self):
        self.robo.aplicar_loss_ciclo(self.banca, 70)
        self.executar_ciclo([("win", 70)])
        self.assertEqual(self.estado["prejuizo_acumulado"], 0)
        self.assertEqual(self.estado["entrada_atual"], 10)
        self.assertEqual(self.estado["ciclo_gestao"], 1)
        self.assertFalse(self.estado["em_recuperacao"])
        self.assertEqual(self.executar_ciclo([("win", 8)]), [10])
        self.assertEqual(self.estado["entrada_atual"], 10)

    def test_win_com_resultado_liquido_negativo_aumenta_prejuizo(self):
        self.robo.aplicar_loss_ciclo(self.banca, 70)
        self.executar_ciclo([
            ("loss", -10.70), ("loss", -21.40), ("win", 20),
        ])
        self.assertEqual(self.estado["prejuizo_acumulado"], 82.10)
        self.assertEqual(self.estado["entrada_atual"], 10.82)
        self.assertEqual(self.estado["ciclo_gestao"], 2)
        self.assertTrue(self.estado["em_recuperacao"])
        self.assertEqual(self.robo.stats["wins"], 1)

    def test_nao_ha_limite_artificial_de_ciclos(self):
        self.robo = carregar_robo({"MAX_CICLOS": "1"})
        for indice in range(100):
            entrada = self.estado["entrada_atual"]
            perda = sum(self.robo.valores_do_ciclo(entrada))
            self.robo.aplicar_loss_ciclo(self.banca, perda)
            self.assertEqual(self.estado["ciclo_gestao"], indice + 2)
            self.assertGreater(self.estado["prejuizo_acumulado"], 0)
            self.assertTrue(self.estado["em_recuperacao"])
        self.assertEqual(self.estado["ciclo_gestao"], 101)

    def test_bancas_mantem_gestao_independente(self):
        outra = self.robo.BANCAS[1]
        self.robo.aplicar_loss_ciclo(self.banca, 70)
        self.assertEqual(self.robo.estado_frentes[outra]["entrada_atual"], 10)
        self.assertEqual(self.robo.estado_frentes[outra]["prejuizo_acumulado"], 0)
        self.robo.aplicar_loss_ciclo(outra, 30)
        self.robo.aplicar_win_recuperacao(self.banca, 70)
        self.assertEqual(self.estado["entrada_atual"], 10)
        self.assertEqual(self.robo.estado_frentes[outra]["entrada_atual"], 10.30)
        self.assertEqual(self.robo.estado_frentes[outra]["prejuizo_acumulado"], 30)
        self.assertTrue(self.robo.estado_frentes[outra]["em_recuperacao"])

    def test_interrupcoes_preservam_loss_confirmado_e_liberam_banca(self):
        for prejuizo_inicial in (0, 70):
            for fim in ("draw", "timeout", "recusa", "erro_resultado", "erro_ordem"):
                with self.subTest(prejuizo=prejuizo_inicial, fim=fim):
                    self.robo = carregar_robo()
                    if prejuizo_inicial:
                        self.robo.aplicar_loss_ciclo(self.banca, prejuizo_inicial)
                    entrada = self.estado["entrada_atual"]
                    ciclo = self.estado["ciclo_gestao"]
                    resultados = [("loss", -entrada)]
                    ordens = [(True, 1), (True, 2)]
                    if fim in ("draw", "timeout"):
                        resultados.append((fim, 0))
                    elif fim == "recusa":
                        ordens[1] = (False, "ordem_recusada")
                    elif fim == "erro_resultado":
                        resultados.append(RuntimeError("falha simulada"))
                    else:
                        ordens[1] = RuntimeError("falha simulada")

                    if fim.startswith("erro_"):
                        with self.assertRaisesRegex(RuntimeError, "falha simulada"):
                            self.executar_ciclo(resultados, ordens)
                    else:
                        self.executar_ciclo(resultados, ordens)

                    esperado = round(prejuizo_inicial + entrada, 2)
                    self.assertEqual(self.estado["prejuizo_acumulado"], esperado)
                    self.assertEqual(self.estado["entrada_atual"],
                                     round(10 + round(esperado * 0.01, 2), 2))
                    self.assertEqual(self.estado["ciclo_gestao"], ciclo)
                    self.assertTrue(self.estado["em_recuperacao"])
                    self.assertEqual(self.robo.stats["losses"], 0)
                    self.assertEqual(self.robo.stats["wins"], 0)
                    self.assert_banca_livre()

    def test_interrupcao_sem_loss_nao_inicia_recuperacao(self):
        for resultado in ("draw", "timeout"):
            with self.subTest(resultado=resultado):
                self.executar_ciclo([(resultado, 0)])
                self.assertEqual(self.estado["prejuizo_acumulado"], 0)
                self.assertEqual(self.estado["entrada_atual"], 10)
                self.assertFalse(self.estado["em_recuperacao"])

    def test_erro_apos_loss_contabilizado_nao_duplica_prejuizo(self):
        with self.assertRaisesRegex(RuntimeError, "saldo indisponível"):
            self.executar_ciclo([
                ("loss", -10), ("loss", -20), ("loss", -40),
            ], erro_saldo=RuntimeError("saldo indisponível"))
        self.assertEqual(self.estado["prejuizo_acumulado"], 70)
        self.assertEqual(self.estado["entrada_atual"], 10.70)
        self.assertEqual(self.estado["ciclo_gestao"], 2)
        self.assertEqual(self.robo.stats["losses"], 1)
        self.assert_banca_livre()

    def test_erro_apos_win_contabilizado_nao_reconta_perdas(self):
        self.robo.aplicar_loss_ciclo(self.banca, 70)
        with self.assertRaisesRegex(RuntimeError, "saldo indisponível"):
            self.executar_ciclo([
                ("loss", -10.70), ("loss", -21.40), ("win", 34.24),
            ], erro_saldo=RuntimeError("saldo indisponível"))
        self.assertEqual(self.estado["prejuizo_acumulado"], 67.86)
        self.assertEqual(self.estado["entrada_atual"], 10.68)
        self.assertEqual(self.robo.stats["wins"], 1)
        self.assert_banca_livre()


if __name__ == "__main__":
    unittest.main()
