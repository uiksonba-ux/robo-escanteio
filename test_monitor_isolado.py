"""Tests for isolated monitor API; no IQ credentials or trading imports."""
import os
import unittest
from unittest.mock import patch
import json
from io import BytesIO

from monitor_test_app import app


class MonitorContractTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_health_is_practice(self):
        result = self.client.get("/health")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["conta"], "PRACTICE")

    def test_missing_token_fails_closed(self):
        with patch.dict(os.environ, {"MONITOR_API_TOKEN": ""}):
            self.assertEqual(self.client.get("/api/v1/monitor").status_code, 503)

    def test_wrong_token_is_rejected(self):
        with patch.dict(os.environ, {"MONITOR_API_TOKEN": "test-secret"}):
            response = self.client.get("/api/v1/monitor", headers={"Authorization": "Bearer wrong"})
            self.assertEqual(response.status_code, 401)

    def test_read_only_practice_schema_and_simulated_origin(self):
        with patch.dict(os.environ, {"MONITOR_API_TOKEN": "test-secret"}):
            response = self.client.get("/api/v1/monitor", headers={"Authorization": "Bearer test-secret"})
        self.assertEqual(response.status_code, 200)
        data = response.json
        self.assertEqual(data["conta"], "PRACTICE")
        self.assertEqual(data["modo"], "somente_leitura")
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["origem"], "ambiente_de_teste_sem_dados_reais")
        self.assertEqual(len(data["bancas"]), 10)
        self.assertEqual(data["estatisticas"]["wins"], 0)
        self.assertIsNone(data["bancas"]["BANCA 1"]["saldo"])

    def test_upstream_requires_separate_secret(self):
        with patch.dict(os.environ, {"MONITOR_API_TOKEN": "app-secret", "MONITOR_UPSTREAM_URL": "https://example.com/api/v1/monitor", "MONITOR_UPSTREAM_TOKEN": ""}):
            response = self.client.get("/api/v1/monitor", headers={"Authorization": "Bearer app-secret"})
        self.assertEqual(response.status_code, 503)

    def test_real_upstream_success_and_contract(self):
        data = {"schema_version": 1, "conta": "PRACTICE", "modo": "somente_leitura",
                "origem": "robo_practice_memoria_volatil",
                "estatisticas": {"wins": 4, "losses": 1, "assertividade": 80.0},
                "bancas": {f"BANCA {i}": {"wins": 0, "losses": 0, "saldo": None} for i in range(1, 11)}}
        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit): return json.dumps(data).encode()
        with patch.dict(os.environ, {"MONITOR_API_TOKEN": "app-secret",
                                     "MONITOR_UPSTREAM_URL": "https://example.com/api/v1/monitor",
                                     "MONITOR_UPSTREAM_TOKEN": "upstream-secret"}):
            with patch("monitor_test_app.urlopen", return_value=FakeResponse()) as upstream:
                response = self.client.get("/api/v1/monitor", headers={"Authorization": "Bearer app-secret"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["estatisticas"]["wins"], 4)
        self.assertEqual(upstream.call_args.args[0].get_header("Authorization"), "Bearer upstream-secret")

    def test_real_upstream_rejects_simulated_source(self):
        data = {"schema_version": 1, "conta": "PRACTICE", "modo": "somente_leitura",
                "origem": "ambiente_de_teste_sem_dados_reais",
                "estatisticas": {}, "bancas": {f"BANCA {i}": {} for i in range(1, 11)}}
        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit): return json.dumps(data).encode()
        with patch.dict(os.environ, {"MONITOR_API_TOKEN": "app-secret",
                                     "MONITOR_UPSTREAM_URL": "https://example.com/api/v1/monitor",
                                     "MONITOR_UPSTREAM_TOKEN": "upstream-secret"}):
            with patch("monitor_test_app.urlopen", return_value=FakeResponse()):
                response = self.client.get("/api/v1/monitor", headers={"Authorization": "Bearer app-secret"})
        self.assertEqual(response.status_code, 502)


if __name__ == "__main__":
    unittest.main()
