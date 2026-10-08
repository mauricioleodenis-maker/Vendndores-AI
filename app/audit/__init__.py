"""Auditoria con cadena HMAC: ``from app.audit import log_event``."""

from app.audit.service import log_event, verify_chain

__all__ = ["log_event", "verify_chain"]
