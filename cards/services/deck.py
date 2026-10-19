"""
Página Especialista: deck de Commander importado + sugestões.
"""

from dataclasses import dataclass, field
from decimal import Decimal

import requests
from django.db import transaction
from django.urls import reverse

from cards.models import COLOR_ORDER, CachedCard, Deck, DeckCard, TagIndex
from cards.services.catalog import (
    ResultCard,
    ScryfallResults,
    from_scryfall,
    get_card_data,
    get_cards_data,
    store_cards,
)
from cards.services.decklist import Entry, parse_decklist
from cards.services.edhrec import TYPE_LABELS, Pool, PoolCard, commander_pool
from cards.services.prices import known_prices, name_keys
from cards.services.scryfall import ScryfallClient, _same_name, card_fields
from cards.services.similar import PRIMARY_TYPES, ROLE_TAGS, build_reference


# Agrupamento das cartas do deck. Terreno vem antes de artefato/encantamento
# para "Artifact Land" ficar com os terrenos; criatura vem antes de tudo.
GROUP_PRIORITY = ["Creature", "Planeswalker", "Battle", "Land", "Instant", "Sorcery", "Artifact", "Enchantment"]

GROUP_LABELS = {"commander": "Comandante", **TYPE_LABELS, "other": "Outras"}

GROUP_ORDER = ["commander", "Creature", "Planeswalker", "Battle", "Instant", "Sorcery",
               "Artifact", "Enchantment", "Land", "other"]


def group_of(type_line: str) -> str:
    front = (type_line or "").split(" // ")[0].split("—")[0]

    for card_type in GROUP_PRIORITY:
        if card_type in front:
            return card_type

    return "other"


def is_basic_land(type_line: str) -> bool:
    return "Basic" in (type_line or "") and "Land" in (type_line or "")


def can_be_commander(data: dict) -> bool:
    type_line = data.get("type_line") or ""
    front = type_line.split(" // ")[0]
    text = (data.get("oracle_text") or "") + " ".join(
        face.get("oracle_text") or "" for face in data.get("card_faces") or []
    )

    return (
        ("Legendary" in front and ("Creature" in front or "Vehicle" in front or "Spacecraft" in front))
        or "Background" in front
        or "can be your commander" in text
    )


def identity_letters(colors) -> str:
    return "".join(c for c in COLOR_ORDER if c in colors)


# ============================================================
# IMPORTAÇÃO
# ============================================================

def _identifier(entry: Entry) -> dict:
    if entry.set_code and entry.collector_number:
        return {"set": entry.set_code, "collector_number": entry.collector_number}
    if entry.set_code:
        return {"name": entry.name, "set": entry.set_code}
    return {"name": entry.name}


def resolve_entries(entries: list[Entry], client: ScryfallClient | None = None):
    """[(entry, JSON do Scryfall ou None)]"""

    client = client or ScryfallClient()
    found = client.collection([_identifier(entry) for entry in entries])

    resolved = []

    for entry, data in zip(entries, found):
        # Edição + número que não batem com o nome: confia no nome
        if data and entry.collector_number and not _same_name(data, entry.name):
            data = None

        if data is None:
            # Nome aproximado: corrige erros de digitação e reconhece nomes
            # em português ("Fuga de Mana" -> Mana Leak)
            try:
                data = client.named_fuzzy(entry.name)
            except requests.RequestException:
                data = None

        resolved.append((entry, data))

    store_cards([data for _, data in resolved if data])

    return resolved


@transaction.atomic
def import_deck(text: str, client: ScryfallClient | None = None, deck: Deck | None = None) -> Deck:
    """Cria um deck com a lista (ou, com `deck`, substitui as cartas dele)."""

    entries = parse_decklist(text)

    if not entries:
        raise ValueError("Não encontrei nenhuma carta na lista.")

    merged = {}

    for entry, data in resolve_entries(entries, client):
        key = (data.get("oracle_id") or data["id"]) if data else "?" + entry.name.lower()

        if key in merged:
            card = merged[key]
            card.quantity += entry.quantity
            card.is_commander = card.is_commander or entry.is_commander
            continue

        fields = card_fields(data) if data else {}

        merged[key] = DeckCard(
            quantity=entry.quantity,
            input_name=entry.name[:255],
            input_line=entry.line[:500],
            name=(data or {}).get("name", "")[:255],
            scryfall_id=(data or {}).get("id", ""),
            oracle_id=fields.get("oracle_id", ""),
            is_commander=entry.is_commander,
            category=entry.category[:255],
        )

    if deck is None:
        deck = Deck.objects.create(raw_text=text)
    else:
        deck.cards.all().delete()
        deck.raw_text = text
        deck.build_report = None
        deck.cover_scryfall_id = ""
        deck.save()

    cards = list(merged.values())
    for position, card in enumerate(cards):
        card.deck = deck
        card.position = position

    DeckCard.objects.bulk_create(cards)

    update_deck_name(deck)

    return deck


def update_deck_name(deck: Deck):
    """Nome = comandante(s), a não ser que o usuário tenha dado um nome."""

    if deck.name_locked:
        deck.save(update_fields=["updated_at"])
        return

    commanders = [c.name or c.input_name for c in deck.cards.filter(is_commander=True)]
    deck.name = " + ".join(commanders)[:255]
    deck.save(update_fields=["name", "updated_at"])


def rename_deck(deck: Deck, name: str):
    """Nome vazio volta ao automático (o nome do comandante)."""

    name = (name or "").strip()[:255]
    deck.name_locked = bool(name)
    if name:
        deck.name = name
        deck.save(update_fields=["name", "name_locked", "updated_at"])
    else:
        deck.save(update_fields=["name_locked"])
        update_deck_name(deck)


@transaction.atomic
def clone_deck(deck: Deck) -> Deck:
    copy = Deck.objects.create(
        name=f"Cópia de {deck.name or 'deck'}"[:255],
        name_locked=True,
        raw_text=deck.raw_text,
        budget=deck.budget,
        build_report=deck.build_report,
        cover_scryfall_id=deck.cover_scryfall_id,
    )
    DeckCard.objects.bulk_create([
        DeckCard(
            deck=copy, position=card.position, quantity=card.quantity, input_name=card.input_name,
            input_line=card.input_line, name=card.name, scryfall_id=card.scryfall_id,
            oracle_id=card.oracle_id, is_commander=card.is_commander, category=card.category,
        )
        for card in deck.cards.all()
    ])
    return copy


def art_crop(data: dict | None) -> str:
    """Recorte só da arte da carta (para capas)."""

    if not data:
        return ""
    uris = data.get("image_uris") or ((data.get("card_faces") or [{}])[0].get("image_uris")) or {}
    return uris.get("art_crop") or uris.get("normal") or ""


def deck_summaries(decks) -> list[dict]:
    """Capa, cores e números de cada deck, para a grade de decks."""

    decks = list(decks)
    cards_by_deck = {deck.pk: list(deck.cards.all()) for deck in decks}

    wanted = set()
    for deck in decks:
        cards = cards_by_deck[deck.pk]
        wanted |= {c.scryfall_id for c in cards if c.is_commander and c.scryfall_id}
        if deck.cover_scryfall_id:
            wanted.add(deck.cover_scryfall_id)
        elif cards and cards[0].scryfall_id:
            wanted.add(cards[0].scryfall_id)

    datas = {c.scryfall_id: c.data for c in CachedCard.objects.filter(scryfall_id__in=wanted)}

    summaries = []
    for deck in decks:
        cards = cards_by_deck[deck.pk]
        commanders = [c for c in cards if c.is_commander]

        cover_id = deck.cover_scryfall_id or next(
            (c.scryfall_id for c in commanders + cards if c.scryfall_id), "")
        identity = identity_letters("".join(
            card_fields(datas[c.scryfall_id])["color_identity"]
            for c in commanders if c.scryfall_id in datas
        ))

        summaries.append({
            "deck": deck,
            "cover": art_crop(datas.get(cover_id)),
            "identity": identity,
            "commanders": [c.name or c.input_name for c in commanders],
            "card_count": sum(c.quantity for c in cards),
            "built": bool(deck.build_report),
        })

    return summaries


# ============================================================
# EXPORTAR
# ============================================================

EXPORT_FORMATS = {
    "ligamagic": ("LigaMagic", "txt"),
    "moxfield": ("Moxfield / MTG Arena", "txt"),
    "archidekt": ("Archidekt", "txt"),
    "texto": ("Texto simples", "txt"),
}


def export_deck(deck: Deck, fmt: str) -> str:
    cards = [c for c in deck.cards.all() if c.name or c.input_name]
    datas = {c.scryfall_id: c.data for c in CachedCard.objects.filter(
        scryfall_id__in=[c.scryfall_id for c in cards if c.scryfall_id])}

    def name(card):
        return card.name or card.input_name

    def printing(card):
        data = datas.get(card.scryfall_id) or {}
        return (data.get("set") or "").lower(), data.get("collector_number") or ""

    commanders = [c for c in cards if c.is_commander]
    others = [c for c in cards if not c.is_commander]

    if fmt == "moxfield":
        # Formato do MTG Arena, que o Moxfield também importa
        def line(card):
            set_code, number = printing(card)
            suffix = f" ({set_code.upper()}) {number}" if set_code and number else ""
            return f"{card.quantity} {name(card)}{suffix}"

        parts = []
        if commanders:
            parts += ["Commander"] + [line(c) for c in commanders] + [""]
        parts += ["Deck"] + [line(c) for c in others]
        return "\n".join(parts) + "\n"

    if fmt == "archidekt":
        def line(card):
            set_code, number = printing(card)
            text = f"{card.quantity}x {name(card)}"
            if set_code:
                text += f" ({set_code}){f' {number}' if number else ''}"
            category = "Commander{top}" if card.is_commander else (card.category or "")
            return text + (f" [{category}]" if category else "")

        return "\n".join(line(c) for c in commanders + others) + "\n"

    if fmt == "ligamagic":
        lines = []
        if commanders:
            lines += ["Comandante"] + [f"{c.quantity} {name(c)}" for c in commanders] + ["", "Deck"]
        lines += [f"{c.quantity} {name(c)}" for c in others]
        return "\n".join(lines) + "\n"

    # Texto simples: "1 Nome", comandante primeiro
    return "\n".join(f"{c.quantity} {name(c)}" for c in commanders + others) + "\n"



def set_commanders(deck: Deck, card_ids):
    deck.cards.update(is_commander=False)
    deck.cards.filter(pk__in=card_ids).update(is_commander=True)
    update_deck_name(deck)


def swap_card(deck_card: DeckCard, scryfall_id: str):
    data, _ = get_card_data(scryfall_id)

    if not data:
        raise ValueError("Carta não encontrada no Scryfall.")

    deck_card.name = data["name"][:255]
    deck_card.input_name = data["name"][:255]
    deck_card.scryfall_id = data["id"]
    deck_card.oracle_id = card_fields(data)["oracle_id"]
    deck_card.category = ""
    deck_card.save()

    deck_card.deck.save(update_fields=["updated_at"])

    return data


# ============================================================
# VISÃO DO DECK
# ============================================================

@dataclass
class DeckItem:
    card: DeckCard
    data: dict | None
    result: ResultCard | None
    group: str
    type_line: str = ""
    is_basic: bool = False
    unit_price: Decimal = Decimal(0)
    estimated: bool = True
    color_identity: str = ""

    @property
    def total_price(self):
        return self.unit_price * self.card.quantity


@dataclass
class DeckView:
    deck: Deck
    items: list[DeckItem]
    commanders: list[DeckItem]
    identity: str
    total: Decimal
    estimated_count: int
    card_count: int
    groups: list[tuple[str, str, list[DeckItem], int]] = field(default_factory=list)
    unresolved: list[DeckCard] = field(default_factory=list)
    type_counts: dict[str, int] = field(default_factory=dict)

    @property
    def commander_total(self):
        return sum((item.total_price for item in self.commanders), Decimal(0))

    @property
    def budget_total(self):
        """O que conta para o orçamento: o deck sem o(s) comandante(s)."""
        return self.total - self.commander_total

    @property
    def remaining(self):
        if self.deck.budget is None:
            return None
        return self.deck.budget - self.budget_total

    @property
    def budget_percent(self):
        if not self.deck.budget:
            return None
        return min(int(self.budget_total / self.deck.budget * 100), 100)

    @property
    def oracle_ids(self):
        return {item.card.oracle_id for item in self.items if item.card.oracle_id}

    @property
    def names(self):
        return {key for item in self.items for key in name_keys(item.card.name or item.card.input_name)}

    def in_deck(self, data: dict) -> bool:
        fields_oracle = card_fields(data)["oracle_id"]
        return fields_oracle in self.oracle_ids or any(
            key in self.names for key in name_keys(data.get("name", ""))
        )


def unit_price(result: ResultCard | None):
    """(preço unitário em R$, é estimativa?)"""

    if result is None:
        return Decimal(0), True
    if result.brl_status == "ok" and result.price_brl is not None:
        return result.price_brl, False
    if result.price_brl is not None:
        # Preço antigo da coleção enquanto atualiza
        return result.price_brl, True
    return result.brl_estimate or Decimal(0), True


def build_deck(deck: Deck, rate) -> DeckView:
    cards = list(deck.cards.all())
    datas = get_cards_data([c.scryfall_id for c in cards])

    resolved = [(card, datas.get(card.scryfall_id)) for card in cards]
    results = from_scryfall([data for _, data in resolved if data], rate)
    results_iter = iter(results)

    items = []

    for card, data in resolved:
        result = next(results_iter) if data else None
        fields = card_fields(data) if data else {}
        price, estimated = unit_price(result)

        # Terreno básico não custa nada (a LigaMagic também é tratada assim)
        if is_basic_land(fields.get("type_line", "")):
            price, estimated = Decimal(0), False

        items.append(DeckItem(
            card=card,
            data=data,
            result=result,
            group="commander" if card.is_commander else group_of(fields.get("type_line", "")),
            type_line=fields.get("type_line", ""),
            is_basic=is_basic_land(fields.get("type_line", "")),
            unit_price=price,
            estimated=estimated,
            color_identity=fields.get("color_identity", ""),
        ))

    commanders = [item for item in items if item.card.is_commander]
    identity = identity_letters("".join(item.color_identity for item in commanders))

    resolved_items = [item for item in items if item.data]

    groups = []
    for key in GROUP_ORDER:
        group_items = [item for item in resolved_items if item.group == key]
        if group_items:
            group_items.sort(key=lambda i: ((i.data or {}).get("cmc") or 0, i.card.name))
            groups.append((key, GROUP_LABELS[key], group_items, sum(i.card.quantity for i in group_items)))

    type_counts = {}
    for item in resolved_items:
        if not item.card.is_commander:
            type_key = group_of(item.type_line)
            type_counts[type_key] = type_counts.get(type_key, 0) + item.card.quantity

    return DeckView(
        deck=deck,
        items=items,
        commanders=commanders,
        identity=identity,
        total=sum((item.total_price for item in items), Decimal(0)),
        estimated_count=sum(1 for item in resolved_items if item.estimated and item.unit_price),
        card_count=sum(item.card.quantity for item in items),
        groups=groups,
        unresolved=[item.card for item in items if not item.data],
        type_counts=type_counts,
    )


def commander_candidates(view: DeckView) -> list[DeckItem]:
    return [item for item in view.items if item.data and can_be_commander(item.data)]


# ============================================================
# POOL DO EDHREC
# ============================================================

@dataclass
class PoolItem:
    result: ResultCard
    pool_card: PoolCard
    data: dict
    unit_price: Decimal = Decimal(0)

    @property
    def synergy_percent(self):
        return round(self.pool_card.synergy * 100) if self.pool_card.synergy is not None else None

    @property
    def inclusion_percent(self):
        return round(self.pool_card.inclusion * 100) if self.pool_card.inclusion is not None else None


@dataclass
class DeckPool:
    pool: Pool
    deck_counts: dict[str, int] = field(default_factory=dict)
    categories: list[tuple[str, str, list[PoolItem]]] = field(default_factory=list)
    by_id: dict[str, PoolItem] = field(default_factory=dict)

    @property
    def type_comparison(self):
        """[(rótulo, no deck, média do EDHREC)] na ordem do gráfico do EDHREC."""

        return [
            (TYPE_LABELS.get(card_type, card_type), self.deck_counts.get(card_type, 0), average)
            for card_type, average in self.pool.type_distribution.items()
        ]


def build_pool(view: DeckView, rate) -> DeckPool:
    names = [item.card.name for item in view.commanders if item.card.name]

    if not names:
        return DeckPool(pool=Pool(found=False))

    pool = commander_pool(names)
    deck_pool = DeckPool(pool=pool, deck_counts=view.type_counts)

    if not pool.found:
        return deck_pool

    unique = pool.cards
    datas = get_cards_data(list(unique))

    candidates = [
        data for sid, data in datas.items()
        if not view.in_deck(data) and not is_basic_land(data.get("type_line", ""))
    ]
    results = {result.name: result for result in from_scryfall(candidates, rate)}

    for sid, data in datas.items():
        result = results.get(data.get("name", ""))
        if result:
            deck_pool.by_id[sid] = PoolItem(result, unique[sid], data, unit_price(result)[0])

    for tag, label, cards in pool.categories:
        items = [deck_pool.by_id[c.scryfall_id] for c in cards if c.scryfall_id in deck_pool.by_id]
        if items:
            deck_pool.categories.append((tag, label, items))

    return deck_pool


# ============================================================
# SUGESTÕES PARA UMA CARTA DO DECK
# ============================================================

@dataclass
class Suggestion:
    result: ResultCard
    scryfall_id: str
    unit_price: Decimal
    difference: Decimal
    synergy_percent: int | None = None
    inclusion_percent: int | None = None
    from_pool: bool = False


@dataclass
class Suggestions:
    item: DeckItem
    role: str | None
    roles: list[str]
    tags: list            # Tag (label, text, count) da carta, para marcar/desmarcar
    selected_tags: list[str]
    selected_types: list[str]
    max_price: Decimal | None
    budget_max: Decimal | None
    user_max: Decimal | None
    pool_items: list[Suggestion]
    other_items: list[Suggestion]
    hidden_by_budget: int
    query: str
    error: str = ""


def _oracle_tags(oracle_ids) -> dict[str, set[str]]:
    return {
        row.target_id: set(row.label_list)
        for row in TagIndex.objects.filter(kind=TagIndex.ORACLE, target_id__in=list(oracle_ids))
    }


def suggestions_for(view: DeckView, deck_pool: DeckPool, deck_card: DeckCard, rate,
                    limit: int = 12, pool_limit: int = 8,
                    user_max_price: Decimal | None = None,
                    user_tags: list[str] | None = None,
                    user_types: list[str] | None = None) -> Suggestions:
    item = next(i for i in view.items if i.card.pk == deck_card.pk)

    ref = build_reference(item.data, rate)
    labels = {tag.label for tag in ref.tags}
    roles = [role for role in ROLE_TAGS if role in labels]
    role = roles[0] if roles else None

    # Tags obrigatórias: por padrão só o papel principal; o usuário pode
    # marcar/desmarcar qualquer tag da carta (inclusive nenhuma)
    if user_tags is None:
        selected = [role] if role else []
    else:
        selected = [label for label in dict.fromkeys(user_tags) if label in labels]

    # Tipos (qualquer um dos marcados). Padrão: nenhum quando há papel
    # conhecido; senão, o tipo da própria carta.
    valid_types = [card_type for card_type, _ in PRIMARY_TYPES]
    if user_types is None:
        types = [] if role or item.group not in valid_types else [item.group]
    else:
        types = [t for t in valid_types if t in user_types]

    # Marcadas primeiro, depois os papéis, depois as demais (mais específicas antes)
    tag_order = sorted(
        ref.tags,
        key=lambda tag: (tag.label not in selected, tag.label not in roles, tag.count or 0, tag.label),
    )

    budget_max = None
    if view.deck.budget is not None:
        # Trocar esta carta: libera o preço dela
        budget_max = max(view.deck.budget - view.budget_total + item.unit_price, Decimal(0))

    # Vale o menor entre o que cabe no orçamento e o limite digitado no modal
    limits = [value for value in (budget_max, user_max_price) if value is not None]
    max_price = min(limits) if limits else None

    hidden = 0

    def fits_budget(price):
        nonlocal hidden
        if max_price is not None and price > max_price:
            hidden += 1
            return False
        return True

    # ---- Do pool do EDHREC, com o mesmo papel (ou o mesmo tipo)
    pool_items = []
    pool_candidates = [
        p for p in deck_pool.by_id.values()
        if p.data.get("id") != item.card.scryfall_id
    ]

    if selected:
        tags = _oracle_tags(card_fields(p.data)["oracle_id"] for p in pool_candidates)
        required = set(selected)
        pool_candidates = [
            p for p in pool_candidates
            if required <= tags.get(card_fields(p.data)["oracle_id"], set())
        ]

    if types:
        pool_candidates = [
            p for p in pool_candidates
            if any(card_type in (p.data.get("type_line") or "") for card_type in types)
        ]

    pool_candidates.sort(key=lambda p: -(p.pool_card.synergy or 0))

    for p in pool_candidates:
        if len(pool_items) >= pool_limit:
            break
        if not fits_budget(p.unit_price):
            continue
        pool_items.append(Suggestion(
            result=p.result,
            scryfall_id=p.data["id"],
            unit_price=p.unit_price,
            difference=p.unit_price - item.unit_price,
            synergy_percent=p.synergy_percent,
            inclusion_percent=p.inclusion_percent,
            from_pool=True,
        ))

    # ---- Outras opções no Scryfall
    terms = [f"otag:{label}" for label in selected]

    if types:
        type_terms = [f"t:{card_type.lower()}" for card_type in types]
        terms.append(type_terms[0] if len(type_terms) == 1 else "(" + " or ".join(type_terms) + ")")
    terms.append(f"id<={view.identity.lower()}" if view.identity else "id:c")
    terms += ["f:commander", "game:paper"]
    if ref.oracle_id:
        terms.append(f"-oracleid:{ref.oracle_id}")
    if max_price is not None:
        terms.append(f"usd<={(max_price / rate).quantize(Decimal('0.01'))}")

    query = " ".join(terms)
    other_items = []
    error = ""

    shown = {s.scryfall_id for s in pool_items}
    shown_names = {s.result.name for s in pool_items}

    try:
        results = ScryfallResults(query, order="edhrec")
        datas = [
            data for data in results[0:min(results.count, 175)]
            if not view.in_deck(data) and data["id"] not in shown and data.get("name") not in shown_names
        ]
    except requests.RequestException as exc:
        datas, error = [], f"Não foi possível consultar o Scryfall ({exc.__class__.__name__})."
    except ValueError as exc:
        datas, error = [], str(exc)

    for result, data in zip(from_scryfall(datas[: limit * 2], rate), datas[: limit * 2]):
        if len(other_items) >= limit:
            break
        price = unit_price(result)[0]
        if not fits_budget(price):
            continue
        pool_card = deck_pool.by_id.get(data["id"])
        other_items.append(Suggestion(
            result=result,
            scryfall_id=data["id"],
            unit_price=price,
            difference=price - item.unit_price,
            synergy_percent=pool_card.synergy_percent if pool_card else None,
            inclusion_percent=pool_card.inclusion_percent if pool_card else None,
        ))

    return Suggestions(
        item=item,
        role=role,
        roles=roles,
        tags=tag_order,
        selected_tags=selected,
        selected_types=types,
        max_price=max_price,
        budget_max=budget_max,
        user_max=user_max_price,
        pool_items=pool_items,
        other_items=other_items,
        hidden_by_budget=hidden,
        query=query,
        error=error,
    )
