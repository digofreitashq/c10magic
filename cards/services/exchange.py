"""Cotação do dólar, usada para converter filtros em R$ para o Scryfall."""

import logging
from decimal import Decimal

import requests
from django.conf import settings
from django.core.cache import cache


log = logging.getLogger(__name__)

CACHE_KEY = "c10magic:usd_brl"


def usd_brl() -> Decimal:
    rate = cache.get(CACHE_KEY)

    if rate is not None:
        return rate

    try:
        response = requests.get(settings.USD_BRL_API, timeout=10)
        response.raise_for_status()
        rate = Decimal(response.json()["USDBRL"]["bid"]).quantize(Decimal("0.0001"))
        cache.set(CACHE_KEY, rate, 6 * 60 * 60)
    except Exception as exc:  # noqa: BLE001 - cotação é auxiliar
        log.warning("Cotação indisponível (%s); usando %s", exc, settings.USD_BRL_FALLBACK)
        rate = Decimal(str(settings.USD_BRL_FALLBACK))
        # Tenta de novo daqui a pouco
        cache.set(CACHE_KEY, rate, 10 * 60)

    return rate


def usd_to_brl(value, rate=None):
    if value is None:
        return None

    rate = rate or usd_brl()

    return (Decimal(value) * rate).quantize(Decimal("0.01"))
