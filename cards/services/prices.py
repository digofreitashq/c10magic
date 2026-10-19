"""
Preços em reais (LigaMagic) para qualquer carta.

- Cartas da coleção: o preço fica no próprio Card.
- Demais cartas: o preço fica em BrlPrice.

Nos dois casos o preço vale por uma semana. Os preços que faltam são
buscados por uma fila em segundo plano, na ordem em que aparecem na tela.
"""

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import requests
from django.db import close_old_connections
from django.utils import timezone

from cards.models import BrlPrice, Card
from cards.services.ligamagic import LigaMagicScraper, PriceNotFound


log = logging.getLogger(__name__)

# Depois de uma falha de rede, espera antes de tentar a mesma carta
RETRY_AFTER_SECONDS = 10 * 60


@dataclass
class Quote:
    price: Decimal | None
    url: str
    updated_at: datetime
    error: str = ""


def name_keys(name: str) -> list[str]:
    """Nome completo e, para cartas de duas faces, o nome da frente."""

    full = name.strip().lower()
    front = full.split(" // ")[0].strip()

    return [full] if front == full else [full, front]


def collection_card(name: str):
    return Card.objects.filter(name_normalized__in=name_keys(name)).first()


def known_prices(names) -> dict[str, Quote | None]:
    """Preços ainda válidos (menos de uma semana) para cada nome."""

    names = list(dict.fromkeys(names))
    keys = {key for name in names for key in name_keys(name)}
    limit = Card.stale_before()

    found = {}

    for card in Card.objects.filter(name_normalized__in=keys, ligamagic_updated_at__gte=limit):
        found[card.name_normalized] = Quote(
            card.price_brl, card.ligamagic_url, card.ligamagic_updated_at, card.ligamagic_error
        )

    for price in BrlPrice.objects.filter(name_normalized__in=keys, updated_at__gte=limit):
        found.setdefault(
            price.name_normalized,
            Quote(price.price_brl, price.url, price.updated_at, price.error),
        )

    return {
        name: next((found[k] for k in name_keys(name) if k in found), None)
        for name in names
    }


def fetch_price(name: str, scraper: LigaMagicScraper | None = None, updater=None):
    """Busca o preço na LigaMagic agora. Erros de rede são propagados."""

    card = collection_card(name)

    if card is not None:
        from cards.services.updater import CardUpdater

        updater = updater or CardUpdater()
        if scraper:
            updater.ligamagic = scraper
        updater.update_ligamagic(card)
        return

    scraper = scraper or LigaMagicScraper()

    try:
        result = scraper.get_price(name)
        price, url, error = result["price_brl"], result["url"] or "", ""
        if price is None:
            error = "Sem ofertas."
    except PriceNotFound as exc:
        price, url = None, scraper.find_card_url(name)
        error = f"Preço não encontrado ({exc})."[:255]

    BrlPrice.objects.update_or_create(
        name_normalized=name.strip().lower(),
        defaults={
            "name": name.strip(),
            "price_brl": price,
            "url": url,
            "error": error,
            "updated_at": timezone.now(),
        },
    )


class PriceQueue:

    def __init__(self):
        self._cond = threading.Condition()
        self._pending = deque()
        self._queued = set()
        self._failed = {}
        self._thread = None
        self.current = ""

    def request(self, names):
        """Coloca os nomes na frente da fila, mantendo a ordem."""

        now = time.monotonic()

        with self._cond:
            for name in reversed(list(dict.fromkeys(names))):
                key = name.strip().lower()

                if now - self._failed.get(key, -RETRY_AFTER_SECONDS) < RETRY_AFTER_SECONDS:
                    continue

                if key in self._queued:
                    try:
                        self._pending.remove(name)
                    except ValueError:
                        # Está sendo buscada agora
                        continue
                else:
                    self._queued.add(key)

                self._pending.appendleft(name)

            self._ensure_thread()
            self._cond.notify()

    def has_failed(self, name):
        key = name.strip().lower()
        return time.monotonic() - self._failed.get(key, -RETRY_AFTER_SECONDS) < RETRY_AFTER_SECONDS

    def size(self):
        return len(self._pending)

    def _ensure_thread(self):
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="c10magic-prices", daemon=True)
            self._thread.start()

    def _run(self):
        scraper = LigaMagicScraper()

        while True:
            with self._cond:
                while not self._pending:
                    self._cond.wait()

                name = self._pending.popleft()

            key = name.strip().lower()
            self.current = name

            try:
                close_old_connections()

                # Pode ter sido buscada por outro caminho enquanto esperava
                if known_prices([name])[name] is None:
                    fetch_price(name, scraper=scraper)

            except requests.RequestException as exc:
                log.warning("LigaMagic indisponível para %s: %s", name, exc)
                self._failed[key] = time.monotonic()
            except Exception:  # noqa: BLE001 - a fila não pode morrer
                log.exception("Erro buscando preço de %s", name)
                self._failed[key] = time.monotonic()
            finally:
                with self._cond:
                    self._queued.discard(key)
                self.current = ""
                close_old_connections()


price_queue = PriceQueue()
