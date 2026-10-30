import re

import phonenumbers
from phonenumbers import PhoneNumberType

# Telefones brasileiros como aparecem em páginas: (61) 3333-4444, 61 99999-8888, +55 (61) 9 9999-8888, 0800...
PHONE_RE = re.compile(r"(?:\+?55[\s.-]?)?\(?\b\d{2}\)?[\s.-]?(?:9[\s.]?)?\d{4}[\s.-]?\d{4}\b")


def add_br_ninth_digit(digits: str) -> str:
    """Celulares brasileiros antigos aparecem sem o 9 (ex.: wa_id 553899991111 do WhatsApp).
    A forma canônica do sistema é sempre com o 9 (5538999991111)."""
    if len(digits) == 12 and digits.startswith("55") and digits[4] in "6789":
        return digits[:4] + "9" + digits[4:]
    if len(digits) == 10 and not digits.startswith("0") and digits[2] in "6789":
        return digits[:2] + "9" + digits[2:]
    return digits


def normalize(raw: str, region: str = "BR") -> str | None:
    """Devolve o número no formato do WhatsApp (só dígitos, com DDI), ou None se inválido."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    international = raw.strip().startswith("+") or (digits.startswith("55") and len(digits) in (12, 13))
    if region == "BR" and (international and digits.startswith("55") or not international):
        digits = add_br_ninth_digit(digits)
    try:
        num = phonenumbers.parse("+" + digits if international else digits, region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(num):
        return None
    return f"{num.country_code}{num.national_number}"


def is_mobile(e164_digits: str) -> bool:
    try:
        num = phonenumbers.parse("+" + e164_digits, None)
    except phonenumbers.NumberParseException:
        return False
    return phonenumbers.number_type(num) in (PhoneNumberType.MOBILE, PhoneNumberType.FIXED_LINE_OR_MOBILE)


def pretty(e164_digits: str | None) -> str:
    if not e164_digits:
        return ""
    try:
        num = phonenumbers.parse("+" + e164_digits, None)
        return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
    except phonenumbers.NumberParseException:
        return e164_digits


def find_phones(text: str) -> list[str]:
    """Extrai e normaliza todos os telefones válidos de um texto, sem repetição, celulares primeiro."""
    seen, out = set(), []
    for m in PHONE_RE.finditer(text or ""):
        n = normalize(m.group(0))
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return sorted(out, key=lambda n: not is_mobile(n))
