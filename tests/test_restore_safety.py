import ast
import pathlib
import unittest

SOURCE = pathlib.Path(__file__).resolve().parents[1] / "robo.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
FUNCTIONS = {node.name: node for node in TREE.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}

class RestoreSafetyTests(unittest.TestCase):
    def test_assertiveness_boundaries(self):
        node = FUNCTIONS["gales_por_assertividade"]
        namespace = {}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), "exec"), namespace)
        cases = [(0,4),(49.99,4),(50,3),(59.99,3),(60,2),(69.99,2),(70,1),(79.99,1),(80,0),(100,0)]
        for pct, expected in cases:
            with self.subTest(pct=pct):
                self.assertEqual(namespace["gales_por_assertividade"](pct), expected)

    def test_practice_guard_present(self):
        self.assertIn("garantir_practice", FUNCTIONS)
        guard = ast.get_source_segment(SOURCE.read_text(encoding="utf-8"), FUNCTIONS["garantir_practice"])
        self.assertIn('change_balance(', guard)
        self.assertIn('"PRACTICE"', guard)

    def test_practice_mode_must_be_confirmed(self):
        source = SOURCE.read_text(encoding="utf-8")
        guard = ast.get_source_segment(source, FUNCTIONS["garantir_practice"])
        self.assertIn("get_balance_mode()", guard)
        self.assertIn('!= "PRACTICE"', guard)
        self.assertIn("return False", guard)

    def test_practice_guard_rejects_real_mode_and_accepts_practice(self):
        import threading
        from unittest.mock import Mock
        node = FUNCTIONS["garantir_practice"]
        api = Mock()
        api.get_balance_mode.return_value = "REAL"
        namespace = {
            "garantir_conexao": lambda: True,
            "api_lock": threading.RLock(),
            "api": api,
            "log": Mock(),
        }
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), "exec"), namespace)
        self.assertFalse(namespace["garantir_practice"]())
        api.change_balance.assert_called_with("PRACTICE")
        api.get_balance_mode.return_value = "PRACTICE"
        self.assertTrue(namespace["garantir_practice"]())
        api.get_balance_mode.side_effect = RuntimeError("connection failed")
        self.assertFalse(namespace["garantir_practice"]())

    def test_duplicate_order_protection(self):
        source = SOURCE.read_text(encoding="utf-8")
        managed = ast.get_source_segment(source, FUNCTIONS["enviar_ordem_gerida"])
        self.assertIn("contador_entrada_lock", managed)
        self.assertIn("ordens_pendentes.values()", managed)
        self.assertIn('"ordem_ja_pendente"', managed)
        self.assertIn("order_id in ordens_pendentes", managed)
        self.assertIn("marcar_envio_indeterminado", managed)

    def test_no_new_underlying_catalog_gate(self):
        self.assertNotIn("get_digital_underlying_list_data", SOURCE.read_text(encoding="utf-8"))

    def test_global_accuracy_shared(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn("taxa_ponderada(stats[\"wins\"], stats[\"losses\"])", source)
        self.assertIn("gales_por_assertividade(assertividade_atual)", source)

if __name__ == "__main__":
    unittest.main()
