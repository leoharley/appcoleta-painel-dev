"""Geração de respostas do chatbot: Claude (SDK oficial da Anthropic) ou ChatGPT (SDK oficial da OpenAI)."""
import logging
from dataclasses import dataclass

import anthropic
import openai
from sqlalchemy.orm import Session

from . import settings_store as cfg

log = logging.getLogger("marketero.ai")

# Modelos que aceitam o fallback automático do servidor quando o modelo recusa um pedido.
_FALLBACK_MODELS = ("claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")
# Modelos que aceitam output_config.effort (nos demais o parâmetro dá erro 400).
_EFFORT_PREFIXES = ("claude-opus-5", "claude-sonnet-5", "claude-haiku-5", "claude-fable", "claude-mythos",
                    "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6", "claude-opus-4-5")
# Respostas de chat: sem esperas longas (o padrão dos SDKs chega a 10 min por tentativa).
TIMEOUT_SECONDS = 60
MAX_RETRIES = 1


class AIError(Exception):
    pass


@dataclass
class AIConfig:
    provider: str
    anthropic_key: str
    anthropic_model: str
    anthropic_effort: str
    openai_key: str
    openai_model: str
    openai_base_url: str

    @property
    def configured(self) -> bool:
        if self.provider == "openai":
            return bool(self.openai_key and self.openai_model)
        return bool(self.anthropic_key and self.anthropic_model)

    @property
    def label(self) -> str:
        return f"ChatGPT ({self.openai_model})" if self.provider == "openai" else f"Claude ({self.anthropic_model})"


def config_from_settings(db: Session) -> AIConfig:
    return AIConfig(
        provider=cfg.get(db, "ai_provider") or "anthropic",
        anthropic_key=cfg.get(db, "anthropic_api_key"),
        anthropic_model=cfg.get(db, "anthropic_model") or "claude-opus-5-5",
        anthropic_effort=cfg.get(db, "anthropic_effort") or "low",
        openai_key=cfg.get(db, "openai_api_key"),
        openai_model=cfg.get(db, "openai_model"),
        openai_base_url=cfg.get(db, "openai_base_url"),
    )


def _normalize_history(history: list[dict]) -> list[dict]:
    """Junta mensagens seguidas do mesmo papel e garante que a conversa comece pelo usuário."""
    out: list[dict] = []
    for m in history:
        text = (m.get("content") or "").strip()
        if not text:
            continue
        if out and out[-1]["role"] == m["role"]:
            out[-1]["content"] += "\n\n" + text
        else:
            out.append({"role": m["role"], "content": text})
    while out and out[0]["role"] == "assistant":
        out.pop(0)
    return out


async def generate_reply(conf: AIConfig, system: str, history: list[dict], system_dynamic: str = "") -> str:
    """Gera a resposta do chatbot.

    system: instruções fixas (iguais em toda conversa: prompt, base de conhecimento, documentos .md).
            Vêm primeiro para aproveitar o cache de prompt dos provedores.
    system_dynamic: dados que mudam por conversa (contato, campanha).
    history: [{"role": "user"|"assistant", "content": str}], terminando na mensagem do contato.
    """
    if not conf.configured:
        raise AIError(f"Chave da API {'OpenAI' if conf.provider == 'openai' else 'Anthropic'} ou modelo não "
                      "configurado. Preencha em Configurações e salve.")
    msgs = _normalize_history(history)
    if not msgs or msgs[-1]["role"] != "user":
        raise AIError("Não há mensagem do contato para responder.")
    if conf.provider == "openai":
        return await _openai(conf, "\n\n".join(p for p in (system, system_dynamic) if p), msgs)
    return await _anthropic(conf, system, system_dynamic, msgs)


async def _anthropic(conf: AIConfig, system: str, system_dynamic: str, msgs: list[dict]) -> str:
    client = anthropic.AsyncAnthropic(api_key=conf.anthropic_key, timeout=TIMEOUT_SECONDS, max_retries=MAX_RETRIES)
    model = conf.anthropic_model
    # Ponto de cache no fim da parte fixa: a base de conhecimento é lida do cache nas mensagens seguintes.
    system_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    if system_dynamic:
        system_blocks.append({"type": "text", "text": system_dynamic})
    kwargs: dict = {"model": model, "max_tokens": 8000, "system": system_blocks, "messages": msgs}
    if model.startswith(_EFFORT_PREFIXES):
        kwargs["output_config"] = {"effort": conf.anthropic_effort or "low"}
    try:
        if model in _FALLBACK_MODELS:
            resp = await client.beta.messages.create(
                **kwargs, betas=["server-side-fallback-2026-07-01"], extra_body={"fallbacks": "default"})
        else:
            resp = await client.messages.create(**kwargs)
    except anthropic.AuthenticationError as e:
        raise AIError("Chave da Anthropic inválida.") from e
    except anthropic.NotFoundError as e:
        raise AIError(f"Modelo Claude não encontrado: {model}") from e
    except anthropic.RateLimitError as e:
        raise AIError("Limite de uso da Anthropic atingido; tente mais tarde.") from e
    except anthropic.APIStatusError as e:
        raise AIError(f"Erro da API Anthropic ({e.status_code}): {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise AIError("Sem conexão com a API da Anthropic.") from e
    except anthropic.AnthropicError as e:
        raise AIError(f"Erro da Anthropic: {e}") from e

    if resp.stop_reason == "refusal":
        raise AIError("O modelo recusou responder esta mensagem.")
    if resp.stop_reason == "max_tokens":
        raise AIError("A resposta da IA foi cortada pelo limite de tamanho; não foi enviada.")
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    if not text:
        raise AIError("A IA não devolveu texto.")
    return text


async def _openai(conf: AIConfig, system: str, msgs: list[dict]) -> str:
    try:
        client = openai.AsyncOpenAI(api_key=conf.openai_key, base_url=conf.openai_base_url or None,
                                    timeout=TIMEOUT_SECONDS, max_retries=MAX_RETRIES)
        resp = await client.chat.completions.create(
            model=conf.openai_model,
            messages=[{"role": "system", "content": system}, *msgs],
        )
    except openai.AuthenticationError as e:
        raise AIError("Chave da OpenAI inválida.") from e
    except openai.NotFoundError as e:
        raise AIError(f"Modelo não encontrado: {conf.openai_model}") from e
    except openai.RateLimitError as e:
        raise AIError("Limite de uso da OpenAI atingido; tente mais tarde.") from e
    except openai.APIStatusError as e:
        raise AIError(f"Erro da API OpenAI ({e.status_code}): {e.message}") from e
    except openai.APIConnectionError as e:
        raise AIError("Sem conexão com a API da OpenAI.") from e
    except openai.OpenAIError as e:
        raise AIError(f"Erro da OpenAI: {e}") from e
    if not resp.choices:
        raise AIError("A IA não devolveu resposta.")
    choice = resp.choices[0]
    if choice.finish_reason == "length":
        raise AIError("A resposta da IA foi cortada pelo limite de tamanho; não foi enviada.")
    text = (choice.message.content or "").strip()
    if not text:
        raise AIError("A IA não devolveu texto.")
    return text
