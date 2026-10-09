"""CLI de administracion: ``python -m app.cli <comando>``."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import importlib
import os
import sys
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import create_user, get_user_by_email
from app.core.crypto import generate_master_key
from app.core.errors import AppError
from app.db.models.plans import Offer, Plan
from app.db.session import dispose_engine, session_scope

PLAN_SEED: tuple[dict[str, object], ...] = (
    {
        "code": "basico",
        "name": "Básico",
        "setup_fee_cop": 800_000,
        "monthly_fee_cop": 250_000,
        "sort": 1,
        "limits": {
            "max_conversations_month": 500,
            "max_calendars": 1,
            "followups_enabled": False,
            "voice_enabled": False,
        },
    },
    {
        "code": "pro",
        "name": "Pro",
        "setup_fee_cop": 1_200_000,
        "monthly_fee_cop": 390_000,
        "sort": 2,
        "limits": {
            "max_conversations_month": 1500,
            "max_calendars": 3,
            "followups_enabled": True,
            "voice_enabled": False,
        },
    },
    {
        "code": "premium",
        "name": "Premium",
        "setup_fee_cop": 2_000_000,
        "monthly_fee_cop": 600_000,
        "sort": 3,
        "limits": {
            "max_conversations_month": 4000,
            "max_calendars": 10,
            "followups_enabled": True,
            "voice_enabled": True,
        },
    },
)

FOUNDER_OFFER: dict[str, object] = {
    "code": "fundador",
    "name": "Oferta Fundador",
    "discount_type": "setup_pct",
    "value": 100,
    "max_redemptions": 5,
    "applies_to_plan_codes": ["basico", "pro", "premium"],
    "conditions": "Testimonio en video a cambio de setup gratis (primeros 5 clientes).",
}


async def seed_data(session: AsyncSession) -> dict[str, int]:
    """Siembra planes y oferta Fundador (idempotente, upsert por ``code``)."""
    created = {"plans": 0, "offers": 0}
    for data in PLAN_SEED:
        plan = (
            await session.execute(select(Plan).where(Plan.code == data["code"]))
        ).scalar_one_or_none()
        if plan is None:
            session.add(Plan(**data))  # type: ignore[arg-type]
            created["plans"] += 1
        else:
            for key, value in data.items():
                setattr(plan, key, value)
    offer = (
        await session.execute(select(Offer).where(Offer.code == FOUNDER_OFFER["code"]))
    ).scalar_one_or_none()
    if offer is None:
        session.add(Offer(**FOUNDER_OFFER))  # type: ignore[arg-type]
        created["offers"] += 1
    await session.flush()
    # Hook opcional: modulos de dominio pueden aportar su propia siembra.
    for modname in ("app.plans.catalog", "app.niches.loader"):
        try:
            module = importlib.import_module(modname)
        except ModuleNotFoundError:
            continue
        hook = getattr(module, "seed", None)
        if hook is not None:
            await hook(session)
    from app.outreach.templates import seed_templates

    await seed_templates(session)
    return created


async def _create_owner(email: str, password: str, name: str) -> str:
    async with session_scope() as session:
        if await get_user_by_email(session, email):
            return "El usuario ya existe"
        await create_user(
            session, email=email, password=password, full_name=name, role="owner", actor=None
        )
    return f"Owner creado: {email}"


async def _seed() -> str:
    async with session_scope() as session:
        created = await seed_data(session)
    return f"Seed listo: {created}"


async def _init_db() -> str:
    import app.db.models  # noqa: F401
    from app.db.base import Base
    from app.db.session import get_engine

    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return "Tablas creadas (solo desarrollo; en produccion usa alembic upgrade head)"


async def _rotate_keys() -> str:
    from app.core.crypto import EncryptedBlob, get_crypto, make_aad
    from app.db.models.tenants import TenantSecret

    crypto = get_crypto()
    rotated = 0
    async with session_scope() as session:
        stmt = select(TenantSecret).where(TenantSecret.key_version != crypto.active_version)
        for row in (await session.execute(stmt)).scalars():
            aad = make_aad("tenant_secrets", row.tenant_id, "ciphertext")
            old = EncryptedBlob(row.ciphertext, row.nonce, row.key_version)
            new = crypto.rotate(old, aad=aad)
            row.ciphertext, row.nonce, row.key_version = new.ciphertext, new.nonce, new.key_version
            rotated += 1
    return f"Secretos re-cifrados: {rotated} (otras tablas cifradas las rota su modulo)"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Vendedores AI")
    sub = parser.add_subparsers(dest="command", required=True)
    owner = sub.add_parser("create-owner", help="Crea el primer usuario owner")
    owner.add_argument("--email", required=True)
    owner.add_argument("--name", default="Owner")
    owner.add_argument("--password", help="Si se omite: VAI_OWNER_PASSWORD o se pregunta")
    sub.add_parser("seed", help="Siembra planes y ofertas (idempotente)")
    sub.add_parser("init-db", help="Crea tablas (solo desarrollo/SQLite)")
    sub.add_parser("rotate-keys", help="Re-cifra secretos con la clave maestra activa")
    sub.add_parser("gen-key", help="Genera una clave maestra base64 de 32 bytes")
    return parser


async def _dispatch(args: argparse.Namespace) -> str:
    try:
        if args.command == "create-owner":
            password = (
                args.password
                or os.environ.get("VAI_OWNER_PASSWORD")
                or getpass.getpass("Contraseña (min 12): ")
            )
            return await _create_owner(args.email, password, args.name)
        if args.command == "seed":
            return await _seed()
        if args.command == "init-db":
            return await _init_db()
        if args.command == "rotate-keys":
            return await _rotate_keys()
        if args.command == "gen-key":
            return generate_master_key()
        raise SystemExit(2)
    finally:
        await dispose_engine()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        message = asyncio.run(_dispatch(args))
    except AppError as exc:
        sys.stderr.write(f"Error: {exc.message}\n")
        return 1
    sys.stdout.write(message + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
