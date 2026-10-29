"""
Cartas similares a uma carta de referência.

Similar = tem as tags de função escolhidas (oracle tags do Scryfall Tagger),
as cores escolhidas e, opcionalmente, o mesmo tipo principal, até um preço
máximo em reais.
"""

from dataclasses import dataclass, field
from decimal import Decimal

import requests
from django.db.models import F, Q

from cards.models import COLOR_ORDER, Card, TagIndex
from cards.search import color_q, parse_query
from cards.services.exchange import estimate_brl
from cards.services.prices import fetch_price, known_prices
from cards.services.scryfall import card_fields
from cards.services.tags import tag_counts, tags_for


# Ordem de prioridade: o primeiro presente é o "tipo principal"
PRIMARY_TYPES = [
    ("Creature", "Criatura"),
    ("Planeswalker", "Planeswalker"),
    ("Battle", "Batalha"),
    ("Instant", "Mágica instantânea"),
    ("Sorcery", "Feitiço"),
    ("Artifact", "Artefato"),
    ("Enchantment", "Encantamento"),
    ("Land", "Terreno"),
]

# Tags que só aparecem em pouquíssimas cartas não ajudam a achar similares
MIN_TAG_COUNT = 3

# Tags de "papel no deck", da mais específica para a mais genérica.
# A tag marcada de início é a primeira desta lista que a carta tiver: a
# contagem sozinha não separa papel (burn, mana-dork) de detalhe
# (full-refund) ou de categoria sem função (cycle, naming-scheme).
ROLE_TAGS = [
    "counterspell", "soft-counter",
    "mana-dork", "mana-rock", "land-ramp", "ritual", "cost-reducer",
    "sweeper", "removal-exile", "removal-destroy", "removal-bounce", "edict", "burn",
    "artifact-removal", "enchantment-removal", "graveyard-hate", "hand-disruption",
    "tutor-creature", "tutor-land", "tutor-artifact", "tutor",
    "reanimate", "regrowth", "recursion",
    "wheel", "impulse-draw", "loot", "rummage", "cantrip", "draw", "card-advantage",
    "extra-turn", "extra-combat", "clone", "copy", "blink",
    "protects-creature", "protection", "fog",
    "sac-outlet", "anthem", "combat-trick", "bite", "untapper", "landfall", "tax",
    "mill", "self-mill", "scry", "surveil", "life-gain",
    "removal", "ramp", "mana-producer", "evasion",
]


@dataclass
class Tag:
    label: str
    text: str
    count: int | None


@dataclass
class Reference:
    data: dict
    name: str
    oracle_id: str
    colors: str
    main_type: str
    main_type_label: str
    image_url: str
    mana_cost: str
    type_line: str
    cmc: float | None
    price_brl: Decimal | None
    brl_estimate: Decimal | None
    tags: list[Tag] = field(default_factory=list)
    tags_ready: bool = True

    @property
    def reference_price(self):
        return self.price_brl if self.price_brl is not None else self.brl_estimate

    def defaults(self) -> dict:
        return {
            "tags": default_tags(self.tags),
            "colors": list(self.colors),
            "only_ref_colors": True,
            "card_types": [self.main_type] if self.main_type else [],
            "max_price": self.reference_price,
            "cmc_diff": None,
            "only_owned": False,
            "sort": "edhrec",
        }


def default_tags(tags: list[Tag]) -> list[str]:
    """
    Uma tag só (duas juntas costumam zerar os resultados): o papel mais
    específico da carta. Sem nenhum papel conhecido, nenhuma tag.
    """

    labels = {tag.label for tag in tags}

    return next(([role] for role in ROLE_TAGS if role in labels), [])


def main_type(type_line: str):
    front = (type_line or "").split(" // ")[0].split("—")[0]

    for english, label in PRIMARY_TYPES:
        if english in front:
            return english, label

    return "", ""


def build_reference(data: dict, rate) -> Reference:
    fields = card_fields(data)
    name = data.get("name", "")

    quote = known_prices([name])[name]

    if quote is None:
        # Uma carta só: vale esperar ~1 s pela LigaMagic
        try:
            fetch_price(name)
            quote = known_prices([name])[name]
        except requests.RequestException:
            pass

    price_usd = Decimal(fields["price_usd"]) if fields["price_usd"] else None
    english_type, type_label = main_type(fields["type_line"])

    tag_info = tags_for(fields["oracle_id"])
    labels = [label for label, _ in tag_info[TagIndex.ORACLE]]
    counts = tag_counts(labels)

    tags = [
        Tag(label, text, counts.get(label))
        for label, text in tag_info[TagIndex.ORACLE]
        if counts.get(label) is None or counts[label] >= MIN_TAG_COUNT
    ]
    tags.sort(key=lambda tag: (tag.count is None, tag.count or 0, tag.label))

    return Reference(
        data=data,
        name=name,
        oracle_id=fields["oracle_id"],
        colors=fields["colors"],
        main_type=english_type,
        main_type_label=type_label,
        image_url=fields["image_url"],
        mana_cost=fields["mana_cost"],
        type_line=fields["type_line"],
        cmc=fields["cmc"],
        price_brl=quote.price if quote else None,
        brl_estimate=estimate_brl(price_usd, rate) if price_usd is not None else None,
        tags=tags,
        tags_ready=tag_info["oracle_ready"],
    )


def cmc_range(ref: Reference, values: dict):
    """(mínimo, máximo) de valor de mana, ou None quando não há limite."""

    diff = values.get("cmc_diff")

    if diff is None or ref.cmc is None:
        return None

    cmc = Decimal(str(ref.cmc))
    diff = Decimal(diff)

    return max(cmc - diff, Decimal(0)), cmc + diff


def _number(value: Decimal) -> str:
    return f"{value.normalize():f}"


def _letters(colors) -> str:
    return "".join(c for c in COLOR_ORDER if c in colors).lower()


def scryfall_query(ref: Reference, values: dict, rate) -> str:
    terms = [f"otag:{label}" for label in values.get("tags") or []]

    required = _letters(values.get("colors") or [])
    if required:
        terms.append(f"c>={required}")

    if values.get("only_ref_colors"):
        terms.append(f"c<={_letters(ref.colors)}" if ref.colors else "c:c")

    card_types = [t.lower() for t in values.get("card_types") or []]
    if card_types:
        terms.append(
            f"t:{card_types[0]}" if len(card_types) == 1
            else "(" + " or ".join(f"t:{t}" for t in card_types) + ")"
        )

    if values.get("max_price") is not None:
        usd = (Decimal(values["max_price"]) / rate).quantize(Decimal("0.01"))
        terms.append(f"usd<={usd}")

    bounds = cmc_range(ref, values)
    if bounds:
        low, high = bounds
        if low == high:
            terms.append(f"mv={_number(low)}")
        else:
            if low > 0:
                terms.append(f"mv>={_number(low)}")
            terms.append(f"mv<={_number(high)}")

    # Só cartas de papel (a LigaMagic não tem as digitais) e sem a própria carta
    terms.append("game:paper")
    if ref.oracle_id:
        terms.append(f"-oracleid:{ref.oracle_id}")

    return " ".join(terms)


def collection_queryset(ref: Reference, values: dict):
    q = Q()

    tags = values.get("tags") or []
    if tags:
        q &= parse_query(" ".join(f"otag:{label}" for label in tags))

    required = "".join(values.get("colors") or [])
    if required:
        q &= color_q("colors", required, "all")

    if values.get("only_ref_colors"):
        q &= color_q("colors", ref.colors or "C", "atmost")

    card_types = values.get("card_types") or []
    if card_types:
        types_q = Q()
        for card_type in card_types:
            types_q |= Q(type_line__icontains=card_type)
        q &= types_q

    if values.get("max_price") is not None:
        q &= Q(price_brl__lte=values["max_price"])

    bounds = cmc_range(ref, values)
    if bounds:
        q &= Q(cmc__gte=bounds[0], cmc__lte=bounds[1])

    cards = Card.objects.filter(q)

    if ref.oracle_id:
        cards = cards.exclude(oracle_id=ref.oracle_id)

    sort = values.get("sort") or "edhrec"

    order = {
        "edhrec": F("raw_scryfall__edhrec_rank").asc(nulls_last=True),
        "price_brl": F("price_brl").asc(nulls_last=True),
        "cmc": F("cmc").asc(nulls_last=True),
        "name": F("name_normalized").asc(),
    }[sort]

    return cards.order_by(order, "name_normalized")


SCRYFALL_SORT = {
    "edhrec": ("edhrec", "auto"),
    "price_brl": ("usd", "asc"),
    "cmc": ("cmc", "asc"),
    "name": ("name", "auto"),
}
