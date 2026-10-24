"""Validação da gestão global por posição; APIs, Telegram e worker ficam isolados."""

import importlib.util
import os
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import MagicMock, call, patch
from concurrent.futures import ThreadPoolExecutor
import itertools


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


class GestaoGlobalTest(unittest.TestCase):
    def setUp(self):
        self.robo = carregar_robo()
        self.banca = self.robo.BANCAS[0]
        self.api = MagicMock()
        self.api.get_balance.return_value = 1000
        self.robo.api = self.api
        self.ids = itertools.count(1001)
        self.compras = []
        self.resultados_envio = []
        self.patches = [
            patch.object(self.robo, "garantir_practice", return_value=True),
            patch.object(self.robo, "executar_ordem", side_effect=self.comprar),
            patch.object(self.robo, "telegram"),
            patch.object(self.robo.time, "sleep"),
            patch.object(self.robo, "texto_saldo", return_value="1000.00"),
        ]
        for remendo in self.patches:
            remendo.start()
            self.addCleanup(remendo.stop)

    def comprar(self, banca, ativo, valor, direcao, expiracao):
        self.compras.append((banca, ativo, valor, direcao, expiracao))
        if self.resultados_envio:
            proximo = self.resultados_envio.pop(0)
            if isinstance(proximo, Exception):
                raise proximo
            return proximo
        return True, next(self.ids)

    def enviar(self, banca=None, nivel=0):
        banca = banca or self.banca
        return self.robo.enviar_ordem_gerida(banca, f"ATIVO-{banca}", "call", 1, nivel=nivel)

    def concluir(self, order_id, resultado, lucro, banca=None):
        self.robo.registrar_resultado_ordem(order_id, resultado, lucro)
        return self.robo.finalizar_operacao_global(banca or self.banca)

    def test_percentual_practice_minimo_e_arredondamento_comercial(self):
        robo = self.robo
        self.assertEqual(robo.PERCENTUAL_ENTRADA, 0.01)
        self.assertEqual(robo.ENTRADA_MINIMA, 2)
        self.assertEqual(robo.MULTIPLICADOR_GALE, 2)
        self.assertEqual(robo.RECUPERACAO_PERCENTUAL, 1)
        self.assertEqual(robo.CONTA, "PRACTICE")
        self.assertEqual(len(robo.BANCAS), 10)
        for saldo, entrada in ((0, 2), (100, 2), (200, 2), (200.5, 2.01),
                                (299.5, 3), (500.5, 5.01), (1000, 10), (10000, 100)):
            with self.subTest(saldo=saldo):
                self.assertEqual(robo.obter_entrada_base(saldo), entrada)
        for saldo in (float("nan"), float("inf")):
            with self.subTest(saldo=saldo), self.assertRaises(ValueError):
                robo.obter_entrada_base(saldo)

    def test_recuperacao_integral_dividida_pelo_payout_com_teto_de_centavos(self):
        for prejuizo, esperado in ((0, 10), (-10, 10), (0.01, 10.02),
                                   (2, 12.50), (10, 22.50), (70, 97.50)):
            with self.subTest(prejuizo=prejuizo):
                self.assertEqual(self.robo.calcular_entrada_recuperacao(prejuizo, 1000), esperado)
        self.robo.PAYOUT_RECUPERACAO = 0.6
        self.assertEqual(self.robo.calcular_entrada_recuperacao(1, 1000), 11.67)

    def test_ambiente_nao_altera_conta_percentual_gales_ou_recuperacao(self):
        robo = carregar_robo({"CONTA": "REAL", "ENTRADA_BASE": "100",
                              "ENTRADA_PERCENTUAL": "0.99", "MAX_GALES": "1",
                              "MULTIPLICADOR_GALE": "3", "RECUPERACAO_PERCENTUAL": "0.5"})
        self.assertEqual(robo.CONTA, "PRACTICE")
        self.assertEqual(robo.obter_entrada_base(1000), 10)
        self.assertEqual(robo.MULTIPLICADOR_GALE, 2)
        self.assertEqual(robo.RECUPERACAO_PERCENTUAL, 1)

    def test_doze_ordens_iniciais_seguem_posicoes_globais_zero_a_nove(self):
        posicoes, numeros, gales = [], [], []
        for indice in range(12):
            banca = self.robo.BANCAS[(indice * 3) % 10]
            ok, order_id, contexto = self.enviar(banca)
            self.assertTrue(ok)
            posicoes.append(contexto["posicao"])
            numeros.append(contexto["numero"])
            gales.append(contexto["gales"])
            self.assertEqual(contexto["entrada"], 10)
            self.assertEqual(contexto["base"], 10)
            self.assertEqual(contexto["primeira_ordem"], order_id)
            self.concluir(order_id, "draw", 0, banca)
        self.assertEqual(posicoes, list(range(10)) + [0, 1])
        self.assertEqual(gales, posicoes)
        self.assertEqual(numeros, list(range(1, 13)))
        self.assertEqual(self.robo.entradas_globais_aceitas, 12)
        self.assertEqual(len(self.robo.operacoes_concluidas), 12)

    def test_win_e_loss_nao_mudam_ou_reutilizam_posicao(self):
        for indice, (resultado, lucro) in enumerate((("loss", -10), ("win", 30),
                                                   ("loss", -5), ("win", 5))):
            ok, order_id, contexto = self.enviar()
            self.assertTrue(ok)
            self.assertEqual(contexto["posicao"], indice)
            self.concluir(order_id, resultado, lucro)
        self.assertEqual(self.robo.entradas_globais_aceitas, 4)

    def test_base_reflete_saldo_practice_de_cada_inicial(self):
        for saldo, entrada in ((1000, 10), (500, 5), (500.5, 5.01), (199, 2)):
            self.api.get_balance.return_value = saldo
            ok, order_id, contexto = self.enviar()
            self.assertTrue(ok)
            self.assertEqual(contexto["base"], entrada)
            self.assertEqual(contexto["entrada"], entrada)
            self.concluir(order_id, "draw", 0)
        self.assertEqual([x[2] for x in self.compras], [10, 5, 5.01, 2])

    def test_gales_dobram_sem_consumir_outra_posicao(self):
        self.robo.entradas_globais_aceitas = 3
        ok, inicial, contexto = self.enviar()
        self.assertTrue(ok)
        self.assertEqual(contexto["gales"], 3)
        self.robo.registrar_resultado_ordem(inicial, "loss", -10)
        for nivel, perda in ((1, -20), (2, -40), (3, -80)):
            ok, order_id, mesmo = self.enviar(nivel=nivel)
            self.assertTrue(ok)
            self.assertEqual(mesmo["numero"], 4)
            self.robo.registrar_resultado_ordem(order_id, "loss", perda)
        self.assertEqual([x[2] for x in self.compras], [10, 20, 40, 80])
        self.assertEqual(self.robo.entradas_globais_aceitas, 4)
        self.assertEqual(self.robo.resultado_global_centavos, 0)
        resumo = self.robo.finalizar_operacao_global(self.banca)
        self.assertEqual(resumo["resultado_liquido_global"], -150)
        self.assertEqual(self.robo.resultado_global_centavos, -15000)

    def test_perdas_em_gales_nao_viram_divida_antes_do_fechamento(self):
        self.robo.entradas_globais_aceitas = 1
        ok, inicial, _ = self.enviar()
        self.robo.registrar_resultado_ordem(inicial, "loss", -10)
        outra = self.robo.BANCAS[1]
        ok, segundo, contexto = self.enviar(outra)
        self.assertTrue(ok)
        self.assertEqual(contexto["prejuizo_alocado"], 0)
        self.assertEqual(contexto["entrada"], 10)
        self.assertEqual(self.robo.resultado_global_centavos, 0)
        ok, gale, _ = self.enviar(nivel=1)
        self.robo.registrar_resultado_ordem(gale, "win", 16)
        self.robo.finalizar_operacao_global(self.banca)
        self.assertEqual(self.robo.resultado_global_centavos, 600)
        self.concluir(segundo, "draw", 0, outra)
        self.assertEqual(self.robo.resultado_global_centavos, 600)

    def test_ledger_preserva_creditos_e_usa_resultados_reais(self):
        for resultado, lucro, saldo_contabil in (("win", 20, 2000),
                                                 ("loss", -15, 500),
                                                 ("loss", -7, -200)):
            ok, order_id, contexto = self.enviar()
            self.assertTrue(ok)
            self.assertEqual(contexto["prejuizo_alocado"], 0)
            self.concluir(order_id, resultado, lucro)
            self.assertEqual(self.robo.resultado_global_centavos, saldo_contabil)
        ok, order_id, contexto = self.enviar()
        self.assertTrue(ok)
        self.assertEqual(contexto["prejuizo_alocado"], 2)
        self.assertEqual(contexto["entrada"], 12.50)
        self.concluir(order_id, "win", 10)
        self.assertEqual(self.robo.resultado_global_centavos, 800)

    def test_win_gale_com_liquido_negativo_acumula_so_o_saldo_do_sinal(self):
        self.robo.entradas_globais_aceitas = 2
        ok, inicial, _ = self.enviar()
        self.robo.registrar_resultado_ordem(inicial, "loss", -10)
        ok, g1, _ = self.enviar(nivel=1)
        self.robo.registrar_resultado_ordem(g1, "loss", -20)
        ok, g2, _ = self.enviar(nivel=2)
        self.robo.registrar_resultado_ordem(g2, "win", 25)
        self.assertEqual(self.robo.resultado_global_centavos, 0)
        resumo = self.robo.finalizar_operacao_global(self.banca)
        self.assertEqual(resumo["resultado_liquido_global"], -5)
        self.assertEqual(resumo["prejuizo_global"], 5)
        self.assertEqual(self.robo.resultado_global_centavos, -500)

    def test_uma_lease_de_recuperacao_e_lucro_de_outros_abate_divida(self):
        self.robo.resultado_global_centavos = -1000
        ok, inicial, contexto = self.enviar()
        self.assertTrue(ok)
        self.assertEqual(contexto["prejuizo_alocado"], 10)
        self.assertEqual(contexto["entrada"], 22.50)
        self.assertEqual(self.robo.recuperacao_em_curso, self.banca)
        outra = self.robo.BANCAS[1]
        ok, segundo, paralelo = self.enviar(outra)
        self.assertTrue(ok)
        self.assertEqual(paralelo["prejuizo_alocado"], 0)
        self.assertEqual(paralelo["entrada"], 10)
        self.concluir(segundo, "win", 4, outra)
        self.assertEqual(self.robo.resultado_global_centavos, -600)
        self.assertEqual(self.robo.recuperacao_em_curso, self.banca)
        self.concluir(inicial, "win", 18)
        self.assertEqual(self.robo.resultado_global_centavos, 1200)
        self.assertIsNone(self.robo.recuperacao_em_curso)
        ok, _, seguinte = self.enviar(outra)
        self.assertTrue(ok)
        self.assertEqual(seguinte["entrada"], 10)
        self.assertEqual(seguinte["prejuizo_alocado"], 0)

    def test_resultado_e_finalizacao_idempotentes(self):
        ok, order_id, contexto = self.enviar()
        self.assertTrue(ok)
        self.assertIn(order_id, self.robo.ordens_pendentes)
        self.assertIsNone(self.robo.finalizar_operacao_global(self.banca))
        self.assertEqual(self.robo.resultado_global_centavos, 0)
        self.robo.registrar_resultado_ordem(order_id, "loss", -3.65)
        self.robo.registrar_resultado_ordem(order_id, "loss", -3.65)
        self.robo.registrar_resultado_ordem(order_id, "win", 999)
        self.assertNotIn(order_id, self.robo.ordens_pendentes)
        self.assertIn(order_id, self.robo.ordens_liquidadas)
        self.assertEqual(self.robo.operacoes_ativas[self.banca]["resultado_centavos"], -365)
        self.robo.finalizar_operacao_global(self.banca)
        self.robo.finalizar_operacao_global(self.banca)
        self.robo.registrar_resultado_ordem(order_id, "loss", -3.65)
        self.assertEqual(self.robo.resultado_global_centavos, -365)
        self.assertIn(order_id, self.robo.operacoes_concluidas)
        self.assertNotIn(self.banca, self.robo.operacoes_ativas)

    def test_segunda_inicial_da_mesma_banca_nao_duplicada(self):
        ok, order_id, _ = self.enviar()
        self.assertTrue(ok)
        self.enviar()
        self.assertEqual(len(self.compras), 1)
        self.assertEqual(self.robo.entradas_globais_aceitas, 1)
        self.assertEqual(len(self.robo.ordens_pendentes), 1)
        self.assertIn(order_id, self.robo.ordens_pendentes)

    def test_recusa_inicial_nao_avanca_posicao_nem_mantem_lease(self):
        self.robo.resultado_global_centavos = -1000
        self.resultados_envio = [(False, "recusada")]
        ok, _, _ = self.enviar()
        self.assertFalse(ok)
        self.assertEqual(self.robo.entradas_globais_aceitas, 0)
        self.assertIsNone(self.robo.recuperacao_em_curso)
        self.assertNotIn(self.banca, self.robo.operacoes_ativas)
        self.assertFalse(self.robo.ordens_pendentes)
        ok, _, contexto = self.enviar()
        self.assertTrue(ok)
        self.assertEqual(contexto["posicao"], 0)
        self.assertEqual(contexto["prejuizo_alocado"], 10)

    def test_recusa_gale_fecha_perdas_confirmadas_sem_consumir_posicao(self):
        self.robo.entradas_globais_aceitas = 1
        ok, inicial, _ = self.enviar()
        self.robo.registrar_resultado_ordem(inicial, "loss", -10)
        self.resultados_envio = [(False, "recusada")]
        ok, _, _ = self.enviar(nivel=1)
        self.assertFalse(ok)
        self.robo.finalizar_operacao_global(self.banca)
        self.assertEqual(self.robo.resultado_global_centavos, -1000)
        self.assertEqual(self.robo.entradas_globais_aceitas, 2)
        self.assertFalse(self.robo.ordens_pendentes)

    def test_envio_indeterminado_bloqueia_todos_os_envios(self):
        self.robo.resultado_global_centavos = -1000
        self.resultados_envio = [(None, "timeout_envio")]
        ok, _, _ = self.enviar()
        self.assertIsNone(ok)
        self.assertTrue(self.robo.envio_indeterminado)
        self.assertEqual(self.robo.entradas_globais_aceitas, 0)
        self.assertEqual(self.robo.resultado_global_centavos, -1000)
        self.enviar(self.robo.BANCAS[1])
        self.enviar(self.banca)
        self.assertEqual(len(self.compras), 1)
        self.assertEqual(self.robo.entradas_globais_aceitas, 0)

    def test_dez_iniciais_concorrentes_tem_posicoes_unicas_e_uma_lease(self):
        self.robo.resultado_global_centavos = -1000
        barreira = threading.Barrier(10)
        def enviar_concorrente(banca):
            barreira.wait(timeout=5)
            return banca, self.enviar(banca)
        with ThreadPoolExecutor(max_workers=10) as executor:
            futuros = [executor.submit(enviar_concorrente, banca) for banca in self.robo.BANCAS]
            respostas = [futuro.result(timeout=10) for futuro in futuros]
        self.assertEqual(self.robo.entradas_globais_aceitas, 10)
        contextos = [resposta[1][2] for resposta in respostas]
        self.assertTrue(all(resposta[1][0] for resposta in respostas))
        self.assertEqual(sorted(x["posicao"] for x in contextos), list(range(10)))
        self.assertEqual(sorted(x["numero"] for x in contextos), list(range(1, 11)))
        self.assertEqual(sum(x["prejuizo_alocado"] > 0 for x in contextos), 1)
        self.assertEqual(sum(x["entrada"] == 22.50 for x in contextos), 1)
        self.assertEqual(sum(x["entrada"] == 10 for x in contextos), 9)
        dono = self.robo.recuperacao_em_curso
        self.assertEqual(self.robo.operacoes_ativas[dono]["prejuizo_alocado"], 10)

    def test_corrida_finalizacao_e_nova_ordem_nao_duplica_recuperacao(self):
        self.robo.resultado_global_centavos = -1000
        ok, inicial, _ = self.enviar()
        self.assertTrue(ok)
        self.robo.registrar_resultado_ordem(inicial, "win", 20)
        outra = self.robo.BANCAS[1]
        barreira = threading.Barrier(2)
        def finalizar():
            barreira.wait(timeout=5)
            return self.robo.finalizar_operacao_global(self.banca)
        def nova_ordem():
            barreira.wait(timeout=5)
            return self.enviar(outra)
        with ThreadPoolExecutor(max_workers=2) as executor:
            fim = executor.submit(finalizar)
            nova = executor.submit(nova_ordem)
            resumo, (ok, _, contexto) = fim.result(timeout=10), nova.result(timeout=10)
        self.assertTrue(ok)
        self.assertEqual(contexto["prejuizo_alocado"], 0)
        self.assertEqual(contexto["entrada"], 10)
        self.assertEqual(self.robo.resultado_global_centavos, 1000)
        self.assertIsNone(self.robo.recuperacao_em_curso)

    def executar_ciclo(self, resultados, antes_resultado=None):
        robo = self.robo
        ativo = f"ATIVO-{self.banca}"
        self.assertEqual(robo.reservar_banca(ativo, "call", 80, 1, 1), self.banca)
        ids_consultados = []
        retornos = iter(resultados)
        def aguardar(order_id):
            ids_consultados.append(order_id)
            if antes_resultado:
                antes_resultado(order_id, len(ids_consultados))
            resultado = next(retornos)
            if isinstance(resultado, Exception):
                raise resultado
            return resultado
        with patch.object(robo, "aguardar_resultado", side_effect=aguardar):
            robo.ciclo(self.banca, ativo, "call", 80, 1, 1)
        return ids_consultados

    def assert_banca_livre(self):
        self.assertFalse(self.robo.estado_frentes[self.banca]["ocupada"])
        self.assertIsNone(self.robo.estado_frentes[self.banca]["order_id"])
        self.assertNotIn(f"ATIVO-{self.banca}", self.robo.ativos_em_uso)
        self.assertNotIn(self.banca, self.robo.operacoes_ativas)

    def test_ciclo_timeout_reconsulta_mesma_ordem_sem_reenvio_e_preserva_lease(self):
        self.robo.resultado_global_centavos = -1000
        self.robo.entradas_globais_aceitas = 1
        def conferir(order_id, chamada):
            self.assertTrue(self.robo.estado_frentes[self.banca]["ocupada"])
            self.assertIn(order_id, self.robo.ordens_pendentes)
            self.assertEqual(self.robo.recuperacao_em_curso, self.banca)
            if chamada == 3:
                self.assertEqual(self.robo.resultado_global_centavos, -1000)
        consultados = self.executar_ciclo([
            ("loss", -22.50), ("timeout", 0), ("win", 36),
        ], conferir)
        self.assertEqual(consultados, [1001, 1002, 1002])
        self.assertEqual([x[2] for x in self.compras], [22.50, 45])
        self.assertEqual(self.robo.entradas_globais_aceitas, 2)
        self.assertEqual(self.robo.resultado_global_centavos, 350)
        self.assertEqual(self.robo.stats["wins"], 1)
        self.assertEqual(self.robo.stats["win_g1"], 1)
        self.assertIsNone(self.robo.recuperacao_em_curso)
        self.assert_banca_livre()

    def test_ciclo_excecao_resultado_reconsulta_mesma_ordem(self):
        consultados = self.executar_ciclo([RuntimeError("falha de consulta"), ("win", 8)])
        self.assertEqual(consultados, [1001, 1001])
        self.assertEqual(len(self.compras), 1)
        self.assertEqual(self.robo.resultado_global_centavos, 800)
        self.assertEqual(self.robo.stats["wins"], 1)
        self.assert_banca_livre()

    def test_ciclo_recusa_gale_fecha_prejuizo_real_e_libera_banca(self):
        self.robo.entradas_globais_aceitas = 1
        self.resultados_envio = [(True, 1001), (False, "recusada")]
        consultados = self.executar_ciclo([("loss", -10)])
        self.assertEqual(consultados, [1001])
        self.assertEqual(self.robo.resultado_global_centavos, -1000)
        self.assertEqual(self.robo.entradas_globais_aceitas, 2)
        self.assert_banca_livre()

    def test_ciclo_duplicado_nao_reenvia_nem_libera_operacao_em_curso(self):
        def duplicar(order_id, chamada):
            ativo = f"ATIVO-{self.banca}"
            self.robo.ciclo(self.banca, ativo, "call", 80, 1, 1)
            self.assertTrue(self.robo.estado_frentes[self.banca]["ocupada"])
            self.assertIn(order_id, self.robo.ordens_pendentes)
        self.executar_ciclo([("win", 8)], duplicar)
        self.assertEqual(len(self.compras), 1)
        self.assertEqual(self.robo.entradas_globais_aceitas, 1)
        self.assertEqual(self.robo.resultado_global_centavos, 800)
        self.assert_banca_livre()


    def test_saldo_pendente_nao_pode_ser_reutilizado(self):
        self.api.get_balance.return_value = 10
        primeiras = []
        for banca in self.robo.BANCAS[:5]:
            ok, order_id, _ = self.enviar(banca)
            self.assertTrue(ok)
            primeiras.append(order_id)
        ok, motivo, _ = self.enviar(self.robo.BANCAS[5])
        self.assertFalse(ok)
        self.assertEqual(motivo, "saldo_insuficiente")
        self.assertEqual(self.robo.entradas_globais_aceitas, 5)
        self.assertEqual(len(self.compras), 5)
        self.concluir(primeiras[0], "draw", 0, self.robo.BANCAS[0])
        ok, _, contexto = self.enviar(self.robo.BANCAS[5])
        self.assertTrue(ok)
        self.assertEqual(contexto["posicao"], 5)
        self.assertEqual(self.robo.entradas_globais_aceitas, 6)

    def test_recuperacao_sem_saldo_nao_reserva_lease_ou_posicao(self):
        self.api.get_balance.return_value = 10
        self.robo.resultado_global_centavos = -1000
        ok, motivo, _ = self.enviar()
        self.assertFalse(ok)
        self.assertEqual(motivo, "saldo_insuficiente")
        self.assertEqual(self.robo.entradas_globais_aceitas, 0)
        self.assertIsNone(self.robo.recuperacao_em_curso)
        self.assertFalse(self.robo.reservas_pendentes)
        self.assertFalse(self.compras)

    def test_excecao_no_envio_pausa_todos_os_envios_sem_apagar_divida(self):
        self.robo.resultado_global_centavos = -1000
        self.resultados_envio = [RuntimeError("conexão caiu durante envio")]
        ok, _, _ = self.enviar()
        self.assertIsNone(ok)
        self.assertTrue(self.robo.envio_indeterminado)
        self.assertEqual(self.robo.recuperacao_em_curso, self.banca)
        self.assertEqual(self.robo.reservas_pendentes[self.banca], 22.50)
        self.assertEqual(self.robo.entradas_globais_aceitas, 0)
        self.assertEqual(self.robo.resultado_global_centavos, -1000)
        self.enviar(self.robo.BANCAS[1])
        self.assertEqual(len(self.compras), 1)

    def test_id_repetido_nao_consume_posicao_e_pausa_novos_envios(self):
        self.resultados_envio = [(True, 1001), (True, 1001)]
        ok, _, _ = self.enviar()
        self.assertTrue(ok)
        ok, _, _ = self.enviar(self.robo.BANCAS[1])
        self.assertIsNone(ok)
        self.assertTrue(self.robo.envio_indeterminado)
        self.assertEqual(self.robo.entradas_globais_aceitas, 1)
        self.assertEqual(len(self.robo.ordens_pendentes), 1)
        self.enviar(self.robo.BANCAS[2])
        self.assertEqual(len(self.compras), 2)

    def test_ciclo_envio_incerto_mantem_banca_e_ativo_reservados(self):
        self.robo.resultado_global_centavos = -1000
        self.resultados_envio = [(None, "envio sem confirmação")]
        self.executar_ciclo([])
        self.assertTrue(self.robo.estado_frentes[self.banca]["ocupada"])
        self.assertIn(f"ATIVO-{self.banca}", self.robo.ativos_em_uso)
        self.assertTrue(self.robo.estado_frentes[self.banca]["envio_indeterminado"])
        self.assertEqual(self.robo.recuperacao_em_curso, self.banca)
        self.assertEqual(self.robo.entradas_globais_aceitas, 0)
        self.assertIsNone(self.robo.reservar_banca("NOVO-ATIVO", "call", 80, 1, 1))

    def test_health_e_home_expoem_mesma_gestao_global(self):
        self.robo.resultado_global_centavos = -1000
        ok, _, contexto = self.enviar()
        self.assertTrue(ok)
        home = self.robo.home()
        health, codigo = self.robo.health()
        self.assertEqual(codigo, 200)
        self.assertEqual(home["conta"], "PRACTICE")
        self.assertEqual(health["conta"], "PRACTICE")
        for gestao in (home["gestao"], health):
            self.assertEqual(gestao["entrada_base"], 10)
            self.assertEqual(gestao["entrada_percentual"], 0.01)
            self.assertEqual(gestao["prejuizo_global"], 10)
            self.assertEqual(gestao["resultado_liquido_global"], -10)
            self.assertEqual(gestao["recuperacao_percentual"], 1)
            self.assertEqual(gestao["gales_proximos"], 1)
            self.assertEqual(gestao["sequencia_gales"], list(range(10)))
            self.assertEqual(gestao["recuperacao_em_curso"], self.banca)
            self.assertEqual(gestao["ordens_pendentes"], 1)
        self.assertEqual(len(home["bancas"]), 10)
        self.assertEqual(health["bancas"], 10)
        self.assertIn("100%", home["gestao"]["regra_recuperacao"])


    def test_resultados_concorrentes_e_finalizadores_nao_duplicam_ledger(self):
        ok, order_id, _ = self.enviar()
        self.assertTrue(ok)
        barreira = threading.Barrier(4)
        def liquidar():
            barreira.wait(timeout=5)
            return self.robo.registrar_resultado_ordem(order_id, "loss", -10)
        with ThreadPoolExecutor(max_workers=4) as executor:
            futuros = [executor.submit(liquidar) for _ in range(4)]
            aceitos = [futuro.result(timeout=10) for futuro in futuros]
        self.assertEqual(aceitos.count(True), 1)
        self.assertEqual(self.robo.operacoes_ativas[self.banca]["resultado_centavos"], -1000)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futuros = [executor.submit(self.robo.finalizar_operacao_global, self.banca) for _ in range(2)]
            [futuro.result(timeout=10) for futuro in futuros]
        self.assertEqual(self.robo.resultado_global_centavos, -1000)
        self.assertEqual(len(self.robo.operacoes_concluidas), 1)

    def test_resultado_inconsistente_ou_nao_finito_nao_altera_ledger(self):
        ok, order_id, _ = self.enviar()
        self.assertTrue(ok)
        for resultado, lucro in (("win", -1), ("loss", 1), ("draw", 1),
                                  ("win", float("nan")), ("loss", float("-inf"))):
            with self.subTest(resultado=resultado, lucro=lucro), self.assertRaises(ValueError):
                self.robo.registrar_resultado_ordem(order_id, resultado, lucro)
        self.assertIn(order_id, self.robo.ordens_pendentes)
        self.assertEqual(self.robo.operacoes_ativas[self.banca]["resultado_centavos"], 0)
        self.assertEqual(self.robo.resultado_global_centavos, 0)

    def test_payout_configuravel_e_invalido_usa_padrao(self):
        robo = carregar_robo({"PAYOUT_RECUPERACAO": "0.5"})
        self.assertEqual(robo.calcular_entrada_recuperacao(10, 1000), 30)
        for invalido in ("inválido", "nan", "inf"):
            with self.subTest(valor=invalido):
                robo = carregar_robo({"PAYOUT_RECUPERACAO": invalido})
                self.assertEqual(robo.PAYOUT_RECUPERACAO, 0.8)
                self.assertEqual(robo.calcular_entrada_recuperacao(10, 1000), 22.50)


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



    def test_conexao_anuncia_gestao_global_e_conta_practice(self):
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
        self.assertIn("1%", mensagem)
        self.assertIn("100%", mensagem)
        self.assertIn("10 bancas", mensagem)
        self.assertIn("GALE X2", mensagem)
        self.assertIn("0 → 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 0", mensagem)
        self.assertNotIn("50%", mensagem)
        self.assertEqual(self.robo.obter_entrada_base(), 100)

    def test_excecao_de_compra_retorna_envio_indeterminado(self):
        self.api.buy_digital_spot_v2.side_effect = RuntimeError("conexão caiu")
        with patch.object(self.robo, "ativo_digital_disponivel", return_value=True):
            ok, _ = self.robo.executar_ordem(self.banca, "EURUSD-OTC", 2, "call", 1)
        self.assertIsNone(ok)
        self.api.buy_digital_spot_v2.assert_called_once()
        self.assertEqual(self.robo.stats["ordens_recusadas"], 0)

    def test_resposta_de_compra_ambigua_retorna_indeterminado(self):
        self.api.buy_digital_spot_v2.return_value = None
        with patch.object(self.robo, "ativo_digital_disponivel", return_value=True):
            ok, _ = self.robo.executar_ordem(self.banca, "EURUSD-OTC", 2, "call", 1)
        self.assertIsNone(ok)
        self.api.buy_digital_spot_v2.assert_called_once()
        self.assertEqual(self.robo.stats["ordens_recusadas"], 0)

    def test_resultado_real_ignora_valores_invalidos_ate_liquidacao_valida(self):
        for lucro, esperado in ((0, "draw"), (8, "win"), (-10, "loss")):
            with self.subTest(lucro=lucro):
                self.api.check_win_digital_v2.reset_mock()
                self.api.check_win_digital_v2.side_effect = [
                    (False, None), (True, None), (True, "texto"),
                    (True, float("nan")), (True, float("inf")),
                    (True, float("-inf")), (True, lucro),
                ]
                with (
                    patch.object(self.robo, "garantir_conexao", return_value=True),
                    patch.object(self.robo.time, "sleep"),
                ):
                    self.assertEqual(self.robo.aguardar_resultado(999), (esperado, lucro))
                self.assertEqual(self.api.check_win_digital_v2.call_args_list,
                                 [call(999)] * 7)


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

