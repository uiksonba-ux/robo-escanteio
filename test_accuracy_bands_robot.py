"""Tests extracted pure configuration without importing robo.py (which starts workers)."""
import ast
import pathlib
import unittest

source = pathlib.Path(__file__).with_name("robo.py").read_text(encoding="utf-8")
tree = ast.parse(source)
names = {"gestao_por_assertividade", "gales_por_assertividade"}
nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
assigns = [n for n in tree.body if isinstance(n, ast.Assign) and any(
    isinstance(t, ast.Name) and t.id in {"MAX_GALES", "LIMIAR_ASSERTIVIDADE_SEM_GALE",
    "MULTIPLICADOR_GALE", "FAIXAS_ASSERTIVIDADE_ATIVAS", "FAIXAS_ASSERTIVIDADE"} for t in n.targets)]
scope = {}
exec(compile(ast.Module(body=assigns + nodes, type_ignores=[]), "<isolated>", "exec"), scope)

class BandTests(unittest.TestCase):
    def test_boundaries(self):
        f = scope["gestao_por_assertividade"]
        for w,l,g in [(0,0,4),(49,51,4),(50,50,3),(59,41,3),
                       (60,40,2),(69,31,2),(70,30,1),(79,21,1),
                       (80,20,0),(100,0,0)]:
            self.assertEqual(f(w,l),(g,2.0))

    def test_disabled_fallback(self):
        f = scope["gestao_por_assertividade"]
        scope["FAIXAS_ASSERTIVIDADE_ATIVAS"] = False
        self.assertEqual(f(59,41),(2,2.0))
        self.assertEqual(f(60,40),(0,2.0))

    def test_order_context_freezes_band(self):
        self.assertIn('"multiplicador": gestao_por_assertividade(', source)
        self.assertIn('contexto.get("multiplicador", MULTIPLICADOR_GALE)', source)
        self.assertIn('max(0, stats["wins"] - wins_base_ponderada)', source)

if __name__ == "__main__":
    unittest.main()
