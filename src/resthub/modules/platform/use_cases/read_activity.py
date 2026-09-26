from __future__ import annotations

from resthub.core.pagination import Page, PageRequest
from resthub.modules.platform.ports.activity_log import PlatformActivityEntry, PlatformActivityLog


class ReadPlatformActivity:
    def __init__(self, activity: PlatformActivityLog) -> None:
        self._activity = activity

    async def __call__(self, page: PageRequest) -> Page[PlatformActivityEntry]:
        return await self._activity.search(page)
