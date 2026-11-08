import threading
import os
import unittest
from unittest import mock

import robo


class MonitorWorkerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.original_worker = robo.worker
        self.original_thread = robo._worker_thread
        self.finished = threading.Event()
        self.started = threading.Event()

        def fake_worker():
            self.started.set()
            self.finished.wait(timeout=5)

        robo.worker = fake_worker
        robo._worker_thread = None

    def tearDown(self):
        self.finished.set()
        thread = robo._worker_thread
        if thread is not None:
            thread.join(timeout=1)
        robo.worker = self.original_worker
        robo._worker_thread = self.original_thread

    def test_worker_is_started_once_and_stays_in_calling_process(self):
        with mock.patch.object(robo.log, "info") as info:
            first = robo.iniciar_worker_em_processo_web()
            self.assertTrue(self.started.wait(timeout=1))
            second = robo.iniciar_worker_em_processo_web()

        self.assertIs(first, second)
        self.assertTrue(first.is_alive())
        self.assertEqual(first.name, "worker-deriv-demo")
        info.assert_called_once()

    def test_import_does_not_start_trading_worker(self):
        self.assertIsNone(self.original_thread)

    @mock.patch.dict(os.environ, {"MONITOR_API_TOKEN": "local-test"})
    def test_monitor_api_reads_the_live_process_counters(self):
        with robo.estado_lock:
            original = (robo.stats["wins"], robo.stats["losses"])
            robo.stats["wins"], robo.stats["losses"] = 3, 2

        try:
            response = robo.app.test_client().get(
                "/api/v1/monitor",
                headers={"Authorization": "Bearer local-test"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["estatisticas"]["wins"], 3)
            self.assertEqual(response.get_json()["estatisticas"]["losses"], 2)
        finally:
            with robo.estado_lock:
                robo.stats["wins"], robo.stats["losses"] = original


if __name__ == "__main__":
    unittest.main()
