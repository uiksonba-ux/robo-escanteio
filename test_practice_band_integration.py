"""Pure unit tests; do not import robo.py because it starts the trading worker."""
import unittest
from unittest.mock import patch, MagicMock
import practice_band_config as cfg

BASE = {"schema_version": 1, "conta": "PRACTICE", "enabled": True,
        "aplicada_no_robo": False, "persistencia": "memoria_temporaria",
        "faixas": [{"min": a, "max": b, "gales": g, "mult": 2.0}
                   for a,b,g in [(0,49,4),(50,59,3),(60,69,2),(70,79,1),(80,100,0)]]}

class PracticeBandIntegrationTests(unittest.TestCase):
    def test_boundaries(self):
        validated = cfg.validate_config(BASE)
        self.assertIsNotNone(validated)
        for wins,losses,expected in [(0,0,4),(49,51,4),(50,50,3),
                                     (59,41,3),(60,40,2),(69,31,2),
                                     (70,30,1),(79,21,1),(80,20,0),(100,0,0)]:
            self.assertEqual(cfg.select_band(wins,losses,validated,(2,2.0)),(expected,2.0))
    def test_disabled_falls_back(self):
        data = dict(BASE, enabled=False)
        self.assertEqual(cfg.select_band(0,0,cfg.validate_config(data),(2,2.0)),(2,2.0))
    def test_invalid_or_real_rejected(self):
        for changed in [dict(BASE, conta="REAL"), dict(BASE, persistencia="duravel"),
                        dict(BASE, aplicada_no_robo=True), dict(BASE, faixas=[])]:
            self.assertIsNone(cfg.validate_config(changed))
        bad = dict(BASE, faixas=[dict(x) for x in BASE["faixas"]])
        bad["faixas"][0]["mult"] = float("nan")
        self.assertIsNone(cfg.validate_config(bad))
    def test_no_network_without_configuration(self):
        with patch.object(cfg, "urlopen") as network:
            self.assertIsNone(cfg.read_remote_config("", ""))
            self.assertIsNone(cfg.read_remote_config("https://evil.example/api/v1/practice/faixas", "secret"))
            self.assertIsNone(cfg.read_remote_config("https://baskaraido-monitor-test.onrender.com:8443/api/v1/practice/faixas", "secret"))
            network.assert_not_called()
    def test_authenticated_read(self):
        import json
        response = MagicMock()
        response.status = 200
        response.read.return_value = json.dumps(BASE).encode()
        response.__enter__.return_value = response
        with patch.object(cfg, "urlopen", return_value=response) as fetch:
            result = cfg.read_remote_config("https://baskaraido-monitor-test.onrender.com/api/v1/practice/faixas","test-token")
            self.assertIsNotNone(result)
            req = fetch.call_args.args[0]
            self.assertEqual(req.get_header("Authorization"), "Bearer test-token")
            self.assertEqual(req.get_method(), "GET")
    def test_order_snapshot(self):
        import pathlib
        src = pathlib.Path("robo.py").read_text()
        self.assertIn("if nivel == 0:\\n        config_remota = read_remote_config(", src)
        self.assertIn('"multiplicador": gestao_sincronizada_practice(', src)
        self.assertIn('contexto.get("multiplicador", MULTIPLICADOR_GALE)', src)

if __name__ == "__main__":
    unittest.main()
