import os
import tempfile
import unittest

from accuracy_store import AccuracyStore, gales_for_accuracy, weighted_accuracy


class AccuracyPersistenceTests(unittest.TestCase):
    def test_prior_formula_and_band_boundaries(self):
        self.assertEqual(weighted_accuracy(0, 0), 80.0)
        self.assertEqual(gales_for_accuracy(weighted_accuracy(0, 0)), 0)
        self.assertEqual(weighted_accuracy(1, 0), 80.95)
        self.assertEqual(weighted_accuracy(0, 1), 76.19)

        expected = [
            (71.99, 5),
            (72.0, 4),
            (73.99, 4),
            (74.0, 3),
            (75.99, 3),
            (76.0, 2),
            (77.99, 2),
            (78.0, 1),
            (79.99, 1),
            (80.0, 0),
        ]
        for accuracy, gales in expected:
            with self.subTest(accuracy=accuracy):
                self.assertEqual(gales_for_accuracy(accuracy), gales)

    def test_per_bank_results_persist_and_duplicate_events_are_ignored(self):
        with tempfile.TemporaryDirectory() as tempdir:
            database_url = "sqlite:///" + os.path.join(tempdir, "accuracy.sqlite3")
            store = AccuracyStore(database_url)
            store.initialize()

            self.assertEqual(
                store.record_settlement(
                    "BANCA 1", "order-1", "operation-1", "loss", 0, None
                ),
                (True, False),
            )
            self.assertEqual(
                store.record_settlement(
                    "BANCA 1", "order-2", "operation-1", "win", 1, "win"
                ),
                (True, True),
            )
            self.assertEqual(
                store.record_settlement(
                    "BANCA 1", "order-2", "operation-1", "win", 1, "win"
                ),
                (False, False),
            )
            self.assertEqual(
                store.record_settlement(
                    "BANCA 2", "order-3", "operation-3", "loss", 0, "loss"
                ),
                (True, True),
            )

            reloaded = AccuracyStore(database_url)
            reloaded.initialize()
            self.assertEqual(
                reloaded.record_settlement(
                    "BANCA 1", "order-2", "operation-1", "win", 1, "win"
                ),
                (False, False),
            )
            counts = reloaded.load_counts(["BANCA 1", "BANCA 2", "BANCA 3"])

            self.assertEqual(counts["BANCA 1"]["wins"], 1)
            self.assertEqual(counts["BANCA 1"]["losses"], 0)
            self.assertEqual(counts["BANCA 1"]["wins_by_level"][1], 1)
            self.assertEqual(counts["BANCA 2"]["wins"], 0)
            self.assertEqual(counts["BANCA 2"]["losses"], 1)
            self.assertEqual(counts["BANCA 3"]["wins"], 0)
            self.assertEqual(counts["BANCA 3"]["losses"], 0)
            self.assertEqual(weighted_accuracy(counts["BANCA 1"]["wins"],
                                               counts["BANCA 1"]["losses"]), 80.95)
            self.assertEqual(weighted_accuracy(counts["BANCA 2"]["wins"],
                                               counts["BANCA 2"]["losses"]), 76.19)


if __name__ == "__main__":
    unittest.main()
