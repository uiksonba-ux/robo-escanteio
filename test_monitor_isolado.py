"""Tests for isolated monitor API; no IQ credentials or trading imports."""
import os
import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
