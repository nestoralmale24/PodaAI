"""Claude via the official CLI: configuration and message handling (no subprocess, no network)."""
import unittest

from poda.config import resolve_llm
from poda.providers import claude_cli


class ClaudeProviderTests(unittest.TestCase):
    def test_resolve_llm(self):
        llm = resolve_llm({"LLM_PROVIDER": "claude", "CLAUDE_MODEL": "sonnet"})
        self.assertEqual((llm.provider, llm.model, llm.triage_model), ("claude", "claude/sonnet", "claude/sonnet"))
        self.assertIsNone(llm.api_key)
        self.assertEqual(resolve_llm({"LLM_PROVIDER": "claude", "CLAUDE_MODEL": "opus"}, cli_triage="haiku").triage_model,
                         "claude/haiku")

    def test_prompt_splits_system_from_conversation(self):
        system, prompt = claude_cli._prompt([{"role": "system", "content": "S"}, {"role": "user", "content": "U"}])
        self.assertEqual((system, prompt), ("S", "U"))

    def test_errors_are_classified(self):
        self.assertIsInstance(claude_cli._classify("Not logged in · Please run /login"), claude_cli.AuthenticationError)
        self.assertIsInstance(claude_cli._classify("5-hour usage limit reached"), claude_cli.BudgetExceededError)
        self.assertNotIsInstance(claude_cli._classify("boom"), (claude_cli.AuthenticationError, claude_cli.BudgetExceededError))

    def test_login_url_is_found_with_and_without_terminal_links(self):
        url = "https://claude.ai/oauth/authorize?abc"
        esc = chr(27)
        osc = "visit:" + chr(10) + esc + "]8;;" + url + esc + chr(92) + url + esc + "]8;;" + esc + chr(92) + chr(10) + "Paste code > "
        self.assertEqual(claude_cli._login_url(osc), url)
        self.assertEqual(claude_cli._login_url("visit " + url + chr(10)), url)
        self.assertEqual(claude_cli._login_url("nothing yet"), "")

    def test_model_labels(self):
        self.assertEqual(claude_cli._pretty("claude-sonnet-5-5"), "Claude Sonnet 5.5")
        self.assertEqual(claude_cli._pretty("claude-opus-5"), "Claude Opus 5")
        self.assertEqual(claude_cli._pretty("weird"), "weird")


if __name__ == "__main__":
    unittest.main()
