"""The same argument routes serve python -m and the standalone executable."""

from contextlib import redirect_stdout
import io
import unittest
from unittest.mock import patch

from beancount_budget_tool.console import command
from beancount_budget_tool.launcher import COMMANDS, main


class LauncherTests(unittest.TestCase):
    def test_every_command_reaches_its_real_parser(self):
        for name in COMMANDS:
            with self.subTest(command=name), redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as caught:
                    main([name, "--help"])
                self.assertEqual(caught.exception.code, 0)
                self.assertIn("usage:", output.getvalue())

    def test_monthly_flags_keep_the_existing_module_interface(self):
        with patch("beancount_budget_tool.v6.monthly_report.main", return_value=2) as report:
            self.assertEqual(main(["--config", "月报 空格.yaml", "--format", "tui"]), 2)
        report.assert_called_once_with(["--config", "月报 空格.yaml", "--format", "tui"])

    def test_help_lists_workflows_without_starting_an_audit(self):
        with redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as caught:
                main([])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("history", output.getvalue())
        self.assertNotIn("next-month", output.getvalue())

    def test_saved_instructions_use_the_frozen_binary_and_powershell_call_operator(self):
        binary = r"C:\预算 工具\beancount-budget-tool.exe"
        with patch("sys.frozen", True, create=True), patch("sys.executable", binary), patch("sys.platform", "win32"):
            self.assertEqual(command("beancount-budget-tool"), f'& "{binary}"')
            self.assertEqual(command("beancount-budget-configure"), f'& "{binary}" configure')

    def test_installed_commands_are_unchanged(self):
        with patch("sys.frozen", False, create=True):
            self.assertEqual(command("beancount-budget-configure"), "beancount-budget-configure")


if __name__ == "__main__":
    unittest.main()
