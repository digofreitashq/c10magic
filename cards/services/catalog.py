"""
Busca na base completa do Scryfall, marcando as cartas da coleção.
"""

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import cached_property

from cards.models import CachedCard, Card
from cards.services.exchange import estimate_brl
from cards.services.prices import Quote, known_prices, name_keys
from cards.services.scryfall import SCRYFALL_PAGE_SIZE, ScryfallClient, card_fields


# ============================================================
# CACHE DE CARTAS
# ============================================================

def _oracle_id(data: dict) -> str:
    faces = data.get("card_faces") or [{}]
    return data.get("oracle_id") or faces[0].get("oracle_id") or ""


def store_cards(cards: list[dict]):
    """Guarda o JSON das cartas vistas (vale por uma semana)."""

    now = timezone.now()

    CachedCard.objects.bulk_create(
        [
            CachedCard(
                scryfall_id=data["id"],
                oracle_id=_oracle_id(data),
                name=data.get("name", ""),
                name_normalized=data.get("name", "").lower(),
                data=data,
                updated_at=now,
            )
            for data in cards
            if data.get("id")
        ],
        update_conflicts=True,
        unique_fields=["scryfall_id"],
        update_fields=["oracle_id", "name", "name_normalized", "data", "updated_at"],
    )


def get_card_data(scryfall_id: str, client: ScryfallClient | None = None):
    """JSON da carta: do banco se tiver menos de uma semana, senão do Scryfall."""

    cached = CachedCard.objects.filter(scryfall_id=scryfall_id).first()

    if cached and not cached.is_stale():
        return cached.data, cached.updated_at

    data = (client or ScryfallClient()).get_by_id(scryfall_id)

    if not data:
        return (cached.data, cached.updated_at) if cached else (None, None)

    store_cards([data])

    return data, timezone.now()


def get_cards_data(scryfall_ids, client: ScryfallClient | None = None) -> dict[str, dict]:
    """
    {scryfall_id: JSON} para várias cartas de uma vez.

    Usa o banco para o que tiver menos de uma semana e busca o resto em
    lote (/cards/collection). Cartas que o Scryfall não devolveu ficam com
    o JSON antigo, se houver.
    """

    ids = list(dict.fromkeys(i for i in scryfall_ids if i))
    cached = {c.scryfall_id: c for c in CachedCard.objects.filter(scryfall_id__in=ids)}

    result = {sid: c.data for sid, c in cached.items()}
    missing = [sid for sid in ids if sid not in cached or cached[sid].is_stale()]

    if missing:
        fetched = [
            data for data in (client or ScryfallClient()).collection([{"id": sid} for sid in missing])
            if data
        ]
        store_cards(fetched)
        result.update({data["id"]: data for data in fetched})

    return result


def get_cards_by_name(names, client: ScryfallClient | None = None) -> dict[str, dict]:
    """{nome pedido: JSON} — banco primeiro (menos de uma semana), resto em lote."""

    names = list(dict.fromkeys(n for n in names if n))
    limit = Card.stale_before()

    cached = {}
    for row in CachedCard.objects.filter(
        name_normalized__in=[n.lower() for n in names], updated_at__gte=limit
    ):
        cached.setdefault(row.name_normalized, row.data)

    result = {name: cached[name.lower()] for name in names if name.lower() in cached}
    missing = [name for name in names if name not in result]

    if missing:
        found = (client or ScryfallClient()).collection([{"name": name} for name in missing])
        store_cards([data for data in found if data])
        result.update({name: data for name, data in zip(missing, found) if data})

    return result


# ============================================================
# RESULTADOS PAGINÁVEIS
# ============================================================

class ScryfallResults:
    """
    Sequência "preguiçosa" sobre /cards/search, para usar com o Paginator.

    O Scryfall devolve 175 cartas por página; cada página é guardada em
    cache e só as páginas necessárias para o pedaço pedido são buscadas.
    """

    def __init__(self, query, unique="cards", order="name", direction="auto", client=None):
        self.query = query
        self.unique = unique
        self.order = order
        self.direction = direction
        self.client = client or ScryfallClient()

    def _cache_key(self, page):
        raw = json.dumps([self.query, self.unique, self.order, self.direction, page])
        return "c10magic:sf:" + hashlib.sha1(raw.encode()).hexdigest()

    def _page(self, page: int) -> dict:
        key = self._cache_key(page)
        body = cache.get(key)

        if body is None:
            body = self.client.search_page(
                self.query, page, self.unique, self.order, self.direction
            )
            body = {
                "data": body.get("data", []),
                "total_cards": body.get("total_cards", 0),
                "warnings": body.get("warnings", []),
            }
            store_cards(body["data"])
            cache.set(key, body, settings.SCRYFALL_SEARCH_CACHE_SECONDS)

        return body

    @cached_property
    def count(self) -> int:
        return self._page(1)["total_cards"]

    @property
    def warnings(self):
        return self._page(1)["warnings"]

    def __len__(self):
        return self.count

    def __getitem__(self, index):
        if not isinstance(index, slice):
            return self[index:index + 1][0]

        start, stop, _ = index.indices(self.count)
        items = []
        position = start

        while position < stop:
            page = position // SCRYFALL_PAGE_SIZE + 1
            offset = position % SCRYFALL_PAGE_SIZE
            data = self._page(page)["data"]

            chunk = data[offset: offset + (stop - position)]
            if not chunk:
                break

            items.extend(chunk)
            position += len(chunk)

        return items


# ============================================================
# CARTA EXIBIDA NA TELA
# ============================================================

@dataclass
class ResultCard:
    name: str
    detail_url: str
    image_url: str = ""
    image_url_back: str = ""
    mana_cost: str = ""
    type_line: str = ""
    set_code: str = ""
    collector_number: str = ""
    rarity: str = ""
    price_usd: Decimal | None = None
    price_brl: Decimal | None = None
    brl_estimate: Decimal | None = None
    # ok: preço da LigaMagic | pending: buscando | none: LigaMagic sem preço
    brl_status: str = "pending"
    owned: int = 0
    owned_print: bool = False
    owned_url: str = ""
    loading: bool = False
    extra: dict = field(default_factory=dict)


class Collection:
    """Índice da coleção para marcar resultados do Scryfall."""

    def __init__(self, cards: list[dict]):
        oracle_ids = {_oracle_id(d) for d in cards} - {""}
        names = {key for d in cards for key in name_keys(d.get("name", ""))}

        self.by_oracle = {}
        self.by_name = {}

        for card in Card.objects.filter(Q(oracle_id__in=oracle_ids) | Q(name_normalized__in=names)):
            if card.oracle_id:
                self.by_oracle.setdefault(card.oracle_id, []).append(card)
            self.by_name.setdefault(card.name_normalized, []).append(card)

    def owned(self, data: dict) -> list[Card]:
        cards = self.by_oracle.get(_oracle_id(data))

        if cards:
            return cards

        for key in name_keys(data.get("name", "")):
            if key in self.by_name:
                return self.by_name[key]

        return []


def _brl_fields(quote, price_usd, rate):
    estimate = estimate_brl(price_usd, rate) if price_usd is not None else None

    if quote is None:
        return {"brl_status": "pending", "brl_estimate": estimate}

    if quote.price is None:
        return {"brl_status": "none", "brl_estimate": estimate}

    return {"brl_status": "ok", "price_brl": quote.price, "brl_estimate": estimate}


def from_scryfall(cards: list[dict], rate) -> list[ResultCard]:
    collection = Collection(cards)
    quotes = known_prices([d.get("name", "") for d in cards])

    results = []

    for data in cards:
        fields = card_fields(data)
        owned = collection.owned(data)

        price_usd = Decimal(fields["price_usd"]) if fields["price_usd"] else None

        results.append(ResultCard(
            name=data.get("name", ""),
            detail_url=reverse("cards:scryfall_detail", args=[data["id"]]),
            image_url=fields["image_url"],
            image_url_back=fields["image_url_back"],
            mana_cost=fields["mana_cost"],
            type_line=fields["type_line"],
            set_code=fields["set_code"],
            collector_number=fields["collector_number"],
            rarity=fields["rarity"],
            price_usd=price_usd,
            owned=sum(c.quantity for c in owned),
            owned_print=any(c.scryfall_id == data["id"] for c in owned),
            owned_url=reverse("cards:detail", args=[owned[0].pk]) if owned else "",
            **_brl_fields(quotes[data.get("name", "")], price_usd, rate),
        ))

    return results


def from_collection(cards, rate) -> list[ResultCard]:
    results = []

    for card in cards:
        quote = None
        if not card.ligamagic_is_stale():
            quote = Quote(card.price_brl, card.ligamagic_url, card.ligamagic_updated_at)

        results.append(ResultCard(
            name=card.name,
            detail_url=reverse("cards:detail", args=[card.pk]),
            image_url=card.image_url,
            image_url_back=card.image_url_back,
            mana_cost=card.mana_cost,
            type_line=card.type_line,
            set_code=card.set_code or card.hint_set_code,
            collector_number=card.collector_number,
            rarity=card.rarity,
            price_usd=card.price_usd,
            owned=card.quantity,
            owned_print=True,
            owned_url=reverse("cards:detail", args=[card.pk]),
            loading=card.scryfall_updated_at is None,
            **_brl_fields(quote, card.price_usd, rate),
        ))

        # Preço antigo ainda é melhor que nada enquanto atualiza
        if quote is None and card.price_brl is not None:
            results[-1].price_brl = card.price_brl

    return results
