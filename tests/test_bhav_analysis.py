import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from trade_recommendations import run_bhav_analysis


class BhavAnalysisIsolationTests(unittest.TestCase):
    @patch("trade_recommendations.subprocess.run")
    def test_success_runs_separate_process(self, run):
        run.return_value = Mock(returncode=0, stdout="done", stderr="")
        self.assertTrue(run_bhav_analysis())
        self.assertEqual(run.call_args.args[0][1:], ["-m", "bhavAnalysis"])
        self.assertEqual(run.call_args.kwargs["cwd"], Path(__file__).resolve().parent.parent)
        self.assertEqual(run.call_args.kwargs["timeout"], 120)

    @patch("trade_recommendations.subprocess.run")
    def test_module_errors_do_not_escape(self, run):
        # Includes an import/syntax error or SystemExit in the child process.
        run.return_value = Mock(returncode=1, stdout="", stderr="SyntaxError")
        with self.assertLogs(level="ERROR"):
            self.assertFalse(run_bhav_analysis())

    @patch("trade_recommendations.subprocess.run")
    def test_timeout_and_launch_errors_do_not_escape(self, run):
        for error in (subprocess.TimeoutExpired("bhavAnalysis", 120),
                      OSError("Cannot start Python"), RuntimeError("Unexpected failure")):
            with self.subTest(error=error), self.assertLogs(level="ERROR"):
                run.side_effect = error
                self.assertFalse(run_bhav_analysis())


if __name__ == "__main__":
    unittest.main()
