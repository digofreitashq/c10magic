"""
Carregamento e atualização das cartas.

Regra: depois que os dados de uma fonte (Scryfall ou LigaMagic) são
carregados, eles só são buscados de novo quando tiverem mais que
settings.CARD_REFRESH_DAYS dias (uma semana).
"""

import logging
import threading

import requests
from django.db import close_old_connections, connection, transaction
from django.db.models import F
from django.utils import timezone

from cards.models import BrlPrice, Card
from cards.services.importer import read_collection
from cards.services.ligamagic import LigaMagicScraper, PriceNotFound
from cards.services.scryfall import ScryfallClient, card_fields


log = logging.getLogger(__name__)


# ============================================================
# COLEÇÃO
# ============================================================

def add_card(name: str, quantity: int = 1, set_code: str = "", collector_number: str = ""):
    """Adiciona a carta (ou soma a quantidade, se já existir)."""

    name = name.strip()
    normalized = name.lower()

    card, created = Card.objects.get_or_create(
        name_normalized=normalized,
        defaults={
            "name": name,
            "quantity": quantity,
            "hint_set_code": set_code,
            "hint_collector_number": collector_number,
        },
    )

    if not created:
        card.quantity += quantity

        update_fields = ["quantity"]

        if set_code and not card.hint_set_code:
            card.hint_set_code = set_code
            card.hint_collector_number = collector_number
            update_fields += ["hint_set_code", "hint_collector_number"]

        card.save(update_fields=update_fields)

    return card, created


def import_csv_content(content: str) -> dict:
    rows = read_collection(content)

    created = 0

    for row in rows:
        _, is_new = add_card(
            row["name"],
            row["quantity"],
            row["set_code"],
            row["collector_number"],
        )
        created += is_new

    return {"rows": len(rows), "created": created}


@transaction.atomic
def clear_collection() -> int:
    """
    Remove todas as cartas da coleção.

    Os preços da LigaMagic ainda válidos vão para BrlPrice, para a busca
    geral continuar mostrando o preço sem consultar a LigaMagic de novo.
    """

    for card in Card.objects.filter(ligamagic_updated_at__gte=Card.stale_before()):
        BrlPrice.objects.update_or_create(
            name_normalized=card.name_normalized,
            defaults={
                "name": card.name,
                "price_brl": card.price_brl,
                "url": card.ligamagic_url,
                "error": card.ligamagic_error,
                "updated_at": card.ligamagic_updated_at,
            },
        )

    removed, _ = Card.objects.all().delete()

    return removed


# ============================================================
# ATUALIZAÇÃO DE UMA CARTA
# ============================================================

class CardUpdater:

    def __init__(self):
        self.scryfall = ScryfallClient()
        self.ligamagic = LigaMagicScraper()

    def update_scryfall(self, card: Card):
        try:
            data = self.scryfall.get_card(
                card.name,
                card.hint_set_code or None,
                card.hint_collector_number or None,
            )
        except requests.RequestException as exc:
            # Falha de rede: não marca como atualizada, tenta de novo depois
            card.scryfall_error = str(exc)[:255]
            card.save(update_fields=["scryfall_error"])
            return False

        if not data:
            card.scryfall_error = "Carta não encontrada no Scryfall."
        else:
            for field, value in card_fields(data).items():
                setattr(card, field, value)
            card.scryfall_error = ""

        card.scryfall_updated_at = timezone.now()
        card.save()

        return bool(data)

    def update_ligamagic(self, card: Card):
        try:
            result = self.ligamagic.get_price(card.name)
        except requests.RequestException as exc:
            card.ligamagic_error = str(exc)[:255]
            card.save(update_fields=["ligamagic_error"])
            return False
        except PriceNotFound as exc:
            card.ligamagic_url = self.ligamagic.find_card_url(card.name)
            card.ligamagic_error = f"Preço não encontrado ({exc})."[:255]
        else:
            card.price_brl = result["price_brl"]
            card.price_brl_foil = result["foil_price_brl"]
            card.ligamagic_url = result["url"] or ""
            card.ligamagic_error = (
                "" if result["price_brl"] is not None else "Sem ofertas."
            )

        card.ligamagic_updated_at = timezone.now()
        card.save()

        return card.price_brl is not None

    def refresh(self, card: Card, force: bool = False) -> dict:
        """Atualiza só as fontes vencidas (ou todas, com force=True)."""

        done = {"scryfall": False, "ligamagic": False}

        if force or card.scryfall_is_stale():
            self.update_scryfall(card)
            done["scryfall"] = True

        if force or card.ligamagic_is_stale():
            self.update_ligamagic(card)
            done["ligamagic"] = True

        return done


def refresh_card(card: Card, force: bool = False) -> dict:
    return CardUpdater().refresh(card, force=force)


def stale_cards():
    return Card.objects.filter(Card.stale_q()).order_by(
        # Primeiro as que nunca foram carregadas
        F("scryfall_updated_at").asc(nulls_first=True),
        "name_normalized",
    )


# ============================================================
# ATUALIZAÇÃO EM SEGUNDO PLANO
# ============================================================

class BackgroundUpdater:
    """
    Uma única thread que percorre as cartas vencidas.

    Serve para o uso local da interface web: importar um CSV e
    continuar navegando enquanto os dados chegam.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()

        self.total = 0
        self.done = 0
        self.current = ""
        self.started_at = None
        self.finished_at = None
        self.last_error = ""

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def status(self):
        return {
            "running": self.running,
            "total": self.total,
            "done": self.done,
            "current": self.current,
            "pending": stale_cards().count(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "last_error": self.last_error,
        }

    def start(self):
        with self._lock:
            if self.running:
                return False

            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="c10magic-updater", daemon=True
            )
            self._thread.start()

            return True

    def stop(self):
        self._stop.set()

    def _run(self):
        close_old_connections()

        self.started_at = timezone.now()
        self.finished_at = None
        self.done = 0
        self.last_error = ""

        try:
            ids = list(stale_cards().values_list("id", flat=True))
            self.total = len(ids)

            updater = CardUpdater()

            for card_id in ids:
                if self._stop.is_set():
                    break

                card = Card.objects.filter(id=card_id).first()

                if card is None:
                    continue

                self.current = card.name

                try:
                    updater.refresh(card)
                except Exception as exc:  # noqa: BLE001 - não derruba o lote
                    log.exception("Erro atualizando %s", card.name)
                    self.last_error = f"{card.name}: {exc}"

                self.done += 1

        finally:
            self.current = ""
            self.finished_at = timezone.now()
            connection.close()


background = BackgroundUpdater()
