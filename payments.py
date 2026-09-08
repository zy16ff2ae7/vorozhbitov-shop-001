"""Payment adapters: Lava (card/SBP), Crypto Pay, Telegram Stars.

Keys come from env. Missing keys mean that method is omitted, not a crash.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any

LOG = logging.getLogger("brand_bot.pay")

PRICE_RE = re.compile(r"\d+")

# Одно число: '11 900 ₽', '11900', '11 900.00 ₽'. Разряды можно разделять
# пробелом (в том числе неразрывным) или апострофом; дробная часть — копейки.
STRICT_PRICE_RE = re.compile(
    r"""^\s*
        (?:от\s+)?                          # необязательное «от»
        (?P<int>\d{1,3}(?:[\s\u00a0\u202f']\d{3})*|\d+)
        (?:[.,](?P<frac>\d{1,2}))?          # копейки
        \s*(?:₽|руб\.?|rub|r)?              # необязательная валюта
    \s*$""",
    re.IGNORECASE | re.VERBOSE,
)


class PriceError(ValueError):
    """Цену не удалось разобрать однозначно."""


def parse_price_strict(value: Any) -> int:
    """Разобрать цену в рублях или бросить :class:`PriceError`.

    В отличие от старого поведения (склеить все цифры подряд) неоднозначные
    строки вроде '1 200 - 1 500 ₽' или '4900 (скидка 3900)' отклоняются:
    молча выставить счёт на 12 001 500 ₽ хуже, чем отказать администратору.
    Копейки округляются до рубля — оплата идёт в целых рублях.
    """
    raw = str(value or "").strip()
    if not raw:
        raise PriceError("Цена пустая")
    match = STRICT_PRICE_RE.match(raw)
    if not match:
        raise PriceError(f"Непонятная цена: {raw!r}")
    rubles = int(re.sub(r"[\s\u00a0\u202f']", "", match.group("int")))
    frac = match.group("frac")
    if frac:
        rubles += round(int(frac.ljust(2, "0")) / 100)
    if rubles < 0 or rubles > 10_000_000:
        raise PriceError(f"Цена вне допустимых границ: {rubles}")
    return rubles


def parse_price_rub(value: Any) -> int:
    """'11 900 ₽' → 11900. Неоднозначная или пустая строка → 0.

    Ноль означает «платить нечем»: вызывающий код не создаёт счёт, поэтому
    сломанный прайс приводит к заявке без оплаты, а не к списанию наугад.
    """
    try:
        return parse_price_strict(value)
    except PriceError:
        LOG.warning("Не удалось разобрать цену %r — счёт не выставляется", value)
        return 0


def format_rub(amount: int) -> str:
    return f"{amount:,}".replace(",", " ") + " ₽"


def stars_amount(rub: int, rub_per_star: float) -> int:
    rate = float(rub_per_star or 2.0)
    if rate <= 0:
        rate = 2.0
    return max(1, int(round(rub / rate)))


def lava_signature(payload: dict[str, Any], secret: str) -> str:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_lava_webhook(raw_body: bytes, signature: str, extra_key: str) -> bool:
    if not extra_key or not signature:
        return False
    expected = hmac.new(extra_key.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.lower(), signature.lower())


def verify_crypto_webhook(raw_body: bytes, signature: str, token: str) -> bool:
    if not token or not signature:
        return False
    secret = hashlib.sha256(token.encode("utf-8")).digest()
    expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.lower(), signature.lower())


def _http_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
    timeout: int = 20,
) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {details}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Bad JSON from {url}") from exc
    return parsed if isinstance(parsed, dict) else {"result": parsed}


def create_lava_invoice(
    shop_id: str,
    secret: str,
    order_id: str,
    amount_rub: int,
    comment: str,
    hook_url: str = "",
    success_url: str = "",
) -> dict[str, str]:
    payload: dict[str, Any] = {
        "shopId": shop_id,
        "sum": float(amount_rub),
        "orderId": order_id,
        "comment": comment[:200],
    }
    if hook_url:
        payload["hookUrl"] = hook_url
    if success_url:
        payload["successUrl"] = success_url
    sign = lava_signature(payload, secret)
    result = _http_json(
        "https://api.lava.ru/business/invoice/create",
        payload,
        {"Signature": sign},
    )
    data = result.get("data") if isinstance(result.get("data"), dict) else result
    url = str(data.get("url") or data.get("paymentUrl") or result.get("url") or "")
    invoice_id = str(data.get("id") or data.get("invoice_id") or data.get("invoiceId") or "")
    if not url:
        raise RuntimeError(f"Lava invoice has no URL: {result}")
    return {"url": url, "id": invoice_id}


def create_crypto_invoice(
    token: str,
    amount_rub: int,
    description: str,
    payload: str,
    paid_btn_url: str = "",
) -> dict[str, str]:
    body: dict[str, Any] = {
        "currency_type": "fiat",
        "fiat": "RUB",
        "amount": str(amount_rub),
        "description": description[:1024],
        "payload": payload[:4096],
        "expires_in": 3600,
        "allow_comments": False,
        "allow_anonymous": False,
    }
    if paid_btn_url.startswith("https://"):
        body["paid_btn_name"] = "callback"
        body["paid_btn_url"] = paid_btn_url
    result = _http_json(
        "https://pay.crypt.bot/api/createInvoice",
        body,
        {"Crypto-Pay-API-Token": token},
    )
    if result.get("ok") is False:
        raise RuntimeError(f"Crypto Pay error: {result}")
    data = result.get("result") if isinstance(result.get("result"), dict) else result
    url = str(
        data.get("mini_app_invoice_url")
        or data.get("bot_invoice_url")
        or data.get("web_app_invoice_url")
        or data.get("pay_url")
        or ""
    )
    invoice_id = str(data.get("invoice_id") or "")
    if not url:
        raise RuntimeError(f"Crypto invoice has no URL: {result}")
    return {"url": url, "id": invoice_id}
