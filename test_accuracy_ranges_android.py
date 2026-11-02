"""Static regression tests: do not run or import the trading robot."""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).parent
ACTIVITY = (ROOT / "android-app/app/src/main/java/com/baskaraido/trader/MainActivity.kt").read_text()
BANDS = (ROOT / "android-app/app/src/main/java/com/baskaraido/trader/AccuracyRanges.kt").read_text()

class AccuracyRangesTests(unittest.TestCase):
    def test_five_bands_cover_percentages(self):
        self.assertEqual(re.findall(r'accuracy < (\d+)\.0 -> (\d+)', BANDS), [('50','0'),('60','1'),('70','2'),('80','3')])
        self.assertIn('else -> 4', BANDS)
        self.assertIn('accuracy > 100.0', BANDS)
        self.assertIn('!accuracy.isFinite()', BANDS)

    def test_defaults(self):
        self.assertIn('intArrayOf(4, 3, 2, 1, 0)', ACTIVITY)
        self.assertIn('intArrayOf(0, 50, 60, 70, 80)', ACTIVITY)
        self.assertIn('intArrayOf(49, 59, 69, 79, 100)', ACTIVITY)
        self.assertIn('getString("mult_$i", "2")', ACTIVITY)

    def test_authenticated_sync_is_practice_only(self):
        self.assertIn('getSharedPreferences("accuracy_ranges", MODE_PRIVATE)', ACTIVITY)
        self.assertIn('currentAccuracy = if (robotData && !snapshot.stale)', ACTIVITY)
        self.assertIn('PracticeProtocol.address(base,', ACTIVITY)
        self.assertIn('instanceFollowRedirects = false', ACTIVITY)
        self.assertIn('isSaveEnabled = false', ACTIVITY)
        self.assertIn('override fun onStop()', ACTIVITY)
        self.assertNotIn('HttpURLConnection', BANDS)
        self.assertNotIn('robo.py', BANDS)

if __name__ == "__main__":
    unittest.main()

