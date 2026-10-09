"""Importa TODOS los modelos para registrarlos en ``Base.metadata`` (Alembic, create_all)."""

from app.db.models.audit import AuditLog
from app.db.models.booking import (
    Appointment,
    CalendarConnection,
    Resource,
    TimeOff,
    WorkingHours,
)
from app.db.models.bots import BotConfig, LlmUsage
from app.db.models.catalog import Faq, KbDocument, Service
from app.db.models.contacts import Consent, Contact
from app.db.models.conversations import Conversation, Handoff, Message
from app.db.models.leads import (
    DemoWhatsappSession,
    Lead,
    LeadEvent,
    LeadSource,
    ReviewSignal,
    SecretShopTest,
)
from app.db.models.niches import NicheTemplate
from app.db.models.outreach import Campaign, CampaignTarget, MessageTemplate, OutreachMessage
from app.db.models.payments import PaymentIntent
from app.db.models.plans import BillingRecord, Offer, Plan, Subscription, UsageCounter
from app.db.models.privacy import SuppressionEntry
from app.db.models.scheduling import ScheduledJob, WebhookEvent
from app.db.models.tenants import ChannelAccount, Tenant, TenantProfile, TenantSecret
from app.db.models.users import User, UserSession
from app.db.models.voice import CallSession

__all__ = [
    "Appointment",
    "AuditLog",
    "BillingRecord",
    "BotConfig",
    "CallSession",
    "CalendarConnection",
    "Campaign",
    "CampaignTarget",
    "ChannelAccount",
    "Consent",
    "Contact",
    "Conversation",
    "Faq",
    "Handoff",
    "KbDocument",
    "DemoWhatsappSession",
    "Lead",
    "LeadEvent",
    "LeadSource",
    "LlmUsage",
    "Message",
    "MessageTemplate",
    "NicheTemplate",
    "Offer",
    "OutreachMessage",
    "PaymentIntent",
    "Plan",
    "Resource",
    "ReviewSignal",
    "ScheduledJob",
    "SecretShopTest",
    "Service",
    "Subscription",
    "SuppressionEntry",
    "Tenant",
    "TenantProfile",
    "TenantSecret",
    "TimeOff",
    "UsageCounter",
    "User",
    "UserSession",
    "WebhookEvent",
    "WorkingHours",
]
