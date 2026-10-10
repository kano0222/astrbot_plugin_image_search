from __future__ import annotations

import unittest
from dataclasses import dataclass

from core.models import AnimeMatch, ImageAsset, ProviderOutcome, SearchMode
from core.service import SearchService


@dataclass
class FakeProvider:
    name: str
    outcome: ProviderOutcome

    async def search(
        self,
        image: ImageAsset,
        mode: SearchMode,
    ) -> ProviderOutcome:
        return self.outcome


class AutomaticFollowupTests(unittest.IsolatedAsyncioTestCase):
    async def test_signal_without_source_match_still_runs_followup(self) -> None:
        source = FakeProvider(
            "source",
            ProviderOutcome(suggests_anime=True),
        )
        followup = FakeProvider(
            "followup",
            ProviderOutcome(matches=[AnimeMatch(provider="followup", title="show")]),
        )
        service = SearchService(
            source_providers=[source],
            anime_followup_providers=[followup],
        )

        response = await service.search(
            ImageAsset(url="https://example.com/image.jpg"),
            SearchMode.AUTO,
        )

        self.assertEqual([match.title for match in response.matches], ["show"])


if __name__ == "__main__":
    unittest.main()
