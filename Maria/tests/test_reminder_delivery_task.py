"""Pin the tracked Task Scheduler template for reminder delivery (V1-23-R1).

The owner registers MariaReminderDelivery from this template, so its
settings are the approved ones exactly: a per-user, least-privilege task
that runs only while the owner is signed in, once a minute, never wakes the
computer, never overlaps its own scheduled runs, and has no time limit
(decision D1).
The test reads the file only; nothing is registered.
"""

from __future__ import annotations

import unittest
import xml.etree.ElementTree as ElementTree

from core.constants import PROJECT_ROOT

TEMPLATE_PATH = PROJECT_ROOT / "docs" / "reminder-delivery-task.xml"
NAMESPACE = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"


def _local(tag: str) -> str:
    """Return an element's tag without the task namespace."""
    return tag.removeprefix(NAMESPACE)


class ReminderDeliveryTaskTemplateTests(unittest.TestCase):
    """Every element of the template, and nothing beyond it."""

    def setUp(self) -> None:
        self.raw = TEMPLATE_PATH.read_bytes()
        # Git may check the file out with either line ending; Task
        # Scheduler accepts both, so only the text is pinned.
        self.text = self.raw.decode("ascii").replace("\r\n", "\n")
        self.task = ElementTree.fromstring(self.text)

    def child(self, parent: ElementTree.Element, name: str) -> ElementTree.Element:
        """Return the one child of that name, failing if there is not one."""
        (found,) = [item for item in parent if _local(item.tag) == name]
        return found

    def children(self, parent: ElementTree.Element) -> dict[str, str]:
        """Return every child's name and text, so extra elements show."""
        return {_local(item.tag): (item.text or "").strip() for item in parent}

    def test_it_is_ascii_without_a_byte_order_mark(self) -> None:
        self.assertFalse(self.raw.startswith(b"\xef\xbb\xbf"))
        self.assertTrue(self.raw.isascii())
        self.assertEqual(self.task.get("version"), "1.2")
        self.assertEqual(
            [_local(item.tag) for item in self.task],
            ["RegistrationInfo", "Triggers", "Principals", "Settings", "Actions"],
        )

    def test_one_time_trigger_repeats_every_minute_without_end(self) -> None:
        triggers = self.child(self.task, "Triggers")
        (trigger,) = list(triggers)

        self.assertEqual(_local(trigger.tag), "TimeTrigger")
        self.assertEqual(
            [_local(item.tag) for item in trigger],
            ["StartBoundary", "Enabled", "Repetition"],
        )
        self.assertEqual(self.child(trigger, "Enabled").text, "true")
        self.assertEqual(
            self.children(self.child(trigger, "Repetition")),
            {"Interval": "PT1M"},
        )

    def test_it_runs_only_as_the_signed_in_owner_with_least_privilege(
        self,
    ) -> None:
        (principal,) = list(self.child(self.task, "Principals"))

        self.assertEqual(
            self.children(principal),
            {"LogonType": "InteractiveToken", "RunLevel": "LeastPrivilege"},
        )

    def test_the_settings_are_exactly_the_approved_ones(self) -> None:
        self.assertEqual(
            self.children(self.child(self.task, "Settings")),
            {
                "MultipleInstancesPolicy": "IgnoreNew",
                "DisallowStartIfOnBatteries": "false",
                "StopIfGoingOnBatteries": "false",
                "StartWhenAvailable": "true",
                "RunOnlyIfNetworkAvailable": "false",
                "RunOnlyIfIdle": "false",
                "WakeToRun": "false",
                "AllowStartOnDemand": "true",
                "Enabled": "true",
                "Hidden": "false",
                "ExecutionTimeLimit": "PT0S",
            },
        )

    def test_its_one_action_runs_the_pass_with_placeholders_only(self) -> None:
        actions = self.child(self.task, "Actions")
        (action,) = list(actions)

        self.assertEqual(_local(action.tag), "Exec")
        self.assertEqual(
            self.children(action),
            {
                "Command": "PYTHONW_EXE",
                "Arguments": '"MARIA_DIR\\deliver_reminders.py"',
                "WorkingDirectory": "MARIA_DIR",
            },
        )

    def test_it_names_no_machine_account_or_password(self) -> None:
        for forbidden in (":\\", "\\Users\\", "S-1-5-", "UserId", "Password"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.text)
        description = self.child(
            self.child(self.task, "RegistrationInfo"),
            "Description",
        ).text
        # Both removal commands name the task, so each works as written.
        self.assertIn("schtasks /end /tn MariaReminderDelivery,", description)
        self.assertIn("schtasks /delete /tn MariaReminderDelivery /f", description)

    def test_its_description_start_and_attributes_are_exactly_these(self) -> None:
        self.assertEqual(
            self.children(self.child(self.task, "RegistrationInfo")),
            {
                "Description": (
                    "Maria reminder delivery: one short pass each minute while "
                    "you are signed in. Deleting this task may need an "
                    "administrator Command Prompt. Remove with schtasks /end "
                    "/tn MariaReminderDelivery, then schtasks /delete /tn "
                    "MariaReminderDelivery /f"
                ),
            },
        )
        trigger = self.child(self.child(self.task, "Triggers"), "TimeTrigger")
        self.assertEqual(
            self.child(trigger, "StartBoundary").text,
            "2026-01-01T00:00:00",
        )
        self.assertEqual(
            {
                _local(item.tag): item.attrib
                for item in self.task.iter()
                if item.attrib
            },
            {
                "Task": {"version": "1.2"},
                "Principal": {"id": "Author"},
                "Actions": {"Context": "Author"},
            },
        )


if __name__ == "__main__":
    unittest.main()
