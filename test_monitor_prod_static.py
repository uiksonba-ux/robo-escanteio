"""Static safety checks: never import robo.py because import starts trading worker."""
import ast
import pathlib
import unittest

SOURCE = pathlib.Path(__file__).with_name("robo.py").read_text(encoding="utf-8")

class ProductionMonitorSafety(unittest.TestCase):
    def test_python_syntax(self):
        ast.parse(SOURCE)

    def test_endpoint_is_read_only_and_authenticated(self):
        tree = ast.parse(SOURCE)
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "monitor_practice")
        self.assertIn('MONITOR_API_TOKEN', ast.get_source_segment(SOURCE, fn))
        self.assertIn('hmac.compare_digest', ast.get_source_segment(SOURCE, fn))
        self.assertIn('estado_lock', ast.get_source_segment(SOURCE, fn))
        self.assertIn('"PRACTICE"', ast.get_source_segment(SOURCE, fn))
        self.assertIn('"somente_leitura"', ast.get_source_segment(SOURCE, fn))
        self.assertNotIn('api.buy', ast.get_source_segment(SOURCE, fn))
        self.assertNotIn('send_telegram', ast.get_source_segment(SOURCE, fn))

if __name__ == "__main__":
    unittest.main()
