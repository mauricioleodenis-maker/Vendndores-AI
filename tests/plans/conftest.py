from __future__ import annotations

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.plans import catalog


@pytest_asyncio.fixture
async def seeded(session: AsyncSession) -> None:
    await catalog.seed(session)
    await session.commit()
