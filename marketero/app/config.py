"""Configuração vinda do ambiente (arquivo /etc/marketero/marketero.env no servidor).

Segredos de integrações (chaves de IA, token do WhatsApp) NÃO ficam aqui:
são cadastrados pela tela de Configurações e gravados criptografados no banco.
"""
import os
import secrets
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("MARKETERO_DATA_DIR", BASE_DIR.parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.environ.get("MARKETERO_DATABASE_URL", f"sqlite:///{DATA_DIR / 'marketero.db'}")

# Prefixo público da aplicação (o Apache publica em /marketero).
ROOT_PATH = os.environ.get("MARKETERO_ROOT_PATH", "").rstrip("/")

# URL pública completa, usada para mostrar o endereço do webhook nas configurações.
PUBLIC_URL = os.environ.get("MARKETERO_PUBLIC_URL", "http://localhost:8100").rstrip("/")

COOKIE_SECURE = os.environ.get("MARKETERO_COOKIE_SECURE", "1") == "1"

TIMEZONE = "America/Sao_Paulo"

SECRET_KEY = os.environ.get("MARKETERO_SECRET_KEY", "")
if not SECRET_KEY:
    # Só para desenvolvimento local: sem chave fixa, as sessões e os segredos
    # criptografados deixam de valer a cada reinício.
    SECRET_KEY = secrets.token_urlsafe(48)
    print("AVISO: MARKETERO_SECRET_KEY não definida; usando chave temporária.", file=sys.stderr)
