"""
Perfil de um comandante, tirado dos decks que as pessoas montam com ele.

Fontes, na ordem:

1. Deck médio do EDHREC (average-decks/<comandante>): proporção de cada papel
   (ramp, compra, remoção, board wipe) pelas tags de função das cartas.
2. Sem deck médio: as cartas mais jogadas com o comandante (pool do EDHREC).
3. Sem nada: proporções padrão.

A curva de mana e a quantidade de criaturas vêm da página do comandante no
EDHREC. Temas são tags muito mais frequentes nesses decks do que no Magic em
geral (ex.: "sacrifice-outlet" num deck de aristocratas).
"""

import math
import re
from dataclasses import dataclass, field

import requests

from cards.models import TagIndex, TagStat
from cards.services.catalog import get_cards_by_name
from cards.services.edhrec import Pool, commander_path, get_page
from cards.services.scryfall import card_fields


# (chave, rótulo, tags que identificam o papel, quantidade padrão em 59 mágicas)
ROLES = [
    ("ramp", "Ramp", {"ramp", "mana-ramp", "mana-rock", "mana-dork", "land-ramp", "mana-producer",
                      "cost-reducer", "ritual"}, 7),
    ("draw", "Compra de cartas", {"draw", "card-advantage", "impulse-draw", "wheel", "loot", "rummage"}, 9),
    ("removal", "Remoção", {"removal", "spot-removal", "creature-removal", "removal-exile", "removal-destroy",
                            "removal-bounce", "edict", "burn-creature", "artifact-removal",
                            "enchantment-removal"}, 8),
    ("sweeper", "Board wipe", {"sweeper", "mass-removal"}, 2),
]
ROLE_LABELS = {key: label for key, label, _, _ in ROLES}

# Prioridade: sinergia (cartas fora das cotas, None) > remoção > compra > board wipe > ramp.
# Usada para preencher as cotas, cortar quando não cabem e decidir onde
# economizar (ou gastar a sobra) do orçamento.
ROLE_PRIORITY = ["removal", "draw", "sweeper", "ramp"]

# Quando as cotas não cabem nas vagas: quanto cada papel perde por vez
# (ramp perde mais rápido; remoção, mais devagar) e o mínimo de cada um
ROLE_CUT_WEIGHT = {"ramp": 1.0, "sweeper": 0.7, "draw": 0.5, "removal": 0.35}
ROLE_MINIMUM = {"ramp": 3, "sweeper": 1, "draw": 3, "removal": 3}


def trim_roles(targets: dict[str, int], cap: int) -> dict[str, int]:
    """
    Corta uma carta por vez até caber. Corta sempre da cota que, proporcional
    ao tamanho original e ponderada pela prioridade, ainda mantém mais: no
    fim, o ramp perde a maior fração e a remoção, a menor.
    """

    original = dict(targets)
    targets = dict(targets)

    while sum(targets.values()) > cap:
        cuttable = {
            key: ROLE_CUT_WEIGHT.get(key, 1.0) * value / original[key]
            for key, value in targets.items()
            if value > ROLE_MINIMUM.get(key, 0)
        }
        if not cuttable:
            break
        targets[max(cuttable, key=cuttable.get)] -= 1

    return targets

# Faixas de valor de mana: 1 (inclui 0) … 7 (7 ou mais)
CURVE_BUCKETS = range(1, 8)

# Juntas, as cotas de papel não ocupam mais que isto das vagas
MAX_ROLE_SHARE = 0.65

# Tags que não descrevem o que a carta faz
NON_THEME = ("cycle", "name", "-with-set", "errata", "erratum", "meme", "alliteration", "unnoted",
             "deprecated", "single-target", "triggered-ability", "activated-ability", "static-ability",
             "reprint", "type-errata", "flavor")

THEME_MIN_SHARE = 0.12
THEME_MIN_LIFT = 2.5
THEME_LIMIT = 6

# Funções do comandante que mudam o que vale a pena jogar
COMMANDER_RULES = [
    {
        "tags": {"cost-reducer-creature", "cost-reducer"},
        "label": "O comandante reduz o custo de criaturas: criaturas caras valem mais",
        "applies": lambda c: c.is_creature and c.cmc >= 5,
        "bonus": lambda c: 0.035 * (min(c.cmc, 9) - 4),
    },
]


# Comandantes que se beneficiam de terrenos (landfall, terreno extra, terrenos
# no cemitério…): para eles, ramp de terreno é sinergia, não só aceleração
LAND_SYNERGY_TAGS = {
    "landfall", "lands-matter", "synergy-land", "extra-land", "play-additional-land",
    "land-ramp", "mass-land-ramp", "multi-land-ramp", "land-count-matters",
    "lands-in-graveyard-matter", "recursion-land", "reanimate-land", "synergy-land-graveyard",
    "land-sacrifice", "sac-outlet-land", "sacrifice-outlet-land",
}


# Ramp que põe terreno em jogo (ou deixa jogar terreno a mais). Em decks com
# sinergia de terrenos, só isto conta como ramp: mana dork e pedra de mana não.
LAND_RAMP_TAGS = {
    "land-ramp", "rampant-growth", "extra-land", "play-additional-land",
    "mass-land-ramp", "multi-land-ramp", "tutor-land-to-battlefield",
}


def ramp_tags_for(land_deck: bool) -> set[str]:
    """Tags que identificam o papel de ramp neste deck."""

    if land_deck:
        return LAND_RAMP_TAGS
    return next(tags for key, _, tags, _ in ROLES if key == "ramp")


def is_mana_rock(tags) -> bool:
    return any(tag == "mana-rock" or tag.startswith("manarock") for tag in tags)


# "typal-bird", "tribal-elf"… → tribo; estas não são tipos de criatura
TRIBE_TAG_RE = re.compile(r"^(?:typal|tribal)-([a-z]+)$")
NOT_TRIBES = {"creature", "spell", "matters", "payoff", "lord", "tribal", "typal", "changeling"}


def tribes_from_tags(tags) -> list[str]:
    """{'typal-bird', 'tribal-bird'} → ['bird']"""

    found = set()
    for tag in tags:
        match = TRIBE_TAG_RE.match(tag)
        if match and match.group(1) not in NOT_TRIBES:
            found.add(match.group(1))
    return sorted(found)


def creature_types(type_line: str) -> set[str]:
    """'Legendary Creature — Bird Scout // …' → {'bird', 'scout'}"""

    front = (type_line or "").split(" // ")[0]
    if "—" not in front or "Creature" not in front.split("—")[0]:
        return set()
    return {word.lower() for word in front.split("—", 1)[1].split()}


def bucket_of(cmc) -> int:
    return min(max(int(cmc or 0), 1), 7)


@dataclass
class Profile:
    source: str
    sample_size: int
    role_targets: dict[str, int]
    creature_target: int | None
    curve_targets: dict[int, int]
    themes: list[dict] = field(default_factory=list)
    rules: list[dict] = field(default_factory=list)
    average_lands: int | None = None
    land_synergy: list[str] = field(default_factory=list)   # tags do comandante ligadas a terrenos
    tribes: list[str] = field(default_factory=list)          # tipo(s) de criatura de um deck tribal
    # Orçamento baixo e comandante sem sinergia com terrenos: sem ramp nem pedras de mana
    skip_ramp: bool = False
    skip_mana_rocks: bool = False

    @property
    def theme_tags(self) -> set[str]:
        return {theme["tag"] for theme in self.themes}

    @property
    def high_curve_share(self) -> float:
        total = sum(self.curve_targets.values()) or 1
        return sum(v for b, v in self.curve_targets.items() if b >= 5) / total


# Tags genéricas de papel não são tema: se fossem, qualquer ramp (ou remoção…)
# ganharia o bônus de sinergia e passaria por cima da prioridade dos papéis.
# Tags mais específicas, como "edict" num deck de sacrifício, continuam valendo.
GENERIC_ROLE_TAGS = (
    next(tags for key, _, tags, _ in ROLES if key == "ramp")
    | {"acceleration", "mana-ramp", "mana-producer", "removal", "spot-removal", "creature-removal",
       "draw", "card-advantage", "sweeper", "mass-removal"}
)


def _is_theme_candidate(label: str) -> bool:
    return label not in GENERIC_ROLE_TAGS and not any(word in label for word in NON_THEME)


def _scale(counts: dict, slots: int) -> dict:
    """Arredonda proporcionalmente para somar exatamente `slots`."""

    total = sum(counts.values())
    if not total:
        return {key: 0 for key in counts}

    shares = {key: value * slots / total for key, value in counts.items()}
    result = {key: math.floor(share) for key, share in shares.items()}

    leftover = slots - sum(result.values())
    for key in sorted(shares, key=lambda k: shares[k] - result[k], reverse=True)[:leftover]:
        result[key] += 1

    return result


def _average_deck_sample(commander_names: list[str]) -> list[dict]:
    """Cartas não-terreno do deck médio do EDHREC (JSON do Scryfall)."""

    path = commander_path(commander_names).replace("commanders/", "average-decks/", 1)
    data = get_page(path)

    deck = (data or {}).get("deck") or {}
    cards = deck.get("cards") if isinstance(deck, dict) else None

    if not cards:
        return []

    commanders = {name.lower() for name in deck.get("commander") or []}
    names = []

    for card_type, entries in cards.items():
        if card_type == "Land":
            continue
        for entry in entries:
            name = entry[0] if isinstance(entry, (list, tuple)) else entry
            if name and name.lower() not in commanders:
                names.append(name)

    try:
        found = get_cards_by_name(names)
    except requests.RequestException:
        return []

    return [found[name] for name in names if name in found]


def _themes(sample_tags: list[set[str]]) -> list[dict]:
    if not sample_tags:
        return []

    size = len(sample_tags)
    counts = {}
    for tags in sample_tags:
        for tag in tags:
            counts[tag] = counts.get(tag, 0) + 1

    frequent = {tag: n for tag, n in counts.items() if n / size >= THEME_MIN_SHARE and _is_theme_candidate(tag)}
    if not frequent:
        return []

    total_cards = TagIndex.objects.filter(kind=TagIndex.ORACLE).count() or 1
    global_counts = dict(
        TagStat.objects.filter(kind=TagIndex.ORACLE, label__in=list(frequent)).values_list("label", "count")
    )

    themes = []
    for tag, n in frequent.items():
        share = n / size
        global_share = global_counts.get(tag, 0) / total_cards
        if not global_share:
            continue
        lift = share / global_share
        if lift >= THEME_MIN_LIFT:
            themes.append({"tag": tag, "share": round(share, 3), "lift": round(lift, 1)})

    themes.sort(key=lambda t: t["share"] * math.log(t["lift"]), reverse=True)

    return themes[:THEME_LIMIT]


def build_profile(commanders: list[dict], pool: Pool, slots: int) -> Profile:
    names = [c["name"] for c in commanders]

    sample = _average_deck_sample(names) if pool.found else []
    source = "deck médio do EDHREC"

    if not sample and pool.found:
        # Sem deck médio: as mais jogadas com o comandante
        from cards.services.catalog import get_cards_data

        top = sorted(pool.cards.values(), key=lambda c: -(c.inclusion or 0))
        datas = get_cards_data([c.scryfall_id for c in top[:80]])
        sample = [d for d in datas.values() if "Land" not in (d.get("type_line") or "").split(" // ")[0]][:64]
        source = "cartas mais jogadas no EDHREC"

    oracle_ids = [card_fields(d)["oracle_id"] for d in sample]
    tag_rows = {
        row.target_id: set(row.label_list)
        for row in TagIndex.objects.filter(kind=TagIndex.ORACLE, target_id__in=oracle_ids)
    }
    sample_tags = [tag_rows.get(oid, set()) for oid in oracle_ids]

    average_lands = (pool.type_distribution or {}).get("Land") if pool.found else None

    # ---- Funções do comandante
    commander_tags = set()
    for row in TagIndex.objects.filter(kind=TagIndex.ORACLE,
                                       target_id__in=[card_fields(c)["oracle_id"] for c in commanders]):
        commander_tags |= set(row.label_list)

    land_synergy = sorted(commander_tags & LAND_SYNERGY_TAGS)

    # ---- Papéis (em deck de terrenos, ramp = só ramp de terreno)
    role_tag_sets = {
        key: ramp_tags_for(bool(land_synergy)) if key == "ramp" else tags
        for key, _, tags, _ in ROLES
    }

    if sample and any(sample_tags):
        size = len(sample)
        role_targets = {
            key: round(sum(1 for tags in sample_tags if tags & role_tag_sets[key]) / size * slots)
            for key, _, _, _ in ROLES
        }

        # Mais terrenos que o deck médio: precisa de menos ramp
        if average_lands and average_lands < 40:
            role_targets["ramp"] = max(role_targets["ramp"] - (40 - average_lands) // 2, 3)

        role_targets = trim_roles(role_targets, int(slots * MAX_ROLE_SHARE))
    else:
        role_targets = {key: quota for key, _, _, quota in ROLES}
        source = "proporções padrão" if not sample else source

    # ---- Curva e criaturas (página do comandante)
    curve_targets = {}
    raw_curve = (pool.mana_curve or {}) if pool.found else {}
    if raw_curve:
        buckets = {b: 0 for b in CURVE_BUCKETS}
        for cmc, count in raw_curve.items():
            buckets[bucket_of(float(cmc))] += count
        curve_targets = _scale(buckets, slots)
    elif sample:
        buckets = {b: 0 for b in CURVE_BUCKETS}
        for data in sample:
            buckets[bucket_of(data.get("cmc"))] += 1
        curve_targets = _scale(buckets, slots)

    creature_target = None
    distribution = pool.type_distribution if pool.found else {}
    if distribution.get("Creature"):
        nonland = sum(v for k, v in distribution.items() if k != "Land") or 1
        creature_target = round(distribution["Creature"] / nonland * slots)

    # Tribo: tags typal-X / tribal-X do comandante ou dos temas do deck médio
    themes = _themes(sample_tags)
    tribes = tribes_from_tags(commander_tags) or tribes_from_tags(t["tag"] for t in themes)

    rules = [
        {"label": rule["label"], "index": index}
        for index, rule in enumerate(COMMANDER_RULES)
        if rule["tags"] & commander_tags
    ]

    return Profile(
        source=source,
        sample_size=len(sample),
        role_targets=role_targets,
        creature_target=creature_target,
        curve_targets=curve_targets,
        themes=themes,
        rules=rules,
        average_lands=average_lands,
        land_synergy=land_synergy,
        tribes=tribes,
    )
