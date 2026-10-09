import pytest
from sqlalchemy import select

from app import cli
from app.core.crypto import generate_master_key
from app.db.models.plans import Offer, Plan
from app.db.models.users import User
from tests.conftest import TEST_PASSWORD


async def test_seed_is_idempotent_and_matches_maestro(session):
    first = await cli.seed_data(session)
    second = await cli.seed_data(session)
    assert first == {"plans": 3, "offers": 1} and second == {"plans": 0, "offers": 0}
    plans = {p.code: p for p in (await session.execute(select(Plan))).scalars()}
    assert (plans["basico"].setup_fee_cop, plans["basico"].monthly_fee_cop) == (800_000, 250_000)
    assert (plans["pro"].setup_fee_cop, plans["pro"].monthly_fee_cop) == (1_200_000, 390_000)
    assert (plans["premium"].setup_fee_cop, plans["premium"].monthly_fee_cop) == (
        2_000_000,
        600_000,
    )
    assert plans["pro"].limits["max_conversations_month"] == 1500
    assert (
        plans["premium"].limits["voice_enabled"] is True
        and plans["basico"].limits["max_calendars"] == 1
    )
    offer = (await session.execute(select(Offer))).scalar_one()
    assert (offer.code, offer.value, offer.max_redemptions) == ("fundador", 100, 5)


async def test_seed_updates_changed_plan(session):
    await cli.seed_data(session)
    plan = (await session.execute(select(Plan).where(Plan.code == "pro"))).scalar_one()
    plan.monthly_fee_cop = 1
    await session.flush()
    await cli.seed_data(session)
    assert plan.monthly_fee_cop == 390_000


async def test_create_owner_and_duplicate(engine):
    msg = await cli._create_owner("Jefe@Example.com", TEST_PASSWORD, "Jefe")
    assert "Owner creado" in msg
    assert "ya existe" in await cli._create_owner("jefe@example.com", TEST_PASSWORD, "Jefe")
    from app.db.session import session_scope

    async with session_scope() as s:
        user = (await s.execute(select(User))).scalar_one()
        assert user.role == "owner" and user.email == "jefe@example.com"


def test_parser_create_owner():
    assert (
        cli.build_parser().parse_args(["create-owner", "--email", "a@b.co"]).command
        == "create-owner"
    )


def test_gen_key_and_parser(capsys):
    assert cli.main(["gen-key"]) == 0
    assert len(capsys.readouterr().out.strip()) >= 43
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


async def test_init_db_and_rotate_keys(engine):
    assert "Tablas creadas" in await cli._init_db()
    assert "re-cifrados: 0" in await cli._rotate_keys()


async def test_rotate_keys_reencrypts(engine, session, tenant, monkeypatch):
    import json

    from app.core import crypto
    from app.core.config import get_settings
    from app.db.models.tenants import TenantSecret

    old = crypto.build_crypto(get_settings())  # clave "dev"
    aad = crypto.make_aad("tenant_secrets", tenant.id, "ciphertext")
    blob = old.encrypt(b"auth-token", aad=aad)
    session.add(TenantSecret(tenant_id=tenant.id, kind="twilio_auth_token", ciphertext=blob.ciphertext,
                             nonce=blob.nonce, key_version=blob.key_version, last4="oken"))  # fmt: skip
    await session.commit()
    new_key = generate_master_key()
    monkeypatch.setenv("VAI_MASTER_KEYS", json.dumps({"v2": new_key}))
    get_settings.cache_clear()
    crypto.get_crypto.cache_clear()
    # la clave 'dev' ya no esta en el llavero -> sin ella no se puede rotar: simula llavero con ambas
    keys = {"v2": crypto._b64key(new_key), "dev": old._keys["dev"]}
    monkeypatch.setattr(crypto, "get_crypto", lambda: crypto.EnvelopeCrypto(keys, "v2"))
    assert "re-cifrados: 1" in await cli._rotate_keys()
    await session.refresh(row := (await session.execute(select(TenantSecret))).scalar_one())
    assert row.key_version == "v2"


def test_worker_settings_discovers_registered_jobs():
    from app.core.jobs import JOB_REGISTRY, register_job

    @register_job("worker.sample")
    async def sample(ctx):
        return 1

    import app.worker as worker

    assert "worker.sample" in JOB_REGISTRY
    assert worker.WorkerSettings.max_jobs > 0
    assert worker.load_job_modules() == [] or isinstance(worker.load_job_modules(), list)


async def test_worker_startup_shutdown(monkeypatch):
    import app.worker as worker
    from app.db import session as db_session

    await worker.startup({})
    assert db_session._engine is not None
    await worker.shutdown({})
    assert db_session._engine is None
