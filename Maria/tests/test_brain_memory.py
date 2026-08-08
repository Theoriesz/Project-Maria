"""Integration tests for explicit memory through MariaBrain."""

from __future__ import annotations

import inspect
import logging
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

import core.brain as brain_module
from core.brain import MariaBrain
from core.config import MariaConfig
from core.memory import Memory
from core.permissions import PermissionManager
from core.planner import PlanStep, Planner
from core.tool_manager import ToolManager


class BrainMemoryIntegrationTests(unittest.TestCase):
    """Exercise memory through the normal application component boundaries."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root_path = Path(self.temporary_directory.name)
        self.config = _temporary_config(self.root_path)
        (
            self.brain,
            self.memory,
            self.tool_manager,
            self.permissions,
        ) = _build_components(self.config)

    def test_remember_recall_and_memories_use_normal_component_flow(self) -> None:
        fact = "My editor is VS Code"

        with (
            patch.object(
                self.permissions,
                "check",
                wraps=self.permissions.check,
            ) as permission_check,
            patch.object(
                self.tool_manager,
                "execute_tool",
                wraps=self.tool_manager.execute_tool,
            ) as execute_tool,
            patch.object(
                self.memory,
                "store_memory",
                wraps=self.memory.store_memory,
            ) as store_memory,
            patch.object(
                self.memory,
                "search_memories",
                wraps=self.memory.search_memories,
            ) as search_memories,
            patch.object(
                self.memory,
                "list_memories",
                wraps=self.memory.list_memories,
            ) as list_memories,
            patch.object(
                self.memory,
                "record_interaction",
                wraps=self.memory.record_interaction,
            ) as record_interaction,
        ):
            remember = self.brain.process(f"remember {fact}")
            recall = self.brain.process("recall EDITOR")
            memories = self.brain.process("memories")

        memory_id = self.memory.list_memories()[0]["id"]
        self.assertEqual(remember.text, f"Remembered memory {memory_id}: {fact}")
        self.assertEqual(recall.text, f"{memory_id}: {fact}")
        self.assertEqual(memories.text, f"{memory_id}: {fact}")
        self.assertFalse(remember.needs_confirmation)
        self.assertFalse(recall.needs_confirmation)
        self.assertFalse(memories.needs_confirmation)

        self.assertEqual(
            remember.plan.steps,
            (PlanStep("memory", "store", {"fact": fact}),),
        )
        self.assertEqual(
            recall.plan.steps,
            (PlanStep("memory", "search", {"query": "EDITOR"}),),
        )
        self.assertEqual(
            memories.plan.steps,
            (PlanStep("memory", "list", {}),),
        )

        self.assertEqual(
            permission_check.call_args_list,
            [call("memory", "store"), call("memory", "search"), call("memory", "list")],
        )
        self.assertEqual(
            execute_tool.call_args_list,
            [
                call("memory", "store", {"fact": fact}),
                call("memory", "search", {"query": "EDITOR"}),
                call("memory", "list", {}),
            ],
        )
        store_memory.assert_called_once_with(fact)
        search_memories.assert_called_once_with("EDITOR", limit=20)
        list_memories.assert_called_once_with(limit=20)
        self.assertEqual(record_interaction.call_count, 3)

        interactions = self.memory.get_recent_interactions(limit=10)
        self.assertEqual(
            [interaction["user_input"] for interaction in interactions],
            ["memories", "recall EDITOR", f"remember {fact}"],
        )
        self.assertEqual(
            [interaction["response"] for interaction in interactions],
            [f"{memory_id}: {fact}", f"{memory_id}: {fact}", f"Remembered memory {memory_id}: {fact}"],
        )

    def test_memory_persists_across_new_application_component_set(self) -> None:
        fact = "that my token is MARIA-V01-CLI-01"
        remembered = self.brain.process(f"Remember {fact}")
        memory_id = self.memory.list_memories()[0]["id"]
        self.assertEqual(remembered.text, f"Remembered memory {memory_id}: {fact}")

        new_brain, new_memory, new_tool_manager, new_permissions = _build_components(
            self.config
        )

        self.assertIsNot(new_brain, self.brain)
        self.assertIsNot(new_memory, self.memory)
        self.assertIsNot(new_tool_manager, self.tool_manager)
        self.assertIsNot(new_permissions, self.permissions)
        recalled = new_brain.process("recall TOKEN")
        self.assertEqual(recalled.text, f"{memory_id}: {fact}")
        self.assertEqual(new_memory.list_memories()[0]["fact"], fact)

        interactions = new_memory.get_recent_interactions(limit=10)
        self.assertEqual(
            [interaction["user_input"] for interaction in interactions],
            ["recall TOKEN", f"Remember {fact}"],
        )

    def test_duplicate_remember_uses_original_stored_fact(self) -> None:
        original_fact = "My editor is VS Code"
        duplicate_fact = "MY EDITOR IS VS CODE"

        remembered = self.brain.process(f"remember {original_fact}")
        duplicate = self.brain.process(f"remember {duplicate_fact}")
        memories = self.brain.process("memories")

        stored = self.memory.list_memories()
        self.assertEqual(len(stored), 1)
        memory_id = stored[0]["id"]
        self.assertEqual(stored[0]["fact"], original_fact)
        self.assertEqual(
            remembered.text,
            f"Remembered memory {memory_id}: {original_fact}",
        )
        self.assertEqual(
            duplicate.text,
            f"Already remembered as memory {memory_id}: {original_fact}",
        )
        self.assertEqual(memories.text, f"{memory_id}: {original_fact}")

    def test_brain_contains_no_memory_specific_dispatch_or_sql(self) -> None:
        source = inspect.getsource(brain_module)

        for method_name in (
            "store_memory",
            "get_memory",
            "search_memories",
            "list_memories",
        ):
            with self.subTest(method_name=method_name):
                self.assertNotIn(method_name, source)
        self.assertNotRegex(source, r"(?m)^\s*(?:import|from)\s+sqlite3\b")
        self.assertIsNone(
            re.search(
                r"\b(?:SELECT\s+.+?\s+FROM|INSERT\s+INTO|UPDATE\s+\w+\s+SET|"
                r"DELETE\s+FROM|CREATE\s+TABLE|ALTER\s+TABLE|DROP\s+TABLE)\b",
                source,
                flags=re.IGNORECASE | re.DOTALL,
            )
        )
        self.assertNotRegex(
            source,
            r"(?:tool_name|step\.tool_name)\s*==\s*['\"]memory['\"]",
        )


def _build_components(
    config: MariaConfig,
) -> tuple[MariaBrain, Memory, ToolManager, PermissionManager]:
    """Build a complete Maria component set against a temporary database."""
    memory = Memory(config.database_path, logger=_quiet_logger("brain-memory"))
    memory.initialize()
    tool_manager = ToolManager(
        config=config,
        memory=memory,
        logger=_quiet_logger("brain-tool-manager"),
    )
    tool_manager.load_tools()
    permissions = PermissionManager(
        confirmation_required=config.confirmation_required,
        denied_actions=config.denied_actions,
        logger=_quiet_logger("brain-permissions"),
    )
    brain = MariaBrain(
        config=config,
        planner=Planner(logger=_quiet_logger("brain-planner")),
        memory=memory,
        tool_manager=tool_manager,
        permissions=permissions,
        logger=_quiet_logger("brain"),
    )
    return brain, memory, tool_manager, permissions


def _temporary_config(root_path: Path) -> MariaConfig:
    """Create a MariaConfig whose writable paths are all temporary."""
    return MariaConfig(
        app_name="Maria",
        version="0.1.0",
        environment="test",
        database_path=root_path / "database" / "maria.db",
        log_dir=root_path / "logs",
        prompt_path=root_path / "system_prompt.txt",
        safe_filesystem_root=root_path,
        log_level="CRITICAL",
        confirmation_required=frozenset({"filesystem.rename", "filesystem.move"}),
        denied_actions=frozenset({"filesystem.delete", "filesystem.write"}),
    )


def _quiet_logger(name: str) -> logging.Logger:
    """Return an isolated logger that never writes an application log file."""
    logger = logging.getLogger(f"tests.{name}")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return logger


if __name__ == "__main__":
    unittest.main()
