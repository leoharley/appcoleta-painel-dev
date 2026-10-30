from datetime import datetime, timezone

from sqlalchemy import (Boolean, DateTime, ForeignKey, Integer, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class KnowledgeDoc(Base):
    """Base de conhecimento adicional importada de arquivos .md (entra nas instruções da IA)."""
    __tablename__ = "knowledge_docs"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    content: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Contact(Base):
    __tablename__ = "contacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    organization: Mapped[str] = mapped_column(String(200), default="")
    role: Mapped[str] = mapped_column(String(120), default="")
    # Telefone em formato internacional, só dígitos (ex.: 5561999998888) = wa_id do WhatsApp
    phone: Mapped[str | None] = mapped_column(String(20), unique=True, nullable=True, index=True)
    phone_is_mobile: Mapped[bool] = mapped_column(Boolean, default=False)
    email: Mapped[str] = mapped_column(String(200), default="")
    website: Mapped[str] = mapped_column(String(300), default="")
    address: Mapped[str] = mapped_column(String(400), default="")
    city: Mapped[str] = mapped_column(String(120), default="")
    uf: Mapped[str] = mapped_column(String(2), default="")
    source: Mapped[str] = mapped_column(String(60), default="manual")
    source_ref: Mapped[str] = mapped_column(String(400), default="")
    tags: Mapped[str] = mapped_column(String(400), default="")
    # unknown | valid (respondeu/entregue) | invalid (sem WhatsApp)
    whatsapp_status: Mapped[str] = mapped_column(String(20), default="unknown")
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False)
    opted_out_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    def tag_list(self) -> list[str]:
        return [t.strip() for t in (self.tags or "").split(",") if t.strip()]


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"), unique=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True)
    bot_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    # A IA sinalizou que a conversa precisa de alguém da equipe (o robô continua respondendo)
    needs_human: Mapped[bool] = mapped_column(Boolean, default=False)
    # Atendimento aberto no Kentro (quando o canal é o Kentro); vazio = sem atendimento aberto
    external_chat_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # O atendimento atual já tem atendente humano no Kentro
    external_assigned: Mapped[bool] = mapped_column(Boolean, default=False)
    unread: Mapped[int] = mapped_column(Integer, default=0)
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    contact: Mapped[Contact] = relationship()
    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", order_by="Message.id", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    direction: Mapped[str] = mapped_column(String(3))  # in | out
    author: Mapped[str] = mapped_column(String(10), default="contact")  # contact | bot | human | campaign
    kind: Mapped[str] = mapped_column(String(20), default="text")
    body: Mapped[str] = mapped_column(Text, default="")
    wa_message_id: Mapped[str | None] = mapped_column(String(120), index=True, nullable=True)
    # received | sent | delivered | read | failed | simulated
    status: Mapped[str] = mapped_column(String(20), default="received")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    template_name: Mapped[str] = mapped_column(String(200))
    # Identificador do template no canal: ID numérico no Kentro; nome do template na Meta
    template_ref: Mapped[str] = mapped_column(String(100), default="")
    template_language: Mapped[str] = mapped_column(String(20), default="pt_BR")
    template_body: Mapped[str] = mapped_column(Text, default="")
    # Valores das variáveis {{1}}, {{2}}... separados por linha; aceitam {nome}, {organizacao}, {cidade}, {uf}
    variables: Mapped[str] = mapped_column(Text, default="")
    ai_context: Mapped[str] = mapped_column(Text, default="")
    filter_tags: Mapped[str] = mapped_column(String(400), default="")
    # draft | running | paused | finished
    status: Mapped[str] = mapped_column(String(20), default="draft")
    start_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rate_per_minute: Mapped[int] = mapped_column(Integer, default=10)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    recipients: Mapped[list["CampaignRecipient"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan")


class CampaignRecipient(Base):
    __tablename__ = "campaign_recipients"
    __table_args__ = (UniqueConstraint("campaign_id", "contact_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"))
    # pending | sent | delivered | read | replied | failed | skipped
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    wa_message_id: Mapped[str | None] = mapped_column(String(120), index=True, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    campaign: Mapped[Campaign] = relationship(back_populates="recipients")
    contact: Mapped[Contact] = relationship()


class KentroChat(Base):
    """Atendimentos do Kentro já vistos e até qual mensagem (kId) foram lidos."""
    __tablename__ = "kentro_chats"
    chat_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id", ondelete="SET NULL"),
                                                        nullable=True)
    last_kid: Mapped[int] = mapped_column(Integer, default=0)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    closed: Mapped[bool] = mapped_column(Boolean, default=False)


class ImportJob(Base):
    __tablename__ = "import_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(60))
    description: Mapped[str] = mapped_column(String(400), default="")
    # running | done | error
    status: Mapped[str] = mapped_column(String(20), default="running")
    log: Mapped[str] = mapped_column(Text, default="")
    found: Mapped[int] = mapped_column(Integer, default=0)
    created: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
