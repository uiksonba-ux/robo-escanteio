"""Isolated endpoint tests; never import robo.py or start a trading worker."""
import os
import unittest
from unittest.mock import patch
import monitor_test_app as service

class BandSyncTests(unittest.TestCase):
    def setUp(self):
        self.client = service.app.test_client()
        self.headers = {"Authorization": "Bearer test-only-secret"}
        self.payload = {"schema_version": 1, "conta": "PRACTICE", "enabled": True,
                        "faixas": [{"min": a, "max": b, "gales": g, "mult": 2.0}
                                  for a,b,g in [(0,49,4),(50,59,3),(60,69,2),(70,79,1),(80,100,0)]]}
        self.patch = patch.dict(os.environ, {"MONITOR_API_TOKEN": "test-only-secret"})
        self.patch.start()
    def tearDown(self):
        self.patch.stop()
    def test_auth(self):
        self.assertEqual(self.client.put("/api/v1/practice/faixas", json=self.payload).status_code, 401)
    def test_put_and_get_not_applied(self):
        res = self.client.put("/api/v1/practice/faixas", headers=self.headers, json=self.payload)
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.json["aplicada_no_robo"])
        get = self.client.get("/api/v1/practice/faixas", headers=self.headers)
        self.assertEqual(get.json["faixas"], self.payload["faixas"])
        self.assertEqual(get.json["persistencia"], "memoria_temporaria")
    def test_reject_real_and_invalid_ranges(self):
        payload = {**self.payload, "conta": "REAL"}
        self.assertEqual(self.client.put("/api/v1/practice/faixas", headers=self.headers, json=payload).status_code, 400)
        payload = {**self.payload, "faixas": self.payload["faixas"][:-1]}
        self.assertEqual(self.client.put("/api/v1/practice/faixas", headers=self.headers, json=payload).status_code, 400)
    def test_no_trade_worker_import(self):
        import pathlib
        src = pathlib.Path(service.__file__).read_text()
        self.assertNotIn("import robo", src)
        self.assertNotIn("api.buy", src)

if __name__ == "__main__":
    unittest.main()
