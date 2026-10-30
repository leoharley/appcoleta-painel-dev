"""Configurações editáveis pela tela de Configurações.

Campos marcados como secretos são gravados criptografados (Fernet, chave
derivada de MARKETERO_SECRET_KEY) e nunca voltam para a tela em claro.
"""
import base64
import hashlib
import secrets
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from .config import SECRET_KEY
from .models import Setting

_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(("marketero:" + SECRET_KEY).encode()).digest()))

DEFAULT_SYSTEM_PROMPT = """Você é o assistente virtual do Instituto Alves Lima, instituto de pesquisas de opinião pública, eleitorais, de mercado e de gestão pública, com sede em Montes Claros (MG).

Seu objetivo é apresentar os serviços do Instituto a partidos políticos, pré-candidatos e gestores, entender a necessidade de quem conversa com você e encaminhar o contato para a equipe comercial quando houver interesse.

Seja cordial, objetivo e profissional. Faça uma pergunta por vez. Não invente preços, prazos ou resultados de pesquisas: quando não souber, diga que a equipe vai retornar."""

DEFAULT_KNOWLEDGE = """- Pesquisas eleitorais e de opinião pública (intenção de voto espontânea e estimulada, rejeição, avaliação de gestão, conhecimento de candidatos).
- Pesquisas de mercado e diagnóstico social.
- Coleta presencial com aplicativo próprio, GPS e auditoria; pesquisas registradas no TSE quando exigido.
- Site: https://institutoalveslima.com.br  |  E-mail: comercial@institutoalveslima.com.br"""


@dataclass(frozen=True)
class FieldDef:
    key: str
    label: str
    group: str
    kind: str = "text"  # text | secret | textarea | bool | select | int
    default: str = ""
    help: str = ""
    options: tuple = ()


FIELDS: list[FieldDef] = [
    # --- IA ---
    FieldDef("ai_provider", "Provedor de IA", "ia", "select", "anthropic",
             options=(("anthropic", "Claude (Anthropic)"), ("openai", "ChatGPT (OpenAI ou compatível)"))),
    FieldDef("anthropic_api_key", "Chave da API Anthropic", "ia", "secret"),
    FieldDef("anthropic_model", "Modelo Claude", "ia", "text", "claude-opus-5-5",
             help="Ex.: claude-opus-5-5, claude-sonnet-5-5, claude-haiku-5-5"),
    FieldDef("anthropic_effort", "Esforço de raciocínio (Claude)", "ia", "select", "low",
             options=(("low", "Baixo (respostas rápidas)"), ("medium", "Médio"), ("high", "Alto"))),
    FieldDef("openai_api_key", "Chave da API OpenAI", "ia", "secret"),
    FieldDef("openai_model", "Modelo ChatGPT", "ia", "text", "gpt-4.1-mini",
             help="Qualquer modelo de chat da sua conta OpenAI."),
    FieldDef("openai_base_url", "URL base (opcional)", "ia", "text", "",
             help="Só para APIs compatíveis com a OpenAI (ex.: Azure, OpenRouter). Deixe vazio para a OpenAI."),
    # --- Atendimento do chatbot ---
    FieldDef("bot_enabled", "Chatbot respondendo automaticamente", "atendimento", "bool", "1"),
    FieldDef("history_messages", "Mensagens de histórico enviadas à IA", "atendimento", "int", "20"),
    FieldDef("reply_delay_first", "Tempo para a primeira resposta (segundos)", "atendimento", "int", "4",
             help="Quando o chatbot ainda não respondeu a pessoa nas últimas 24 h (contato novo ou retomando a conversa)."),
    FieldDef("reply_delay_next", "Tempo para as demais respostas (segundos)", "atendimento", "int", "4",
             help="Nas respostas seguintes da mesma conversa."),
    FieldDef("system_prompt", "Instruções do assistente", "atendimento", "textarea", DEFAULT_SYSTEM_PROMPT),
    FieldDef("knowledge_base", "Base de conhecimento (serviços, contatos, perguntas frequentes)", "atendimento",
             "textarea", DEFAULT_KNOWLEDGE),
    # --- Canal ---
    FieldDef("wa_provider", "Como o sistema acessa o WhatsApp", "canal", "select", "kentro",
             options=(("kentro", "Pelo Kentro (atenderbem) — recomendado"), ("meta", "Direto na Cloud API da Meta"))),
    FieldDef("wa_simulation", "Modo simulação (não envia de verdade)", "canal", "bool", "1",
             help="Ligado: os disparos e respostas são só registrados. Desligue quando a configuração estiver pronta."),
    # --- Kentro ---
    FieldDef("kentro_base_url", "Endereço do Kentro", "kentro", "text", "https://institutoalves.atenderbem.com"),
    FieldDef("kentro_queue_id", "ID da fila do WhatsApp", "kentro", "text",
             help="Fila (canal) do WhatsApp oficial no Kentro por onde o robô envia e responde."),
    FieldDef("kentro_api_key", "Chave da API (da fila ou chave global)", "kentro", "secret",
             help="Kentro > Configurações > Geral > Chave global de API, ou a chave configurada na fila."),
    FieldDef("kentro_poll_seconds", "Verificar mensagens novas a cada (segundos)", "kentro", "int", "8"),
    FieldDef("kentro_answer_assigned", "Responder também atendimentos já assumidos por um atendente", "kentro",
             "bool", "0", help="Desligado (recomendado): o robô só responde enquanto o atendimento aguarda na fila."),
    FieldDef("kentro_action_when_open", "Disparo para quem já está em atendimento", "kentro", "select", "1",
             options=(("1", "Não enviar (marca como falha)"), ("2", "Esperar o atendimento terminar e enviar"),
                      ("0", "Enviar mesmo assim"))),
    # --- WhatsApp (Meta direto) ---
    FieldDef("wa_phone_number_id", "Phone Number ID", "whatsapp", "text",
             help="Meta for Developers > WhatsApp > Configuração da API."),
    FieldDef("wa_business_account_id", "WhatsApp Business Account ID (WABA)", "whatsapp", "text"),
    FieldDef("wa_access_token", "Token de acesso (permanente, de usuário do sistema)", "whatsapp", "secret"),
    FieldDef("wa_app_secret", "App Secret (chave secreta do app)", "whatsapp", "secret",
             help="Usada para validar a assinatura das notificações do webhook."),
    FieldDef("wa_verify_token", "Token de verificação do webhook", "whatsapp", "text",
             help="Copie este valor para o campo 'Verificar token' no painel da Meta."),
    FieldDef("wa_graph_version", "Versão da Graph API", "whatsapp", "text", "v23.0"),
    # --- Disparos ---
    FieldDef("dispatch_daily_limit", "Limite de envios por dia (todas as campanhas)", "disparo", "int", "250",
             help="Respeite o limite de conversas do seu número na Meta (começa em 250/dia)."),
    FieldDef("dispatch_hour_start", "Horário inicial de envio", "disparo", "int", "8"),
    FieldDef("dispatch_hour_end", "Horário final de envio", "disparo", "int", "19"),
    FieldDef("dispatch_weekends", "Enviar aos sábados e domingos", "disparo", "bool", "0"),
    FieldDef("optout_keywords", "Palavras de descadastro", "disparo", "text", "SAIR, PARAR, STOP, CANCELAR, DESCADASTRAR",
             help="Mensagem igual a uma destas palavras descadastra o contato."),
    FieldDef("optout_reply", "Resposta ao descadastro", "disparo", "textarea",
             "Pronto, você não receberá mais mensagens nossas. Se mudar de ideia, é só nos chamar por aqui."),
]
FIELD_MAP = {f.key: f for f in FIELDS}

# Subtelas de Configurações: (endereço, título, grupos de campos)
SECTIONS = [
    ("ia", "Inteligência artificial", ("ia",)),
    ("atendimento", "Atendimento do chatbot", ("atendimento",)),
    ("whatsapp", "WhatsApp", ("canal", "kentro", "whatsapp")),
    ("disparos", "Disparos e descadastro", ("disparo",)),
]
SECTION_GROUPS = {key: groups for key, _title, groups in SECTIONS}
MAX_REPLY_DELAY = 600


def _encrypt(v: str) -> str:
    return "enc:" + _fernet.encrypt(v.encode()).decode()


def _decrypt(v: str) -> str:
    if not v.startswith("enc:"):
        return v
    try:
        return _fernet.decrypt(v[4:].encode()).decode()
    except InvalidToken:
        return ""


def get(db: Session, key: str) -> str:
    row = db.get(Setting, key)
    if row is None:
        f = FIELD_MAP.get(key)
        return f.default if f else ""
    return _decrypt(row.value)


def get_bool(db: Session, key: str) -> bool:
    return get(db, key) in ("1", "true", "on", "yes")


def get_int(db: Session, key: str, fallback: int = 0) -> int:
    try:
        return int(get(db, key))
    except (TypeError, ValueError):
        return fallback


def set_value(db: Session, key: str, value: str) -> None:
    f = FIELD_MAP.get(key)
    stored = _encrypt(value) if (f and f.kind == "secret" and value) else value
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=stored))
    else:
        row.value = stored


def is_set(db: Session, key: str) -> bool:
    return bool(get(db, key))


def ensure_defaults(db: Session) -> None:
    if not db.get(Setting, "wa_verify_token"):
        set_value(db, "wa_verify_token", secrets.token_urlsafe(24))
    db.commit()
