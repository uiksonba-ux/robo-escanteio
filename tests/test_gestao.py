"""Validação da gestão do PR #2; APIs, Telegram e worker ficam isolados."""

import importlib.util
import os
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import MagicMock, call, patch


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
        self.mensagens = []

    @property
    def estado(self):
        return self.robo.estado_frentes[self.banca]

    def preparar_recuperacao(self, prejuizo=70, gales=2):
        self.robo.aplicar_loss_ciclo(self.banca, prejuizo)
        self.estado["gales_reativos"] = gales

    def executar_ciclo(self, resultados, ordens=None, erro_saldo=None,
                       saldo=1000000):
        """Executa o ciclo real com ordens e resultados exclusivamente simulados."""
        robo = self.robo
        self.assertEqual(robo.reservar_banca("EURUSD", "call", 80, 1, 1),
                         self.banca)
        if ordens is None:
            ordens = [(True, i + 1) for i in range(len(resultados))]
        api = MagicMock()
        api.get_balance.return_value = saldo
        with (
            patch.object(robo, "api", api),
            patch.object(robo, "garantir_practice", return_value=True),
            patch.object(robo, "executar_ordem", side_effect=ordens) as executar,
            patch.object(robo, "aguardar_resultado", side_effect=resultados),
            patch.object(robo, "telegram", side_effect=self.mensagens.append),
            patch.object(robo.time, "sleep"),
            patch.object(robo, "texto_saldo", return_value="1000000.00",
                         side_effect=erro_saldo),
        ):
            try:
                robo.ciclo(self.banca, "EURUSD", "call", 80, 1, 1)
            finally:
                self.assert_banca_livre()
        return [chamada.args[2] for chamada in executar.call_args_list]

    def assert_banca_livre(self):
        self.assertFalse(self.estado["ocupada"])
        self.assertIsNone(self.estado["ativo"])
        self.assertIsNone(self.estado["order_id"])
        self.assertNotIn("EURUSD", self.robo.ativos_em_uso)
        self.assertNotIn(self.banca, self.robo.reservas_pendentes)

    def test_configuracao_fixa_e_dez_bancas(self):
        robo = self.robo
        self.assertEqual(robo.ENTRADA_BASE, 2)
        self.assertEqual(robo.ENTRADA_MINIMA, 2)
        self.assertEqual(robo.MULTIPLICADOR_GALE, 2)
        self.assertEqual(robo.GALES_POR_CICLO, 0)
        self.assertEqual(robo.RECUPERACAO_PERCENTUAL, 0.50)
        self.assertEqual(robo.PAYOUT_RECUPERACAO, 0.80)
        self.assertEqual(robo.CONTA, "PRACTICE")
        self.assertEqual(robo.QTD_BANCAS, 10)
        self.assertEqual(len(set(robo.BANCAS)), 10)
        self.assertEqual(robo.valores_do_ciclo(2), [2])
        self.assertEqual(robo.valores_do_ciclo(2, 3), [2, 4, 8, 16])
        for dados in robo.estado_frentes.values():
            self.assertEqual(dados["gales_reativos"], 0)
            self.assertEqual(dados["entrada_atual"], 2)
            self.assertEqual(dados["prejuizo_acumulado"], 0)

    def test_ambiente_nao_sobrescreve_gestao_nem_conta(self):
        robo = carregar_robo({
            "ENTRADA_BASE": "25", "MULTIPLICADOR_GALE": "3",
            "MAX_GALES": "8", "RECUPERACAO_PERCENTUAL": "0.02",
            "CONTA": "REAL", "ENTRADA_PERCENTUAL": "0.99",
        })
        self.assertEqual(robo.ENTRADA_BASE, 2)
        self.assertEqual(robo.MULTIPLICADOR_GALE, 2)
        self.assertEqual(robo.GALES_POR_CICLO, 0)
        self.assertEqual(robo.RECUPERACAO_PERCENTUAL, 0.50)
        self.assertEqual(robo.CONTA, "PRACTICE")
        for saldo in (None, 0, 100, 10000, 1000000):
            self.assertEqual(robo.obter_entrada_base(saldo), 2)

    def test_payout_configuravel_e_valores_invalidos(self):
        robo = carregar_robo({"PAYOUT_RECUPERACAO": "0.5"})
        self.assertEqual(robo.calcular_entrada_recuperacao(100), 102)
        for invalido in ("inválido", "nan", "inf"):
            with self.subTest(valor=invalido):
                robo = carregar_robo({"PAYOUT_RECUPERACAO": invalido})
                self.assertEqual(robo.PAYOUT_RECUPERACAO, 0.8)
                self.assertEqual(robo.calcular_entrada_recuperacao(100), 64.50)

    def test_entrada_recupera_metade_do_prejuizo_com_payout_estimado(self):
        robo = self.robo
        for prejuizo, esperado in ((-10, 2), (0, 2), (2, 3.25),
                                   (11.75, 9.34), (70, 45.75), (100, 64.5)):
            with self.subTest(prejuizo=prejuizo):
                self.assertEqual(robo.calcular_entrada_recuperacao(prejuizo), esperado)
        self.assertAlmostEqual((45.75 - 2) * 0.8, 70 * 0.5)

    def test_status_expoe_gestao_e_gales_por_banca(self):
        self.robo.aplicar_loss_ciclo(self.banca, 2)
        status = self.robo.home()
        health, codigo = self.robo.health()
        self.assertEqual(codigo, 200)
        for dados in (status["gestao"], health):
            self.assertEqual(dados["entrada_base"], 2)
            self.assertEqual(dados["multiplicador_gale"], 2)
            self.assertEqual(dados["gales_iniciais"], 0)
            self.assertEqual(dados["recuperacao_percentual"], 0.5)
            self.assertEqual(dados["ciclos"], "ilimitados")
            self.assertNotIn("entrada_percentual", dados)
        self.assertEqual(status["conta"], "PRACTICE")
        self.assertEqual(health["conta"], "PRACTICE")
        self.assertEqual(health["bancas"], 10)
        self.assertEqual(status["gestao"]["gales_por_banca"][self.banca], 1)
        self.assertEqual(status["bancas"][self.banca]["prejuizo_acumulado"], 2)
        self.assertNotIn("início 10", status["gestao"]["regra_recuperacao"])
        self.assertRegex(health["regra_gales"], r"WIN -1.*LOSS(?: completo)? \+1")

    def test_loss_completo_aumenta_um_gale_em_cada_ciclo(self):
        self.assertEqual(self.executar_ciclo([("loss", -2)]), [2])
        self.assertEqual(self.estado["prejuizo_acumulado"], 2)
        self.assertEqual(self.estado["entrada_atual"], 3.25)
        self.assertEqual(self.estado["gales_reativos"], 1)
        self.assertEqual(self.estado["ciclo_gestao"], 2)
        self.assertEqual(self.executar_ciclo([("loss", -3.25), ("loss", -6.50)]),
                         [3.25, 6.50])
        self.assertEqual(self.estado["prejuizo_acumulado"], 11.75)
        self.assertEqual(self.estado["entrada_atual"], 9.34)
        self.assertEqual(self.estado["gales_reativos"], 2)
        self.assertEqual(self.estado["ciclo_gestao"], 3)
        self.assertTrue(self.estado["em_recuperacao"])
        self.assertEqual(self.robo.stats["losses"], 2)
        self.assertEqual(self.robo.perdas_progressao, 2)
        self.assertIn("Gales na próxima: 2", self.mensagens[-1])

    def test_win_direto_decrementa_ate_zero(self):
        self.preparar_recuperacao(prejuizo=20, gales=2)
        for lucro, gales, restante, entrada in ((5, 1, 15, 11.38),
                                                (5, 0, 10, 8.25),
                                                (5, 0, 5, 5.12)):
            with self.subTest(gales=gales, restante=restante):
                self.executar_ciclo([("win", lucro)])
                self.assertEqual(self.estado["gales_reativos"], gales)
                self.assertEqual(self.estado["prejuizo_acumulado"], restante)
                self.assertEqual(self.estado["entrada_atual"], entrada)
        self.assertEqual(self.robo.vitorias_progressao, 3)
        self.assertEqual(self.robo.stats["win_direto"], 3)

    def test_win_g2_usa_lucro_liquido_e_reduz_so_um_gale(self):
        self.preparar_recuperacao()
        valores = self.executar_ciclo([
            ("loss", -45.75), ("loss", -91.50), ("win", 146.40),
        ])
        self.assertEqual(valores, [45.75, 91.50, 183])
        self.assertEqual(self.estado["prejuizo_acumulado"], 60.85)
        self.assertEqual(self.estado["entrada_atual"], 40.03)
        self.assertEqual(self.estado["gales_reativos"], 1)
        self.assertEqual(self.estado["ciclo_gestao"], 2)
        self.assertTrue(self.estado["em_recuperacao"])
        self.assertEqual(self.robo.stats["win_g2"], 1)
        self.assertEqual(self.robo.stats["losses"], 0)
        self.assertIn("Resultado líquido do sinal: 9.15", self.mensagens[-1])
        self.assertIn("Gales na próxima: 1", self.mensagens[-1])

    def test_win_com_liquido_negativo_aumenta_divida_mas_decrementa_gale(self):
        self.preparar_recuperacao()
        self.executar_ciclo([
            ("loss", -45.75), ("loss", -91.50), ("win", 100),
        ])
        self.assertEqual(self.estado["prejuizo_acumulado"], 107.25)
        self.assertEqual(self.estado["entrada_atual"], 69.03)
        self.assertEqual(self.estado["gales_reativos"], 1)
        self.assertEqual(self.robo.stats["wins"], 1)
        self.assertEqual(self.robo.vitorias_progressao, 1)

    def test_recuperacao_total_reseta_base_sem_apagar_regra_de_gales(self):
        for lucro in (70, 75):
            with self.subTest(lucro=lucro):
                self.robo = carregar_robo()
                self.preparar_recuperacao(gales=3)
                self.executar_ciclo([("win", lucro)])
                self.assertEqual(self.estado["prejuizo_acumulado"], 0)
                self.assertEqual(self.estado["entrada_atual"], 2)
                self.assertEqual(self.estado["gales_reativos"], 2)
                self.assertEqual(self.estado["ciclo_gestao"], 1)
                self.assertFalse(self.estado["em_recuperacao"])
                self.assertEqual(self.executar_ciclo([("win", 1.6)]), [2])
                self.assertEqual(self.estado["gales_reativos"], 1)
                self.assertEqual(self.estado["prejuizo_acumulado"], 0)

    def test_dez_bancas_mantem_prejuizo_entrada_e_gales_independentes(self):
        robo = self.robo
        for indice, banca in enumerate(robo.BANCAS, start=1):
            for _ in range(indice):
                robo.aplicar_loss_ciclo(banca, 2)
        antes = {nome: dict(dados) for nome, dados in robo.estado_frentes.items()}
        robo.aplicar_win_recuperacao(self.banca, 2)
        self.assertEqual(self.estado["prejuizo_acumulado"], 0)
        self.assertEqual(self.estado["entrada_atual"], 2)
        self.assertEqual(self.estado["gales_reativos"], 0)
        for indice, banca in enumerate(robo.BANCAS[1:], start=2):
            self.assertEqual(robo.estado_frentes[banca], antes[banca])
            self.assertEqual(robo.estado_frentes[banca]["prejuizo_acumulado"], indice * 2)
            self.assertEqual(robo.estado_frentes[banca]["gales_reativos"], indice)

    def test_ciclos_e_gales_nao_tem_teto_artificial(self):
        self.robo = carregar_robo({"MAX_CICLOS": "1", "MAX_GALES": "1"})
        for indice in range(30):
            self.robo.aplicar_loss_ciclo(self.banca, 2)
            self.assertEqual(self.estado["gales_reativos"], indice + 1)
            self.assertEqual(self.estado["ciclo_gestao"], indice + 2)
        self.assertEqual(self.estado["prejuizo_acumulado"], 60)

    def test_interrupcoes_preservam_perda_sem_alterar_gales_ou_ciclo(self):
        for prejuizo_inicial in (0, 70):
            for fim in ("draw", "timeout", "recusa", "erro_resultado", "erro_ordem"):
                with self.subTest(prejuizo=prejuizo_inicial, fim=fim):
                    self.robo = carregar_robo()
                    if prejuizo_inicial:
                        self.preparar_recuperacao(prejuizo_inicial)
                    else:
                        self.estado["gales_reativos"] = 2
                    entrada = self.estado["entrada_atual"]
                    ciclo = self.estado["ciclo_gestao"]
                    gales = self.estado["gales_reativos"]
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
                                     round(2 + esperado * 0.5 / 0.8, 2))
                    self.assertEqual(self.estado["ciclo_gestao"], ciclo)
                    self.assertEqual(self.estado["gales_reativos"], gales)
                    self.assertTrue(self.estado["em_recuperacao"])
                    self.assertEqual(self.robo.stats["losses"], 0)
                    self.assertEqual(self.robo.stats["wins"], 0)
                    self.assertEqual(self.robo.perdas_progressao, 0 if not prejuizo_inicial else 1)

    def test_interrupcao_antes_de_perder_nao_altera_gestao(self):
        for resultado in ("draw", "timeout", "recusa"):
            with self.subTest(resultado=resultado):
                if resultado == "recusa":
                    self.executar_ciclo([], ordens=[(False, "recusada")])
                else:
                    self.executar_ciclo([(resultado, 0)])
                self.assertEqual(self.estado["prejuizo_acumulado"], 0)
                self.assertEqual(self.estado["entrada_atual"], 2)
                self.assertEqual(self.estado["gales_reativos"], 0)
                self.assertEqual(self.estado["ciclo_gestao"], 1)
                self.assertFalse(self.estado["em_recuperacao"])

    def test_erro_apos_loss_contabilizado_nao_duplica_prejuizo(self):
        with self.assertRaisesRegex(RuntimeError, "saldo indisponível"):
            self.executar_ciclo([("loss", -2)],
                                erro_saldo=RuntimeError("saldo indisponível"))
        self.assertEqual(self.estado["prejuizo_acumulado"], 2)
        self.assertEqual(self.estado["entrada_atual"], 3.25)
        self.assertEqual(self.estado["gales_reativos"], 1)
        self.assertEqual(self.estado["ciclo_gestao"], 2)
        self.assertEqual(self.robo.stats["losses"], 1)

    def test_erro_apos_win_contabilizado_nao_reconta_perdas(self):
        self.preparar_recuperacao()
        with self.assertRaisesRegex(RuntimeError, "saldo indisponível"):
            self.executar_ciclo([
                ("loss", -45.75), ("loss", -91.50), ("win", 146.40),
            ], erro_saldo=RuntimeError("saldo indisponível"))
        self.assertEqual(self.estado["prejuizo_acumulado"], 60.85)
        self.assertEqual(self.estado["entrada_atual"], 40.03)
        self.assertEqual(self.estado["gales_reativos"], 1)
        self.assertEqual(self.robo.stats["wins"], 1)

    def test_saldo_insuficiente_impede_ciclo_e_libera_banca(self):
        self.assertEqual(self.executar_ciclo([], saldo=1.99), [])
        self.assertEqual(self.robo.entradas_globais_aceitas, 0)
        self.assertEqual(self.estado["gales_reativos"], 0)
        self.assertEqual(self.estado["prejuizo_acumulado"], 0)

    def test_reservas_compartilham_saldo_sem_misturar_gestao(self):
        robo = self.robo
        outra, terceira = robo.BANCAS[1:3]
        api = MagicMock()
        api.get_balance.return_value = 4
        with patch.object(robo, "api", api), patch.object(robo, "garantir_practice", return_value=True):
            self.assertEqual(robo.reservar_entrada_percentual(self.banca), 2)
            self.assertEqual(robo.reservar_entrada_percentual(outra), 2)
            self.assertIsNone(robo.reservar_entrada_percentual(terceira))
        self.assertEqual(robo.reservas_pendentes, {self.banca: 2, outra: 2})
        self.assertEqual(robo.estado_frentes[terceira]["prejuizo_acumulado"], 0)
        self.assertEqual(robo.estado_frentes[terceira]["gales_reativos"], 0)

    def test_reservar_dez_bancas_e_rejeitar_duplicacao_ativo(self):
        robo = self.robo
        nomes = []
        for indice in range(10):
            nomes.append(robo.reservar_banca(f"ATIVO{indice}", "call", 80, 1, 1))
        self.assertEqual(nomes, robo.BANCAS)
        self.assertIsNone(robo.reservar_banca("ATIVO10", "call", 80, 1, 1))
        robo.liberar_banca(nomes[0], "ATIVO0")
        self.assertIsNone(robo.reservar_banca("ATIVO1", "call", 80, 1, 1))
        self.assertEqual(robo.reservar_banca("ATIVO10", "call", 80, 1, 1), nomes[0])

    def test_contadores_so_avancam_com_entrada_aceita(self):
        self.executar_ciclo([], ordens=[(False, "recusada")])
        self.assertEqual(self.robo.entradas_globais_aceitas, 0)
        self.assertEqual(self.estado["entradas_janela"], 0)
        self.executar_ciclo([("win", 1.60)])
        self.assertEqual(self.robo.entradas_globais_aceitas, 1)
        self.assertEqual(self.estado["entradas_janela"], 1)

    def test_reset_assertividade_nao_apaga_prejuizo(self):
        self.preparar_recuperacao(70, gales=2)
        self.estado.update(entradas_janela=99, wins=20, losses=10)
        self.executar_ciclo([("draw", 0)])
        self.assertEqual(self.estado["entradas_janela"], 0)
        self.assertEqual(self.estado["wins_base_janela"], 20)
        self.assertEqual(self.estado["losses_base_janela"], 10)
        self.assertEqual(self.estado["prejuizo_acumulado"], 70)
        self.assertEqual(self.estado["gales_reativos"], 2)


class IntegracoesTest(unittest.TestCase):
    def setUp(self):
        self.robo = carregar_robo({"EXECUTAR_ORDENS": "true", "CONTA": "REAL"})
        self.banca = self.robo.BANCAS[0]
        self.api = MagicMock()
        self.api.check_connect.return_value = True
        self.api.get_balance.return_value = 10000
        self.api.buy_digital_spot_v2.return_value = (True, 123)
        self.robo.api = self.api

    def test_ordem_escolhe_practice_antes_de_enviar(self):
        with patch.object(self.robo, "ativo_digital_disponivel", return_value=True):
            self.assertEqual(self.robo.executar_ordem(self.banca, "EURUSD-OTC", 2, "call", 1),
                             (True, 123))
        self.api.change_balance.assert_called_once_with("PRACTICE")
        self.api.buy_digital_spot_v2.assert_called_once_with("EURUSD-OTC", 2, "call", 1)
        chamadas = self.api.mock_calls
        self.assertLess(chamadas.index(call.change_balance("PRACTICE")),
                        chamadas.index(call.buy_digital_spot_v2("EURUSD-OTC", 2, "call", 1)))
        self.assertEqual(self.robo.stats["ordens_aceitas"], 1)
        self.api.buy_digital_spot.assert_not_called()
        self.api.buy.assert_not_called()

    def test_falha_ao_escolher_practice_impede_ordem(self):
        self.api.change_balance.side_effect = RuntimeError("sem conta de treino")
        self.assertEqual(self.robo.executar_ordem(self.banca, "EURUSD-OTC", 2, "call", 1),
                         (False, "sem_conexao"))
        self.api.buy_digital_spot_v2.assert_not_called()

    def test_execucao_desligada_nao_envia_ordem(self):
        self.robo.EXECUTAR_ORDENS = False
        self.assertEqual(self.robo.executar_ordem(self.banca, "EURUSD-OTC", 2, "call", 1),
                         (False, "EXECUTAR_ORDENS=false"))
        self.api.buy_digital_spot_v2.assert_not_called()

    def test_ordem_nao_ultrapassa_saldo_nem_fica_abaixo_minimo(self):
        for valor in (1.99, 10000.01):
            with self.subTest(valor=valor):
                self.assertEqual(self.robo.executar_ordem(self.banca, "EURUSD-OTC", valor, "call", 1),
                                 (False, "saldo_insuficiente"))
        self.api.buy_digital_spot_v2.assert_not_called()

    def test_instrumento_fechado_impede_ordem(self):
        with patch.object(self.robo, "ativo_digital_disponivel", return_value=False):
            self.assertEqual(self.robo.executar_ordem(self.banca, "EURUSD-OTC", 2, "call", 1),
                             (False, "instrumento_digital_fechado"))
        self.api.buy_digital_spot_v2.assert_not_called()

    def test_disponibilidade_cache_preserva_instrumentos_digitais_otc(self):
        self.robo._disponibilidade_cache.update(dados={
            "EURUSD-OTC": {"open": True}, "GOOGLE-OTC": {"open": True},
            "AMAZON-OTC": {"open": False}, "EURUSD": {"open": True},
        }, ate=float("inf"))
        self.assertEqual(self.robo.listar_ativos_otc(), ["EURUSD-OTC", "GOOGLE-OTC"])
        self.assertTrue(self.robo.ativo_digital_disponivel("GOOGLE-OTC"))
        self.assertFalse(self.robo.ativo_digital_disponivel("AMAZON-OTC"))
        self.assertIsNone(self.robo.ativo_digital_disponivel("DESCONHECIDO-OTC"))
        self.api.get_all_open_time.assert_not_called()

    def test_telegram_preservado_sem_requisicao_real(self):
        self.robo.TELEGRAM_TOKEN = "token-de-teste"
        self.robo.CHAT_ID = "chat-de-teste"
        self.robo.telegram("mensagem de teste")
        self.robo.requests.post.assert_called_once_with(
            "https://api.telegram.org/bottoken-de-teste/sendMessage",
            data={"chat_id": "chat-de-teste", "text": "mensagem de teste"}, timeout=10)

    def test_telegram_sem_configuracao_nao_envia(self):
        self.robo.telegram("mensagem de teste")
        self.robo.requests.post.assert_not_called()

    def test_conexao_anuncia_regra_correta_e_so_practice(self):
        self.robo.api = None
        self.robo.IQ_EMAIL = "email-de-teste"
        self.robo.IQ_PASSWORD = "senha-de-teste"
        self.robo.IQ_Option.return_value = self.api
        self.api.connect.return_value = (True, "ok")
        with patch.object(self.robo, "telegram") as telegram, patch.object(self.robo.time, "sleep"):
            self.assertTrue(self.robo.conectar())
        self.api.change_balance.assert_called_once_with("PRACTICE")
        mensagem = telegram.call_args.args[0]
        self.assertIn("PRACTICE", mensagem)
        self.assertIn("10 bancas", mensagem)
        self.assertIn("R$2.00", mensagem)
        self.assertIn("50%", mensagem)
        self.assertIn("GALE X2", mensagem)
        self.assertRegex(mensagem, r"WIN -1.*LOSS(?: completo)? \+1")
        self.assertNotIn("X1,5", mensagem)


class EstrategiaTest(unittest.TestCase):
    def test_parametros_e_timeframes_preservados(self):
        robo = carregar_robo()
        self.assertEqual(robo.SCORE_MIN, 65)
        self.assertEqual(robo.DIFERENCA_MINIMA, 15)
        self.assertEqual(robo.TIMEFRAMES, {
            1: {"segundos": 60, "expiracao": 1},
            5: {"segundos": 300, "expiracao": 5},
            15: {"segundos": 900, "expiracao": 5},
        })
        self.assertIsNone(robo.analisar([]))
        self.assertEqual(robo.media([1, 2, 3]), 2)
        self.assertAlmostEqual(robo.desvio([1, 2, 3]), (2 / 3) ** 0.5)
        self.assertEqual(robo.ema([1, 2, 3, 4], 3), 3)
        self.assertEqual(robo.rsi(list(range(20))), 100)
        self.assertEqual(robo.rsi(list(reversed(range(20)))), 0)
        self.assertAlmostEqual(robo.macd(list(range(60))), 7)


if __name__ == "__main__":
    unittest.main()
