"""Motor conversacional del recepcionista (dueno: B5; firmas publicas fijas por MAESTRO §5).

Pipeline por mensaje: pre-checks (opt-out, humano a cargo, limites) -> guardrails de entrada ->
memoria (12 mensajes + resumen) -> system prompt fijo + tools (bucle <= 4) -> guardrails de
salida -> persistencia (cifrada) + uso de LLM. ``sandbox_reply`` ejecuta lo mismo sin escribir
mensajes ni contactos.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.client import LLMClient, get_llm
from app.ai.usage import merge_usage, record_llm_usage
from app.conversation import memory
from app.conversation.context import (
    BusinessContext,
    active_appointments,
    build_output_context,
    build_system_prompt,
    load_business_context,
)
from app.conversation.guardrails import (
    check_input,
    check_output,
    reply_for,
    wrap_user_text,
)
from app.conversation.handoff import bot_is_paused, open_handoff, resume_bot
from app.conversation.tools import ToolContext, run_tool, tool_definitions
from app.core.clock import utcnow
from app.core.config import get_settings
from app.core.errors import NotFoundError
from app.core.jobs import enqueue
from app.core.logging import get_logger
from app.core.rate_limit import get_rate_limiter
from app.db.models.contacts import Contact
from app.db.models.conversations import Conversation, Message
from app.plans.entitlements import PlanLimitExceeded, assert_within_limit, record_usage
from app.privacy.service import redact_pii

log = get_logger(__name__)

MAX_TOOL_ITERATIONS = 4
LLM_TIMEOUT_S = 20.0
CONTACT_MSGS_PER_HOUR = 30
MAX_FAILURES_BEFORE_HANDOFF = 2
RECENT_FLAG_WINDOW = 20
OFFERED_LOOKBACK = 8


@dataclass(slots=True)
class TurnResult:
    reply: str
    usage: dict[str, int] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    handoff_reason: str | None = None
    handoff_done: bool = False
    tools_used: list[str] = field(default_factory=list)
    offered: set[str] = field(default_factory=set)


def _tool_result_block(tool_id: str, content: str, is_error: bool) -> dict[str, Any]:
    return {"type": "tool_result", "tool_use_id": tool_id, "content": content, "is_error": is_error}


async def _call_llm(llm: LLMClient, **kwargs: Any) -> Any:
    return await asyncio.wait_for(llm.complete(**kwargs), timeout=LLM_TIMEOUT_S)


async def _converse(
    session: AsyncSession,
    llm: LLMClient,
    bc: BusinessContext,
    tctx: ToolContext,
    *,
    user_text: str,
    history: list[dict[str, Any]],
    summary: str,
    prior_injections: int = 0,
    prior_offtopic: int = 0,
) -> TurnResult:
    """Un turno completo (compartido por produccion y sandbox). No persiste mensajes."""
    verdict = check_input(
        user_text,
        templates=bc.templates,
        prior_injections=prior_injections,
        prior_offtopic_streak=prior_offtopic,
        business_name=bc.tenant.name,
    )
    if verdict.reply is not None:
        return TurnResult(
            reply=verdict.reply, flags=verdict.flags, handoff_reason=verdict.handoff_reason
        )

    appointments = await active_appointments(
        session, bc.tenant.id, tctx.contact.id if tctx.contact else None
    )
    system, rules_text = build_system_prompt(bc, appointments=appointments, summary=summary)
    out_ctx = build_output_context(bc, rules_text, _user_phone_digits(tctx, verdict.text))
    messages: list[dict[str, Any]] = [
        *history,
        {"role": "user", "content": wrap_user_text(verdict.text)},
    ]
    if history and history[-1]["role"] == "user":  # evita dos "user" seguidos
        messages = [
            *history[:-1],
            {
                "role": "user",
                "content": history[-1]["content"] + "\n" + wrap_user_text(verdict.text),
            },
        ]

    usage: dict[str, int] = {}
    result = TurnResult(reply="", flags=list(verdict.flags))
    final_text = ""
    exhausted = True
    tools = tool_definitions()
    for i in range(MAX_TOOL_ITERATIONS + 1):
        last = i == MAX_TOOL_ITERATIONS  # ultima: sin tools
        try:
            resp = await _call_llm(
                llm,
                system=system,
                messages=list(messages),
                tools=None if last else tools,
                max_tokens=700,
                temperature=0.3,
            )
        except Exception as exc:  # noqa: BLE001 - timeout/proveedor: respuesta segura + handoff
            log.error("conversation.llm_failed", error=type(exc).__name__)
            result.flags.append("llm_failure")
            result.handoff_reason = "tool_failure"
            result.reply = reply_for(bc.templates, "fallback_reply")
            result.usage = usage
            return _finish(result, tctx)
        merge_usage(usage, resp.usage)
        if not resp.tool_calls or last:
            final_text = resp.text
            exhausted = bool(resp.tool_calls) and not resp.text
            break
        blocks: list[dict[str, Any]] = []
        if resp.text:
            blocks.append({"type": "text", "text": resp.text})
        blocks += [
            {"type": "tool_use", "id": c.id, "name": c.name, "input": c.input}
            for c in resp.tool_calls
        ]
        messages.append({"role": "assistant", "content": blocks})
        results = []
        for call in resp.tool_calls:
            outcome = await run_tool(tctx, call.name, call.input)
            results.append(_tool_result_block(call.id, outcome.dumps(), outcome.is_error))
        messages.append({"role": "user", "content": results})

    result.usage = usage
    if exhausted:
        result.flags.append("tool_loop_exhausted")
        result.handoff_reason = result.handoff_reason or "tool_failure"
        result.reply = reply_for(bc.templates, "fallback_reply")
        return _finish(result, tctx)
    out = check_output(final_text or reply_for(bc.templates, "safe_reply"), out_ctx)
    if out.flags:
        result.flags += [f"out:{f}" for f in out.flags]
        log.warning(
            "guardrail.blocked" if out.blocked else "guardrail.sanitized",
            tenant_id=str(bc.tenant.id),
            flags=out.flags,
            alert=out.alert,
        )
    result.reply = out.text
    if tctx.failures >= MAX_FAILURES_BEFORE_HANDOFF and not tctx.handoff_reason:
        result.handoff_reason = "tool_failure"
    return _finish(result, tctx)


def _finish(result: TurnResult, tctx: ToolContext) -> TurnResult:
    result.tools_used = list(tctx.tools_used)
    result.offered = set(tctx.offered)
    if tctx.handoff_reason:
        result.handoff_reason = tctx.handoff_reason
        result.handoff_done = True
    return result


def _user_phone_digits(tctx: ToolContext, text: str) -> set[str]:
    """Digitos que el propio cliente escribio: no son 'datos de terceros' si el bot los repite."""
    return {re.sub(r"\D", "", m) for m in re.findall(r"\+?\d[\d\s().-]{6,}\d", text)}


class ConversationEngine:
    """Orquesta un turno de conversacion. ``llm`` se inyecta en tests; en prod ``get_llm()``."""

    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm

    def _client(self) -> LLMClient:
        return self._llm or get_llm()

    async def handle_inbound(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID,
        text: str,
    ) -> list[str]:
        conv = (
            await session.execute(
                select(Conversation).where(
                    Conversation.id == conversation_id, Conversation.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()
        if conv is None:
            raise NotFoundError("Conversación no encontrada")
        contact = (
            await session.get(Contact, conv.contact_id) if conv.contact_id is not None else None
        )
        if contact is not None and contact.tenant_id != tenant_id:
            raise NotFoundError("Conversación no encontrada")

        inbound = await self._inbound_message(session, conv, text)
        now = utcnow()
        conv.last_inbound_at = now
        conv.last_message_at = now

        # 2. Pre-checks: opt-out, humano a cargo, limite por contacto
        if contact is not None and contact.opted_out:
            await session.flush()
            return []
        if conv.status == "closed" or (conv.status == "handoff" and not bot_is_paused(conv)):
            resume_bot(conv)
        if bot_is_paused(conv):
            await session.flush()
            return []
        if contact is not None:
            rl = await get_rate_limiter().hit(
                f"conv-msg:{tenant_id}:{contact.id}", limit=CONTACT_MSGS_PER_HOUR, window_s=3600
            )
            if not rl.allowed:
                log.warning("conversation.rate_limited", tenant_id=str(tenant_id))
                inbound.guardrail_flags = {"flags": ["rate_limited"]}
                await session.flush()
                return []

        bc = await load_business_context(session, tenant_id)
        has_outbound, offered = await self._outbound_state(session, conv)
        first_turn = not has_outbound
        if first_turn:
            try:
                await assert_within_limit(session, tenant_id, "conversations")
            except PlanLimitExceeded:
                reply = reply_for(bc.templates, "limit_reply")
                await self._persist_reply(
                    session, conv, inbound, reply, TurnResult(reply=reply, flags=["plan_limit"])
                )
                return [reply]

        summary, history = await memory.build_context(session, conv, exclude_message_id=inbound.id)
        injections, offtopic = await self._recent_flags(session, conv, inbound.id)
        tctx = ToolContext(
            session=session,
            tenant_id=tenant_id,
            bc=bc,
            last_user_text=text,
            conversation=conv,
            contact=contact,
            offered=offered,
        )
        try:
            llm = self._client()
        except Exception as exc:  # noqa: BLE001 - IA sin configurar: no perder el mensaje
            log.error("conversation.llm_unavailable", error=type(exc).__name__)
            fallback = TurnResult(
                reply=reply_for(bc.templates, "fallback_reply"),
                flags=["llm_failure"],
                handoff_reason="tool_failure",
            )
            await open_handoff(session, conv, "tool_failure", _handoff_summary(fallback))
            inbound.guardrail_flags = {"flags": fallback.flags}
            await self._persist_reply(session, conv, inbound, fallback.reply, fallback)
            return [fallback.reply]
        result = await _converse(
            session,
            llm,
            bc,
            tctx,
            user_text=text,
            history=history,
            summary=summary,
            prior_injections=injections,
            prior_offtopic=offtopic,
        )
        if result.handoff_reason and not result.handoff_done:
            await open_handoff(session, conv, result.handoff_reason, _handoff_summary(result))
        inbound.guardrail_flags = {"flags": result.flags}
        await self._persist_reply(session, conv, inbound, result.reply, result)
        if first_turn:
            await record_usage(session, tenant_id, conversations=1)
        if await memory.needs_summary(session, conv):
            await enqueue("conversation.summarize", str(tenant_id), str(conv.id))
        return [result.reply]

    # ------------------------------------------------------------------ persistencia
    async def _inbound_message(
        self, session: AsyncSession, conv: Conversation, text: str
    ) -> Message:
        """Reutiliza el mensaje entrante ya guardado por el canal (sin procesar) o lo crea."""
        pending = (
            (
                await session.execute(
                    select(Message)
                    .where(
                        Message.tenant_id == conv.tenant_id,
                        Message.conversation_id == conv.id,
                        Message.direction == "in",
                        Message.processed_at.is_(None),
                    )
                    .order_by(Message.created_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if pending is not None and memory.decrypt_body(conv.tenant_id, pending) == text:
            pending.processed_at = utcnow()
            return pending
        msg = Message(
            tenant_id=conv.tenant_id,
            conversation_id=conv.id,
            direction="in",
            role="user",
            body_enc=memory.encrypt_body(conv.tenant_id, text),
            body_redacted=redact_pii(text)[:2000],
            status="received",
            processed_at=utcnow(),
            purge_after=(utcnow() + timedelta(days=get_settings().msg_retention_days)).date(),
        )
        session.add(msg)
        await session.flush()
        return msg

    async def _persist_reply(
        self,
        session: AsyncSession,
        conv: Conversation,
        inbound: Message,
        reply: str,
        result: TurnResult,
    ) -> None:
        usage_meta: dict[str, Any] = {
            "input_tokens": result.usage.get("input_tokens", 0),
            "output_tokens": result.usage.get("output_tokens", 0),
            "tools": result.tools_used,
            "offered_slots": sorted(result.offered),
        }
        session.add(
            Message(
                tenant_id=conv.tenant_id,
                conversation_id=conv.id,
                direction="out",
                role="assistant",
                body_enc=memory.encrypt_body(conv.tenant_id, reply),
                body_redacted=redact_pii(reply)[:2000],
                status="queued",
                llm_usage=usage_meta,
                guardrail_flags={"flags": result.flags} if result.flags else None,
                purge_after=(utcnow() + timedelta(days=get_settings().msg_retention_days)).date(),
            )
        )
        conv.last_message_at = utcnow()
        await record_llm_usage(session, conv.tenant_id, result.usage)
        await session.flush()

    async def _recent_flags(
        self, session: AsyncSession, conv: Conversation, current_id: uuid.UUID
    ) -> tuple[int, int]:
        """(intentos de inyeccion previos, racha de fuera-de-tema inmediatamente anterior)."""
        rows = (
            await session.execute(
                select(Message.guardrail_flags)
                .where(
                    Message.tenant_id == conv.tenant_id,
                    Message.conversation_id == conv.id,
                    Message.direction == "in",
                    Message.id != current_id,
                )
                .order_by(Message.created_at.desc())
                .limit(RECENT_FLAG_WINDOW)
            )
        ).all()
        flag_lists = [(r[0] or {}).get("flags", []) for r in rows]
        injections = sum("injection" in f for f in flag_lists)
        streak = 0
        for flags in flag_lists:
            if "off_topic" not in flags:
                break
            streak += 1
        return injections, streak

    async def _outbound_state(
        self, session: AsyncSession, conv: Conversation
    ) -> tuple[bool, set[str]]:
        """``(hay salientes previos, horarios ofrecidos recientemente)`` en una sola consulta."""
        rows = (
            await session.execute(
                select(Message.llm_usage)
                .where(
                    Message.tenant_id == conv.tenant_id,
                    Message.conversation_id == conv.id,
                    Message.direction == "out",
                )
                .order_by(Message.created_at.desc())
                .limit(OFFERED_LOOKBACK)
            )
        ).all()
        offered: set[str] = set()
        for (meta,) in rows:
            if isinstance(meta, dict):
                offered.update(str(s) for s in meta.get("offered_slots", []))
        return bool(rows), offered


def _handoff_summary(result: TurnResult) -> str:
    return json.dumps({"flags": result.flags}, ensure_ascii=False)[:300]


async def sandbox_reply(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    history: list[dict[str, Any]],
    text: str,
    *,
    bot_config_id: uuid.UUID | None = None,
    llm: LLMClient | None = None,
) -> str:
    """Respuesta de prueba (chat del panel): mismos guardrails y tools, sin escribir
    ``messages`` ni ``contacts``; reservas y traspasos se simulan."""
    bc = await load_business_context(session, tenant_id, bot_config_id)
    items = [
        (str(h.get("role")), str(h.get("content", "")))
        for h in history[-memory.HISTORY_LIMIT :]
        if h.get("role") in ("user", "assistant")
    ]
    tctx = ToolContext(
        session=session, tenant_id=tenant_id, bc=bc, last_user_text=text, sandbox=True
    )
    result = await _converse(
        session,
        llm or get_llm(),
        bc,
        tctx,
        user_text=text,
        history=memory.to_llm_messages(items),
        summary="",
    )
    await record_llm_usage(session, tenant_id, result.usage, purpose="sandbox", count_plan=False)
    await session.flush()
    return result.reply
