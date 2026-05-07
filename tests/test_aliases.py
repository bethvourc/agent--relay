"""Tests for agent alias resolution and CLI arg parsing."""

from __future__ import annotations

import argparse
from unittest import TestCase

from agent_relay.agents import AGENT_ALIASES, AGENT_REGISTRY, resolve_agent_key


class ResolveAgentKeyTests(TestCase):
    def test_full_key_passes_through(self) -> None:
        self.assertEqual(resolve_agent_key("claude"), "claude")
        self.assertEqual(resolve_agent_key("codex"), "codex")

    def test_alias_resolves(self) -> None:
        self.assertEqual(resolve_agent_key("c"), "claude")
        self.assertEqual(resolve_agent_key("x"), "codex")

    def test_unknown_raises(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            resolve_agent_key("zz")
        self.assertIn("Unknown agent", str(ctx.exception))
        self.assertIn("claude (c)", str(ctx.exception))
        self.assertIn("codex (x)", str(ctx.exception))


class AliasRegistryTests(TestCase):
    def test_all_agents_have_aliases(self) -> None:
        for key, adapter in AGENT_REGISTRY.items():
            self.assertTrue(adapter.alias, f"{key} missing alias")

    def test_aliases_are_unique(self) -> None:
        aliases = [a.alias for a in AGENT_REGISTRY.values()]
        self.assertEqual(len(aliases), len(set(aliases)), "Duplicate aliases found")

    def test_alias_map_matches_registry(self) -> None:
        for key, adapter in AGENT_REGISTRY.items():
            self.assertIn(adapter.alias, AGENT_ALIASES)
            self.assertEqual(AGENT_ALIASES[adapter.alias], key)


class ParseAgentsAndTaskTests(TestCase):
    """Test the _parse_agents_and_task helper via argparse simulation."""

    def _make_args(self, args: list[str], task_flag: str | None = None) -> argparse.Namespace:
        import argparse

        ns = argparse.Namespace()
        ns.args = args
        ns.task_flag = task_flag
        return ns

    def test_task_as_last_positional(self) -> None:
        from agent_relay.cli import _parse_agents_and_task

        args = self._make_args(["c", "x", "fix the tests"])
        agents, task = _parse_agents_and_task(args)
        self.assertEqual(agents, ["claude", "codex"])
        self.assertEqual(task, "fix the tests")

    def test_task_via_flag(self) -> None:
        from agent_relay.cli import _parse_agents_and_task

        args = self._make_args(["claude", "codex"], task_flag="fix the tests")
        agents, task = _parse_agents_and_task(args)
        self.assertEqual(agents, ["claude", "codex"])
        self.assertEqual(task, "fix the tests")

    def test_three_agents_with_task(self) -> None:
        from agent_relay.cli import _parse_agents_and_task

        args = self._make_args(["c", "x", "c", "review code"])
        agents, task = _parse_agents_and_task(args)
        self.assertEqual(agents, ["claude", "codex", "claude"])
        self.assertEqual(task, "review code")

    def test_missing_task_raises(self) -> None:
        from agent_relay.cli import _parse_agents_and_task

        # All args are agent keys — no task
        args = self._make_args(["c", "x"])
        with self.assertRaises(SystemExit) as ctx:
            _parse_agents_and_task(args)
        self.assertIn("Missing task", str(ctx.exception))

    def test_too_few_agents_raises(self) -> None:
        from agent_relay.cli import _parse_agents_and_task

        args = self._make_args(["c", "fix tests"])
        with self.assertRaises(SystemExit):
            _parse_agents_and_task(args)

    def test_aliases_resolved_in_positional(self) -> None:
        from agent_relay.cli import _parse_agents_and_task

        args = self._make_args(["x", "c", "do stuff"])
        agents, task = _parse_agents_and_task(args)
        self.assertEqual(agents, ["codex", "claude"])
        self.assertEqual(task, "do stuff")
