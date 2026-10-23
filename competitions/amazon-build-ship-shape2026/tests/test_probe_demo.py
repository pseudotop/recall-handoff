"""Portable probe arguments; no browser, public API or model is launched."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("probe_demo", Path(__file__).parents[1] / "preparation/probe_demo.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ProbeArgumentTests(unittest.TestCase):
    def test_default_browser_is_playwright_managed(self):
        args = module.parse_args(["--output", ".local/probe"])
        self.assertIsNone(args.browser_executable)

    def test_explicit_browser_preserves_non_ascii_and_spaces(self):
        path = str(Path("브라우저 경로") / "chrome.exe")
        args = module.parse_args(["--output", ".local/probe", "--browser-executable", path])
        self.assertEqual(str(args.browser_executable), path)


if __name__ == "__main__": unittest.main()
