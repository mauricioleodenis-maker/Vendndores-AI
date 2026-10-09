from app.core.deps import session_cookie_name


async def test_get_session_commits_and_rolls_back(engine):
    from sqlalchemy import select

    from app.core.deps import get_session
    from app.db.models.tenants import Tenant

    gen = get_session()
    s = await gen.__anext__()
    s.add(Tenant(slug="a", name="A"))
    try:
        await gen.__anext__()
    except StopAsyncIteration:
        pass
    gen2 = get_session()
    s2 = await gen2.__anext__()
    assert (await s2.execute(select(Tenant))).scalars().all()[0].slug == "a"
    s2.add(Tenant(slug="b", name="B"))
    try:
        await gen2.athrow(RuntimeError("fallo"))
    except RuntimeError:
        pass
    gen3 = get_session()
    s3 = await gen3.__anext__()
    assert len((await s3.execute(select(Tenant))).scalars().all()) == 1
    await gen3.aclose()


def test_cookie_name_dev():
    assert session_cookie_name() == "vai_session"


def test_cookie_name_prod_prefix(monkeypatch):
    monkeypatch.setenv("VAI_COOKIE_SECURE", "true")
    from app.core.config import get_settings

    get_settings.cache_clear()
    assert session_cookie_name() == "__Host-vai_session"
