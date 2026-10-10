from __future__ import annotations

import unittest

from tool_contract import available_modes, tool_description


class ToolContractTests(unittest.TestCase):
    def test_modes_follow_registered_services(self) -> None:
        cases = [
            ((), (), ()),
            (("google_lens",), (), ("auto", "source")),
            ((), ("trace_moe",), ("anime",)),
            (("saucenao",), ("trace_moe",), ("auto", "source", "anime")),
        ]
        for source, anime, expected in cases:
            with self.subTest(source=source, anime=anime):
                self.assertEqual(available_modes(source, anime), expected)

    def test_only_enabled_names_are_described(self) -> None:
        names = ("google_lens", "saucenao", "ascii2d", "trace_moe")
        for name in names:
            source = () if name == "trace_moe" else (name,)
            anime = (name,) if name == "trace_moe" else ()
            with self.subTest(name=name):
                description = tool_description(source, anime)
                self.assertIn(name, description)
                for disabled in set(names) - {name}:
                    self.assertNotIn(disabled, description)

    def test_automatic_followup_requires_both_modes(self) -> None:
        self.assertNotIn("may add anime", tool_description(("google_lens",), ()))
        self.assertNotIn("may add anime", tool_description((), ("trace_moe",)))
        self.assertIn("may add anime", tool_description(("saucenao",), ("trace_moe",)))

    def test_enabled_does_not_claim_available(self) -> None:
        self.assertIn(
            "does not guarantee service availability",
            tool_description(("ascii2d",), ()),
        )

    def test_guard_and_direct_output_contract_remain(self) -> None:
        description = tool_description(("google_lens",), ())
        self.assertIn("explicit user request", description)
        self.assertIn("attached or replied", description)
        self.assertIn("sends formatted results directly", description)
