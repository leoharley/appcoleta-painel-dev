import hashlib
import hmac
import secrets
import time
from collections import defaultdict

from fastapi import Request

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, dk_hex = stored.split("$")
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex),
                            n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except (ValueError, TypeError):
        return False


class LoginRequired(Exception):
    pass


class CsrfError(Exception):
    pass


def current_user_id(request: Request) -> int | None:
    return request.session.get("uid")


def require_login(request: Request) -> int:
    uid = current_user_id(request)
    if not uid:
        raise LoginRequired()
    return uid


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        request.session["csrf"] = tok
    return tok


async def check_csrf(request: Request):
    """Lê o formulário, valida o token CSRF e devolve o formulário."""
    form = await request.form()
    sent = form.get("csrf_token", "")
    if not sent or not hmac.compare_digest(str(sent), request.session.get("csrf", "")):
        raise CsrfError()
    return form


# Limite simples de tentativas de login por IP (memória do processo).
_failures: dict[str, list[float]] = defaultdict(list)
MAX_FAILURES, WINDOW = 5, 15 * 60


def _prune(now: float) -> None:
    for ip in [k for k, ts in _failures.items() if not ts or now - ts[-1] >= WINDOW]:
        del _failures[ip]


def login_blocked(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _failures.get(ip, []) if now - t < WINDOW]
    if recent:
        _failures[ip] = recent
    else:
        _failures.pop(ip, None)
    return len(recent) >= MAX_FAILURES


def register_failure(ip: str) -> None:
    now = time.time()
    if len(_failures) > 5000:
        _prune(now)
    _failures[ip].append(now)


def clear_failures(ip: str) -> None:
    _failures.pop(ip, None)
