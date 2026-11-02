"""Tests extracted pure configuration without importing robo.py (which starts workers)."""
import ast
import pathlib
import unittest

source = pathlib.Path(__file__).with_name("robo.py").read_text(encoding="utf-8")
tree = ast.parse(source)
names = {"gestao_por_assertividade", "gestao_sincronizada_practice", "gales_por_assertividade"}
nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
assigns = [n for n in tree.body if isinstance(n, ast.Assign) and any(
    isinstance(t, ast.Name) and t.id in {"MAX_GALES", "LIMIAR_ASSERTIVIDADE_SEM_GALE",
    "MULTIPLICADOR_GALE", "FAIXAS_ASSERTIVIDADE_ATIVAS", "FAIXAS_ASSERTIVIDADE"} for t in n.targets)]
from practice_band_config import select_band
scope = {"select_band": select_band}
exec(compile(ast.Module(body=assigns + nodes, type_ignores=[]), "<isolated>", "exec"), scope)

class BandTests(unittest.TestCase):
    def setUp(self):
        scope["FAIXAS_ASSERTIVIDADE_ATIVAS"] = True

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

    def test_authenticated_bands_override_default_only_when_enabled(self):
        remote = (True, ((0.0, 101.0, 1, 3.0),))
        self.assertEqual(scope["gestao_sincronizada_practice"](0, 0, remote), (1, 3.0))
        self.assertEqual(scope["gestao_sincronizada_practice"](0, 0, None), (4, 2.0))

if __name__ == "__main__":
    unittest.main()

