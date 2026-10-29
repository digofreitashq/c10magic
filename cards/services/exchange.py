"""
Cotação do dólar, usada para converter filtros em R$ para o Scryfall, e a
estimativa de preço em R$ das cartas que ainda não têm preço da LigaMagic.
"""

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


# Estimativa do preço na LigaMagic a partir do preço em dólar do Scryfall.
# A LigaMagic é bem mais barata que dólar × cotação, principalmente nas cartas
# baratas (o Scryfall cota uma edição; a LigaMagic, o menor preço entre todas).
# Por faixa de preço em US$, quanto da conversão direta a LigaMagic cobra:
# a soma dos preços reais dividida pela soma das conversões, calculada com as
# cartas que já têm os dois preços (sem dados suficientes, os valores padrão,
# medidos em out/2026 com ~3.000 cartas).
ESTIMATE_BANDS = (Decimal("0.05"), Decimal("0.10"), Decimal("0.25"), Decimal("1"), Decimal("5"), None)
DEFAULT_FACTORS = (Decimal("0.26"), Decimal("0.25"), Decimal("0.34"), Decimal("0.88"), Decimal("0.82"), Decimal("0.55"))
MIN_SAMPLES = 50
FACTORS_CACHE_KEY = "c10magic:estimate_factors"


def _band(usd: Decimal) -> int:
    return next(i for i, top in enumerate(ESTIMATE_BANDS) if top is None or usd <= top)


def estimate_factors(rate) -> tuple:
    """Fator de cada faixa (ver ESTIMATE_BANDS), recalculado uma vez por dia."""

    factors = cache.get(FACTORS_CACHE_KEY)
    if factors is not None:
        return factors

    from cards.models import BrlPrice, CachedCard

    real = dict(BrlPrice.objects.exclude(price_brl=None).values_list("name", "price_brl"))
    sums = [[Decimal(0), Decimal(0), 0] for _ in ESTIMATE_BANDS]      # real, US$, quantas

    if real:
        for name, usd in CachedCard.objects.filter(name__in=list(real)).values_list("name", "data__prices__usd"):
            try:
                usd = Decimal(str(usd))
            except Exception:  # noqa: BLE001 - sem preço em dólar
                continue
            if usd <= 0:
                continue
            band = sums[_band(usd)]
            band[0] += real[name]
            band[1] += usd
            band[2] += 1

    factors = tuple(
        (real_sum / (usd_sum * rate)).quantize(Decimal("0.01")) if count >= MIN_SAMPLES else default
        for (real_sum, usd_sum, count), default in zip(sums, DEFAULT_FACTORS)
    )
    cache.set(FACTORS_CACHE_KEY, factors, 24 * 60 * 60)

    return factors


def estimate_brl(value, rate=None):
    """Preço provável na LigaMagic de uma carta que custa `value` dólares no Scryfall."""

    if value is None:
        return None

    value = Decimal(value)
    rate = rate or usd_brl()
    factor = estimate_factors(rate)[_band(value)] if value > 0 else Decimal(1)

    return max((value * rate * factor).quantize(Decimal("0.01")), Decimal("0.01") if value > 0 else Decimal(0))
