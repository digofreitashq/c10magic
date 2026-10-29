"""
Construtor: monta um deck de Commander a partir do(s) comandante(s).

A ideia é ser um espelho do deck médio do comandante no EDHREC, dentro do
orçamento:

1. Perfil (cards/services/profile.py): quantas cartas de remoção, compra,
   board wipe, ramp e quantas criaturas o deck médio tem; o resto é sinergia.
   Com orçamento, usa os decks baratos do EDHREC ("budget").
2. Candidatas: as cartas da página do comandante no EDHREC + buscas no
   Scryfall por cartas da mesma função (papéis, temas, tribo, equipamentos…).
3. Ordem: popularidade com ESTE comandante (% dos decks dele que usam a carta
   e a sinergia do EDHREC). A popularidade geral no Magic não conta.
4. Seleção: as cotas de cada papel, depois as vagas de sinergia, mantendo a
   quantidade de criaturas do deck médio.
5. Orçamento (só as mágicas; comandante e básicos não contam): troca cada
   carta por outra mais barata da mesma função, perdendo o mínimo; depois
   usa a sobra.
6. Estratégias do comandante:
   - tribal: troca cartas por criaturas da tribo com a mesma função
     (principalmente não-criaturas por criaturas);
   - voltron: equipamentos e proteção valem mais;
   - orçamento de até R$ 10: sem ramp, a não ser que o comandante tenha
     sinergia com terrenos (aí, mágicas que põem terrenos), com artefatos
     (aí, pedras de mana) ou custe muito.
7. Terrenos: de duas cores com a sobra do orçamento e básicos pelos símbolos de mana.
"""

import hashlib
import math
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from functools import cached_property
from decimal import Decimal

import requests
from django.conf import settings
from django.core.cache import cache
from django.db import transaction

from cards.models import COLOR_ORDER, Deck, DeckCard, TagIndex, TagStat
from cards.services.catalog import get_cards_data, store_cards
from cards.services.deck import can_be_commander, group_of, identity_letters, is_basic_land
from cards.services.edhrec import commander_pool
from cards.services.exchange import estimate_brl
from cards.services.prices import known_prices, price_queue
from cards.services.profile import (
    CURVE_BUCKETS,
    NON_THEME,
    LAND_RAMP_TAGS,
    LAND_SYNERGY_TAGS,
    PROTECTION_TAGS,
    ROLE_LABELS,
    ROLE_PRIORITY,
    ROLES,
    Profile,
    bucket_of,
    build_profile,
    creature_types,
    is_mana_rock,
    ramp_tags_for,
)
from cards.services.scryfall import ScryfallClient, ScryfallQueryError, card_fields
from cards.services.tags import last_sync


BASIC_LANDS_COUNT = 40
DECK_SIZE = 100

FILLER_LABEL = "Sinergia"

# Sobra no orçamento: troca básicos por terrenos que geram 2+ cores do
# comandante (ou buscam um básico). Quantos, pelo número de cores:
FIXING_LANDS_MAX = {2: 10, 3: 14, 4: 16, 5: 18}
MIN_BASIC_LANDS = 20   # sobra básico para os que buscam básico (Terramorphic Expanse…)
# Parte do orçamento reservada para esses terrenos antes de ajustar as mágicas
# (sem reserva, as mágicas sempre consomem o orçamento até o último centavo)
FIXING_BUDGET_SHARE = Decimal("0.15")
FETCH_BASIC_RE = re.compile(r"search your library for (?:a|an|up to (?:one|two)) basic land", re.IGNORECASE)

BASIC_BY_COLOR = {"W": "Plains", "U": "Island", "B": "Swamp", "R": "Mountain", "G": "Forest", "C": "Wastes"}

# Orçamento até este valor: sem ramp, a não ser que o comandante tenha uma
# estratégia que faça o ramp valer a pena (ver ramp_policy)
LOW_BUDGET_LIMIT = Decimal("10")
# Comandante a partir deste custo: o ramp vale a pena mesmo num deck barato
EXPENSIVE_COMMANDER_MV = 6

# Com orçamento até este valor, as cartas e o deck médio vêm dos decks baratos do EDHREC
BUDGET_POOL_LIMIT = Decimal("500")

# Média por carta abaixo disto: também procura as cartas mais baratas de cada função
CHEAP_AVERAGE = Decimal("0.40")

# Nenhuma carta sozinha pode levar mais que esta fração do orçamento
MAX_SHARE_OF_BUDGET = Decimal("0.3")

# ---- Nota (ordem das cartas)
# Popularidade com o comandante: % dos decks dele que usam a carta + sinergia do EDHREC
SYNERGY_WEIGHT = 0.9
# Desempates para as cartas que não estão na página do comandante (nota 0)
# e entre as que estão: temas do deck médio, tags escolhidas pelo usuário
THEME_BONUS = 0.05
THEME_BONUS_MAX = 0.15
TAG_BONUS = 0.25
TAG_BONUS_MAX = 0.5
# Voltron: equipamentos e cartas que dão proteção (hexproof, indestrutível…)
VOLTRON_BONUS = 0.3
# Tribal: criatura da tribo vale um pouco mais na ordem...
TRIBE_BONUS = 0.1
# ...e na troca por função (passo 6), entra no lugar de uma carta até esta
# diferença de nota (o tema tribal vale mais que a popularidade); com o tipo
# de criatura escolhido pelo usuário, mais ainda
TRIBE_SWAP_MARGIN = 0.25
USER_TRIBE_SWAP_MARGIN = 0.6
# ...e as criaturas podem passar das do deck médio até esta quantidade
TRIBE_EXTRA_CREATURES = 6

# Cartas "relentless" (o deck pode ter quantas quiser, ex.: Tempest Hawk,
# Relentless Rats) com sinergia com o deck entram com várias cópias
RELENTLESS_COPIES = 8
RELENTLESS_MIN_SYNERGY = 0.05

IRREGULAR_PLURALS = {"elf": "elves", "dwarf": "dwarves", "wolf": "wolves", "werewolf": "werewolves",
                     "fungus": "fungi", "octopus": "octopi", "mouse": "mice", "ox": "oxen"}

MAX_PRIORITY_TAGS = 12

# Quanto vale proteger a vaga ao economizar / melhorar com a sobra do orçamento:
# sinergia (None) > remoção > compra > board wipe > ramp
PRIORITY_WEIGHT = {None: 1.6, "removal": 1.3, "draw": 1.15, "sweeper": 1.0, "ramp": 0.7}

CREATURE_TOLERANCE = 3

# Troca por orçamento: a carta nova deve ser parecida com a que sai (mesmas
# tags, tipos e cores). Cada troca "perde" a diferença de nota mais este peso
# vezes o quanto as duas são diferentes (0 = iguais, 1 = nada em comum)
SIMILARITY_WEIGHT = 0.5

PARTNER_WORDS = ("Partner", "Friends forever", "Choose a Background", "Doctor's companion")


class BuildError(ValueError):
    pass


@dataclass
class Candidate:
    data: dict
    name: str
    oracle_id: str
    roles: set[str]
    score: float
    price: Decimal
    estimated: bool
    source: str           # "edhrec" | "scryfall"
    is_creature: bool
    cmc: float = 0
    tags: frozenset = frozenset()
    synergy: float | None = None
    inclusion: float | None = None
    assigned: str | None = None   # papel que ocupa no deck (ou None = sinergia)
    functions: int = 0            # papéis + temas + tags escolhidas + estratégia do comandante
    tribe_member: bool = False    # criatura da tribo do deck (ou Changeling)
    tribe_payoff: bool = False    # cita a tribo no texto
    voltron_piece: bool = False   # equipamento ou proteção, num deck voltron
    copy_index: int = 0           # cópia extra de carta "relentless" (0 = original)
    locked: bool = False          # pacote "relentless": entra inteiro e não sai nas trocas
    wanted_hits: int = 0          # quantas das tags escolhidas pelo usuário a carta tem

    @property
    def category(self):
        return ROLE_LABELS.get(self.assigned, FILLER_LABEL)

    @property
    def priority(self) -> float:
        return PRIORITY_WEIGHT.get(self.assigned, 1.0)

    @property
    def wanted(self) -> bool:
        return self.wanted_hits > 0

    @property
    def base_oracle(self) -> str:
        """oracle_id sem o sufixo das cópias extras ("abc#2" → "abc")."""
        return self.oracle_id.split("#")[0]

    @property
    def pure_ramp(self) -> bool:
        """Só ramp, sem outra função: entra apenas pela cota de ramp."""
        return self.roles == {"ramp"} and self.functions <= 1

    @property
    def bucket(self):
        return bucket_of(self.cmc)

    @cached_property
    def features(self) -> frozenset:
        """O que a carta é e faz: tags de função, tipos, subtipos e se é multicolorida."""

        type_line = (self.data.get("type_line") or "").split(" // ")[0]
        words = {f"type:{w.lower()}" for w in type_line.replace("—", " ").split()}
        colors = set(self.data.get("colors") or [])
        for face in self.data.get("card_faces") or []:
            colors |= set(face.get("colors") or [])
        tags = {t for t in self.tags if not any(word in t for word in NON_THEME)}
        return frozenset(tags | words | ({"multicolor"} if len(colors) >= 2 else set()))


def similarity(a: Candidate, b: Candidate) -> float:
    """0 a 1: quanto as duas cartas têm em comum (tags, tipos, cores)."""

    union = a.features | b.features
    return len(a.features & b.features) / len(union) if union else 0.0


@dataclass
class FixingLand:
    data: dict
    price: Decimal
    estimated: bool


@dataclass
class Plan:
    commanders: list[dict]
    identity: str
    picked: list[Candidate]
    lands: dict[str, int]
    report: dict
    fixing_lands: list[FixingLand] = None

    def __post_init__(self):
        self.fixing_lands = self.fixing_lands or []


# ============================================================
# COMANDANTES
# ============================================================

def _pairs(first: dict, second: dict) -> bool:
    def text(data):
        return " ".join(
            [data.get("oracle_text") or "", data.get("type_line") or ""]
            + [face.get("oracle_text") or "" for face in data.get("card_faces") or []]
        )

    a, b = text(first), text(second)
    return any(word in a or word in b for word in PARTNER_WORDS) or "Background" in a + b


def resolve_commanders(names: list[str], client: ScryfallClient | None = None) -> list[dict]:
    client = client or ScryfallClient()
    commanders = []

    for name in names:
        try:
            data = client.named_fuzzy(name)
            if not data:
                # Só parte do nome ("Kastral"): tenta a primeira sugestão do Scryfall
                suggestions = client.autocomplete(name)
                if suggestions:
                    data = client.named_fuzzy(suggestions[0])
        except requests.RequestException as exc:
            raise BuildError(f"Não foi possível consultar o Scryfall ({exc.__class__.__name__}).") from exc

        if not data:
            raise BuildError(f"Carta não encontrada: {name}")
        if not can_be_commander(data):
            raise BuildError(f"{data['name']} não pode ser comandante.")
        if (data.get("legalities") or {}).get("commander") not in ("legal", None):
            raise BuildError(f"{data['name']} não é legal como comandante.")

        commanders.append(data)

    if len(commanders) == 2:
        if commanders[0]["oracle_id"] == commanders[1]["oracle_id"]:
            raise BuildError("Escolha dois comandantes diferentes.")
        if not _pairs(*commanders):
            raise BuildError(
                f"{commanders[0]['name']} e {commanders[1]['name']} não formam dupla "
                "(partner, background, friends forever…)."
            )

    store_cards(commanders)

    return commanders


# ============================================================
# CANDIDATAS
# ============================================================

def _price(data, quotes, rate):
    """(preço em R$, é estimativa?) ou (None, True) sem preço nenhum."""

    quote = quotes.get(data["name"])
    if quote is not None and quote.price is not None:
        return quote.price, False

    usd = (data.get("prices") or {}).get("usd") or (data.get("prices") or {}).get("usd_foil")
    if usd:
        return estimate_brl(Decimal(usd), rate), True

    return None, True


def _is_allowed(data: dict, identity: str, commander_oracles: set[str]) -> bool:
    fields = card_fields(data)

    if fields["oracle_id"] in commander_oracles:
        return False
    if group_of(fields["type_line"]) == "Land" or is_basic_land(fields["type_line"]):
        return False
    if (data.get("legalities") or {}).get("commander") != "legal":
        return False
    if not set(fields["color_identity"]) <= set(identity):
        return False
    if "paper" not in (data.get("games") or ["paper"]):
        return False
    # Folhas de adesivos e atrações não são cartas do deck
    if any(t in fields["type_line"] for t in ("Stickers", "Attraction")):
        return False

    return True


def candidate_queries(identity: str, max_usd: Decimal | None, profile: Profile,
                      cheapest: bool = False) -> list[tuple[str, str]]:
    """
    Buscas no Scryfall, como (busca, ordem): cartas de cada função que o deck
    precisa (papéis, temas, tribo, equipamentos…). Com cheapest=True
    (orçamento muito baixo), também as mais baratas de cada uma.
    """

    base = [f"id<={identity.lower()}" if identity else "id:c", "f:commander", "game:paper", "-t:land"]
    if max_usd is not None:
        base.append(f"usd<={max_usd}")

    def query(*terms):
        return " ".join([*terms, *base])

    def any_tag(tags):
        return f"({' or '.join(f'otag:{t}' for t in sorted(tags))})"

    land_deck = bool(profile.land_synergy)
    queries = [
        query(any_tag(ramp_tags_for(land_deck) if key == "ramp" else tags))
        for key, _, tags, _ in ROLES
        if not (profile.skip_ramp and key == "ramp")
    ]
    queries += [query(f"otag:{theme['tag']}") for theme in profile.themes[:3]]
    queries += [
        query(f"otag:{tag}", "t:creature" if tag in profile.creature_only_tags else "")
        for tag in profile.priority_tags
    ]

    if profile.creature_target and profile.creature_target >= 18:
        queries.append(query("t:creature", "mv<=4"))
    if profile.high_curve_share >= 0.2:
        queries.append(query("t:creature" if profile.creature_target else "", "mv>=5"))

    # Tribal: criaturas da tribo, também as que fazem o papel de outra carta
    for tribe in profile.tribes:
        queries.append(query(f"t:{tribe}"))
        queries += [query(f"t:{tribe}", any_tag(tags)) for key, _, tags, _ in ROLES if key in ("removal", "draw")]
    if profile.tribes:
        queries.append(query("otag:relentless"))

    # Voltron: equipamentos e proteção
    if profile.voltron:
        queries += [query("t:equipment"), query(any_tag(PROTECTION_TAGS))]

    queries.append(query())

    queries = [q.replace("  ", " ").strip() for q in queries]
    ordered = [(q, "edhrec") for q in queries]

    if cheapest:
        # Das mais baratas (as mais populares raramente são as de R$ 0,05 que
        # um orçamento assim precisa)
        ordered += [(q, "usd") for q in queries if "mv" not in q]

    return list(dict.fromkeys(ordered))


def _fetch_query(item) -> tuple[list[dict], bool]:
    """
    Primeira página (175 cartas) de uma busca, na ordem pedida ("edhrec" =
    mais populares, "usd" = mais baratas); só rede e cache. (cartas, falhou?)
    """

    query, order = (item, "edhrec") if isinstance(item, str) else item
    direction = "asc" if order == "usd" else "auto"

    key = "c10magic:builder:" + hashlib.sha1(f"{order}|{query}".encode()).hexdigest()
    cards = cache.get(key)

    if cards is None:
        try:
            cards = ScryfallClient().search_page(query, 1, "cards", order, direction)["data"]
        except ScryfallQueryError:
            cards = []
        except (requests.RequestException, RuntimeError):
            # Rede ou limite do Scryfall: segue sem esta busca (e não guarda no cache)
            return [], True
        cache.set(key, cards, settings.SCRYFALL_SEARCH_CACHE_SECONDS)

    return cards, False


def scryfall_candidates(queries: list, notes: list[str]) -> list[dict]:
    # Rede em paralelo, dentro do limite do Scryfall (ver _throttle_search);
    # o banco só é usado aqui, na thread principal
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(_fetch_query, queries))

    failed = sum(1 for _, error in results if error)
    if failed:
        notes.append(
            f"{failed} de {len(queries)} buscas no Scryfall falharam (limite de requisições ou rede); "
            "o deck foi montado com as demais. Reconstruir mais tarde pode melhorar."
        )

    found = [card for cards, _ in results for card in cards]
    store_cards(list({card["id"]: card for card in found}.values()))

    return found


def apply_bonuses(candidate: Candidate, profile: Profile):
    """
    Desempates da nota (que é a popularidade com o comandante): temas do deck
    médio, tags escolhidas, tribo e peças de voltron. Conta as funções da carta.
    """

    matched_themes = len(candidate.tags & profile.theme_tags)
    candidate.score += min(THEME_BONUS * matched_themes, THEME_BONUS_MAX)

    if candidate.wanted_hits:
        candidate.score += min(TAG_BONUS * candidate.wanted_hits, TAG_BONUS_MAX)
    if candidate.tribe_member:
        candidate.score += TRIBE_BONUS
    if candidate.voltron_piece:
        candidate.score += VOLTRON_BONUS

    candidate.functions = (len(candidate.roles) + matched_themes + candidate.wanted_hits
                           + (candidate.tribe_member or candidate.tribe_payoff) + candidate.voltron_piece)


def _tribe_words(tribe: str) -> list[str]:
    return [tribe, IRREGULAR_PLURALS.get(tribe, tribe + "s")]


def _is_tribe_member(data: dict, fields: dict, tribes: list[str]) -> bool:
    if not tribes:
        return False
    if "Changeling" in (data.get("keywords") or []):
        return True
    return bool(creature_types(fields["type_line"]) & set(tribes))


def _mentions_tribe(fields: dict, tribes: list[str]) -> bool:
    text = (fields.get("oracle_text") or "").lower()
    return any(
        re.search(rf"\b{word}\b", text)
        for tribe in tribes
        for word in _tribe_words(tribe)
    )


def is_voltron_piece(fields: dict, tags) -> bool:
    """Equipamento ou carta que protege (hexproof, indestrutível, proteção, ward)."""

    return "Equipment" in fields["type_line"].split(" // ")[0] or bool(set(tags) & PROTECTION_TAGS)


def has_relentless_synergy(candidate: Candidate, themes: set[str]) -> bool:
    """Carta "relentless" que vale a pena repetir neste deck."""

    if "relentless" not in candidate.tags:
        return False
    return (
        candidate.tribe_member
        or (candidate.synergy or 0) >= RELENTLESS_MIN_SYNERGY
        or bool(candidate.tags & themes)
    )


def add_relentless_copies(candidates: list[Candidate], themes: set[str]) -> list[Candidate]:
    """Cópias extras (cada uma ocupa uma vaga) das cartas "relentless" com sinergia."""

    extra = []
    for candidate in candidates:
        if has_relentless_synergy(candidate, themes):
            for index in range(1, RELENTLESS_COPIES):
                extra.append(replace(candidate, oracle_id=f"{candidate.oracle_id}#{index}",
                                     copy_index=index, assigned=None))
    return candidates + extra


def build_candidates(commanders, identity, pool, profile: Profile, rate, budget, slots, notes):
    commander_oracles = {card_fields(c)["oracle_id"] for c in commanders}

    max_price = None
    if budget is not None:
        # Uma carta sozinha não pode comer o orçamento todo
        max_price = max(budget * MAX_SHARE_OF_BUDGET, budget / slots)

    max_usd = (max_price / rate).quantize(Decimal("0.01")) if max_price is not None else None

    pool_cards = pool.cards if pool.found else {}
    pool_datas = get_cards_data(list(pool_cards)) if pool_cards else {}

    cheapest = budget is not None and budget / slots < CHEAP_AVERAGE
    extra = scryfall_candidates(candidate_queries(identity, max_usd, profile, cheapest), notes)

    datas = list(pool_datas.values()) + extra
    datas = [d for d in datas if _is_allowed(d, identity, commander_oracles)]

    quotes = known_prices([d["name"] for d in datas])

    oracle_ids = {card_fields(d)["oracle_id"] for d in datas}
    tags = {
        row.target_id: frozenset(row.label_list)
        for row in TagIndex.objects.filter(kind=TagIndex.ORACLE, target_id__in=list(oracle_ids))
    }

    wanted_tags = frozenset(profile.priority_tags)
    land_deck = bool(profile.land_synergy)
    by_oracle = {}

    for data in datas:
        fields = card_fields(data)
        price, estimated = _price(data, quotes, rate)

        if price is None:
            if budget is not None:
                continue      # sem preço não dá para respeitar o orçamento
            price = Decimal(0)

        if max_price is not None and price > max_price:
            continue

        # Nota: popularidade com este comandante (a página dele no EDHREC)
        pool_card = pool_cards.get(data["id"])
        if pool_card:
            score = (pool_card.inclusion or 0) + SYNERGY_WEIGHT * max(pool_card.synergy or 0, 0)
            source = "edhrec"
        else:
            score = 0.0
            source = "scryfall"

        card_tags = tags.get(fields["oracle_id"], frozenset())
        roles = {key for key, _, role_tags, _ in ROLES if card_tags & role_tags}

        if land_deck and "ramp" in roles and not card_tags & LAND_RAMP_TAGS:
            # Deck de terrenos: o ramp que vale é o que põe terrenos em jogo
            roles.discard("ramp")
            if not roles and not card_tags & (profile.theme_tags | LAND_SYNERGY_TAGS):
                continue

        if profile.skip_mana_rocks and is_mana_rock(card_tags):
            continue

        if profile.skip_ramp:
            # Sem ramp: só entra se fizer outra coisa (e aí o ramp não conta como função)
            if roles == {"ramp"} and not card_tags & profile.theme_tags:
                continue
            roles.discard("ramp")

        is_creature = "Creature" in fields["type_line"].split(" // ")[0]
        hits = card_tags & wanted_tags
        if not is_creature:
            hits = hits - profile.creature_only_tags

        candidate = Candidate(
            data=data,
            name=data["name"],
            oracle_id=fields["oracle_id"],
            roles=roles,
            score=score,
            price=price,
            estimated=estimated,
            source=source,
            is_creature=is_creature,
            cmc=float(data.get("cmc") or 0),
            tags=card_tags,
            synergy=pool_card.synergy if pool_card else None,
            inclusion=pool_card.inclusion if pool_card else None,
            wanted_hits=len(hits),
            tribe_member=is_creature and _is_tribe_member(data, fields, profile.tribes),
            tribe_payoff=_mentions_tribe(fields, profile.tribes),
            voltron_piece=profile.voltron and is_voltron_piece(fields, card_tags),
        )

        apply_bonuses(candidate, profile)

        current = by_oracle.get(candidate.oracle_id)
        if current is None or candidate.score > current.score:
            by_oracle[candidate.oracle_id] = candidate

    return add_relentless_copies(list(by_oracle.values()), profile.theme_tags)


# ============================================================
# ESCOLHA
# ============================================================

def _ranked(candidates: list[Candidate]) -> list[Candidate]:
    """Pela nota; no empate, a mais barata."""

    return sorted(candidates, key=lambda c: (-c.score, c.price))


def select(candidates: list[Candidate], slots: int, profile: Profile) -> list[Candidate]:
    """
    Espelho do deck médio: as cotas de cada papel (remoção, compra, board
    wipe, ramp) com as melhores cartas de cada um; as vagas de sinergia com as
    melhores do resto; e a quantidade de criaturas do deck médio.
    """

    ranked = _ranked(candidates)
    picked, used = [], set()
    target = profile.creature_target

    def take(candidate, role=None):
        candidate.assigned = role
        picked.append(candidate)
        used.add(candidate.oracle_id)

    # 1. Cotas de papel (uma carta que serve a dois papéis fica no primeiro)
    for key in ROLE_PRIORITY:
        quota = profile.role_targets.get(key, 0)
        for candidate in ranked:
            if len(picked) >= slots or sum(c.assigned == key for c in picked) >= quota:
                break
            if candidate.oracle_id not in used and key in candidate.roles:
                take(candidate, key)

    # 1b. Pacote "relentless": se a carta merece uma vaga, entram todas as
    #     cópias juntas (cada uma busca a próxima). Não contam nas criaturas.
    copies = [c for c in ranked if c.copy_index]
    if copies:
        best = max(copies, key=lambda c: c.score)
        if best.score >= ranked[min(slots, len(ranked)) - 1].score:
            for candidate in ranked:
                if len(picked) >= slots:
                    break
                if candidate.base_oracle == best.base_oracle and candidate.oracle_id not in used:
                    take(candidate)
                    candidate.locked = True

    def creature_count():
        return sum(c.is_creature and not c.locked for c in picked)

    # 2. Sinergia: as melhores do resto, sem passar das criaturas do deck médio
    #    (ramp puro só entra pela cota de ramp)
    def room_for(candidate):
        if target is None:
            return True
        creatures = creature_count()
        others = sum(not c.is_creature for c in picked)
        if candidate.is_creature:
            return creatures < target + CREATURE_TOLERANCE
        return others < slots - target + CREATURE_TOLERANCE

    for candidate in ranked:
        if len(picked) >= slots:
            break
        if candidate.oracle_id not in used and not candidate.pure_ramp and room_for(candidate):
            take(candidate)

    # 3. Faltou: completa com o que houver
    for candidate in ranked:
        if len(picked) >= slots:
            break
        if candidate.oracle_id not in used:
            take(candidate)

    # 4. Criaturas longe do deck médio: troca nas vagas de sinergia
    if target is not None:
        for want_creature in (True, False):
            while True:
                creatures = creature_count()
                if want_creature and creatures >= target - CREATURE_TOLERANCE:
                    break
                if not want_creature and creatures <= target + CREATURE_TOLERANCE:
                    break
                spare = [c for c in picked if c.assigned is None and c.is_creature != want_creature and not c.locked]
                options = [c for c in ranked if c.is_creature == want_creature and c.oracle_id not in used
                           and not c.pure_ramp]
                if not spare or not options:
                    break
                worst, best = min(spare, key=lambda c: c.score), options[0]
                picked[picked.index(worst)] = best
                best.assigned = None
                used.discard(worst.oracle_id)
                used.add(best.oracle_id)

    return picked


def _compatible(out: Candidate, new: Candidate, level: int) -> bool:
    """
    Troca da mesma função: mantém o papel, o "é criatura", a tribo e a faixa
    de custo: mesma faixa (nível 0), faixa vizinha (1) ou qualquer (2).
    """

    if out.assigned is not None and out.assigned not in new.roles:
        return False
    if out.assigned is None and new.pure_ramp:
        return False
    if out.tribe_member and not new.tribe_member:
        return False
    if out.voltron_piece and not new.voltron_piece:
        return False
    if out.is_creature != new.is_creature:
        return False
    if level == 0:
        return out.bucket == new.bucket
    if level == 1:
        return abs(out.bucket - new.bucket) <= 1
    return True


def _keeps_creature_count(picked, out, new, creature_target):
    """Criatura por não-criatura (ou o contrário) só se a contagem seguir na tolerância."""

    if out.is_creature == new.is_creature:
        return True
    if creature_target is None:
        return True
    creatures = sum(c.is_creature for c in picked) - out.is_creature + new.is_creature
    return abs(creatures - creature_target) <= CREATURE_TOLERANCE


def fit_budget(picked: list[Candidate], candidates: list[Candidate], budget: Decimal,
               creature_target: int | None = None) -> bool:
    """
    Troca cartas por outras mais baratas da mesma função até caber: sempre a
    troca que mais economiza perdendo menos (nota e semelhança com a carta
    que sai). Retorna se coube.
    """

    used = {c.oracle_id for c in picked}
    pool = sorted((c for c in candidates if c.oracle_id not in used), key=lambda c: c.price)

    total = sum((c.price for c in picked), Decimal(0))

    while total > budget:
        best = None

        # Primeiro mantém a faixa de custo e o "é criatura"; só afrouxa se não
        # houver troca. No nível 3, mantém o papel e a tribo e aceita criatura
        # por não-criatura se a contagem de criaturas seguir na tolerância; no
        # 4 (último recurso, o orçamento não fecha de outro jeito), só o papel.
        for level in (0, 1, 2, 3, 4):
            for out in picked:
                if out.locked:
                    continue
                for new in pool:
                    if new.price >= out.price:
                        break
                    if level < 3 and not _compatible(out, new, level):
                        continue
                    if level >= 3:
                        if out.assigned is not None and out.assigned not in new.roles:
                            continue
                        if out.assigned is None and new.pure_ramp:
                            continue
                        if level == 3 and out.tribe_member and not new.tribe_member:
                            continue
                        if not _keeps_creature_count(picked, out, new, creature_target):
                            continue
                    saved = float(out.price - new.price)
                    # Perder qualidade em ramp custa menos que em sinergia; carta
                    # parecida com a que sai perde menos
                    lost = out.score - new.score + SIMILARITY_WEIGHT * (1 - similarity(out, new))
                    lost = max(lost, 0.0005) * out.priority
                    ratio = saved / lost
                    if best is None or ratio > best[0]:
                        best = (ratio, out, new)
            if best is not None:
                break

        if best is None:
            return False

        _, out, new = best
        new.assigned = out.assigned
        picked[picked.index(out)] = new
        pool.remove(new)
        pool.append(out)
        pool.sort(key=lambda c: c.price)
        total += new.price - out.price

    return True


def fit_with_release(picked: list[Candidate], candidates: list[Candidate], budget: Decimal,
                     profile: Profile, notes: list[str]) -> bool:
    """
    fit_budget; se não couber nem trocando, libera as cotas da menor
    prioridade para a maior (ramp primeiro) e tenta de novo. As cartas da
    cota liberada viram vagas de sinergia e podem dar lugar a qualquer carta.
    """

    fits = fit_budget(picked, candidates, budget, profile.creature_target)
    released = []

    for role in reversed(ROLE_PRIORITY):
        if fits:
            break
        if not any(c.assigned == role for c in picked):
            continue
        for c in picked:
            if c.assigned == role:
                c.assigned = None
        released.append(ROLE_LABELS[role].lower())
        fits = fit_budget(picked, candidates, budget, profile.creature_target)

    if released:
        notes.append(
            "Para caber no orçamento, a cota de " + ", ".join(released)
            + " foi liberada (as vagas puderam receber cartas mais baratas de outro papel)."
        )

    return fits


def use_leftover(picked: list[Candidate], candidates: list[Candidate], budget: Decimal, rounds: int = 150,
                 levels: tuple = (0, 1)):
    """Com sobra no orçamento, troca por cartas de nota maior da mesma função que ainda caibam."""

    used = {c.oracle_id for c in picked}
    total = sum((c.price for c in picked), Decimal(0))

    floor = min((c.score for c in picked), default=0)
    pool = [c for c in candidates if c.oracle_id not in used and c.score > floor]

    for _ in range(rounds):
        slack = budget - total
        best = None

        for level in levels:
            for out in picked:
                if out.locked:
                    continue
                for new in pool:
                    if new.score <= out.score or new.price - out.price > slack:
                        continue
                    if not _compatible(out, new, level):
                        continue
                    extra = float(max(new.price - out.price, Decimal("0.01")))
                    # A sobra vai primeiro para sinergia, depois remoção, compra… ramp por último
                    gain = (new.score - out.score) * out.priority / extra
                    if best is None or gain > best[0]:
                        best = (gain, out, new)
            if best is not None:
                break

        if best is None:
            break

        _, out, new = best
        new.assigned = out.assigned
        picked[picked.index(out)] = new
        pool.remove(new)
        if out.score > floor:
            pool.append(out)
        total += new.price - out.price


def tribal_swaps(picked: list[Candidate], candidates: list[Candidate], profile: Profile,
                 budget: Decimal | None) -> int:
    """
    Deck tribal: troca cartas de fora da tribo por criaturas da tribo que
    fazem a mesma função (o mesmo papel; nas vagas de sinergia, qualquer
    uma), primeiro as não-criaturas. A criatura da tribo entra mesmo com nota
    um pouco menor (até a margem), e as criaturas podem passar das do deck
    médio até TRIBE_EXTRA_CREATURES. Retorna quantas trocas fez.
    """

    if not profile.tribes:
        return 0

    margin = USER_TRIBE_SWAP_MARGIN if profile.user_tribes else TRIBE_SWAP_MARGIN
    used = {c.oracle_id for c in picked}
    total = sum((c.price for c in picked), Decimal(0))
    options = _ranked([c for c in candidates if c.tribe_member and c.oracle_id not in used])
    max_creatures = (profile.creature_target + TRIBE_EXTRA_CREATURES
                     if profile.creature_target is not None else len(picked))
    swaps = 0

    # Não-criaturas primeiro; dentro de cada grupo, as de nota menor
    for out in sorted((c for c in picked if not c.tribe_member and not c.locked),
                      key=lambda c: (c.is_creature, c.score)):
        if not out.is_creature and sum(c.is_creature and not c.locked for c in picked) >= max_creatures:
            continue
        for new in options:
            if new.score + margin < out.score:
                break          # as próximas têm nota ainda menor
            if new.oracle_id in used:
                continue
            if out.assigned is not None and out.assigned not in new.roles:
                continue
            if out.assigned is None and new.pure_ramp:
                continue
            if budget is not None and total - out.price + new.price > budget:
                continue
            new.assigned = out.assigned
            picked[picked.index(out)] = new
            used.discard(out.oracle_id)
            used.add(new.oracle_id)
            total += new.price - out.price
            swaps += 1
            break

    return swaps


# ============================================================
# TERRENOS
# ============================================================

def is_fixing_land(data: dict, identity: str) -> bool:
    """Terreno não-básico que gera 2+ cores do comandante ou busca um básico."""

    fields = card_fields(data)
    front = fields["type_line"].split(" // ")[0]

    if "Land" not in front or "Basic" in front:
        return False
    if (data.get("legalities") or {}).get("commander") != "legal":
        return False
    if "paper" not in (data.get("games") or ["paper"]):
        return False
    if not set(fields["color_identity"]) <= set(identity):
        return False

    produced = set(data.get("produced_mana") or []) & set(identity)
    return len(produced) >= 2 or bool(FETCH_BASIC_RE.search(fields["oracle_text"] or ""))


def pick_fixing_lands(identity: str, slack: Decimal | None, rate, notes: list[str],
                      cheapest: bool = False) -> list[FixingLand]:
    """
    Os terrenos de duas cores (ou que buscam básico) mais populares que cabem
    na sobra do orçamento (sem orçamento: até o limite).
    """

    if len(identity) < 2 or (slack is not None and slack <= 0):
        return []

    limit = min(FIXING_LANDS_MAX.get(len(identity), 10), BASIC_LANDS_COUNT - MIN_BASIC_LANDS)

    base = f"t:land -t:basic id<={identity.lower()} f:commander game:paper"
    if slack is not None:
        base += f" usd<={(slack / rate).quantize(Decimal('0.01'))}"
    queries = [(base, "edhrec")] + ([(base, "usd")] if cheapest else [])

    found = {}
    for data in scryfall_candidates(queries, notes):
        if is_fixing_land(data, identity):
            found.setdefault(card_fields(data)["oracle_id"], data)

    quotes = known_prices([d["name"] for d in found.values()])
    ranked = sorted(found.values(), key=lambda d: d.get("edhrec_rank") or 999999)

    # Nenhum terreno sozinho leva mais que 3× a parte da reserva que cabe a
    # cada um: com pouco dinheiro, vários baratos (Evolving Wilds, taplands)
    # valem mais que um popular e caro (Exotic Orchard)
    max_each = slack / limit * 3 if slack is not None else None

    picked, remaining = [], slack
    for data in ranked:
        if len(picked) >= limit:
            break
        price, estimated = _price(data, quotes, rate)
        if price is None:
            if slack is not None:
                continue
            price = Decimal(0)
        if remaining is not None and (price > remaining or price > max_each):
            continue
        picked.append(FixingLand(data, price, estimated))
        if remaining is not None:
            remaining -= price

    return picked


SYMBOL_RE = re.compile(r"\{([^}]+)\}")


def pip_weights(cards: list[dict], commanders: list[dict], identity: str) -> dict[str, float]:
    weights = {color: 0.0 for color in identity}

    def add(mana_cost, factor):
        for symbol in SYMBOL_RE.findall(mana_cost or ""):
            colors = [c for c in symbol.split("/") if c in weights]
            for color in colors:
                weights[color] += factor / len(colors)

    for data in cards:
        add(card_fields(data)["mana_cost"], 1)
    for data in commanders:
        # O comandante é jogado toda partida: pesa mais
        add(card_fields(data)["mana_cost"], 3)

    return weights


def split_basic_lands(weights: dict[str, float], total: int = BASIC_LANDS_COUNT) -> dict[str, int]:
    if not weights:
        return {"C": total}

    minimum = 2 if len(weights) * 2 <= total else 0
    free = total - minimum * len(weights)
    weight_sum = sum(weights.values())

    if weight_sum <= 0:
        shares = {color: free / len(weights) for color in weights}
    else:
        shares = {color: free * w / weight_sum for color, w in weights.items()}

    counts = {color: minimum + math.floor(share) for color, share in shares.items()}

    leftover = total - sum(counts.values())
    by_fraction = sorted(shares, key=lambda c: shares[c] - math.floor(shares[c]), reverse=True)
    for color in by_fraction[:leftover]:
        counts[color] += 1

    return {color: counts[color] for color in COLOR_ORDER + "C" if counts.get(color)}


# ============================================================
# MONTAGEM
# ============================================================

# Eixos que o usuário pode ajustar no relatório (gráfico de radar)
TARGET_KEYS = ["synergy", "removal", "draw", "sweeper", "ramp", "creatures"]


def apply_overrides(profile: Profile, overrides: dict, slots: int, notes: list[str]):
    """
    Alvos escolhidos pelo usuário. Sinergia + papéis dividem as mesmas vagas:
    se não somarem `slots`, são ajustados proporcionalmente. Criaturas é à parte.
    """

    parts = {key: max(int(overrides.get(key, 0) or 0), 0) for key in TARGET_KEYS[:5]}

    if sum(parts.values()) and sum(parts.values()) != slots:
        parts = _scale_counts(parts, slots)

    for key in ("removal", "draw", "sweeper", "ramp"):
        profile.role_targets[key] = parts[key]

    # Ramp pedido explicitamente: vale mesmo com orçamento baixo
    if parts["ramp"] and profile.skip_ramp:
        profile.skip_ramp = False

    if overrides.get("creatures") is not None:
        profile.creature_target = min(max(int(overrides["creatures"]), 0), slots)

    notes.append("Proporções ajustadas por você no gráfico do relatório.")


def _scale_counts(counts: dict, total: int) -> dict:
    """Arredonda proporcionalmente para somar exatamente `total`."""

    current = sum(counts.values())
    shares = {k: v * total / current for k, v in counts.items()}
    result = {k: math.floor(v) for k, v in shares.items()}
    for key in sorted(shares, key=lambda k: shares[k] - result[k], reverse=True)[: total - sum(result.values())]:
        result[key] += 1
    return result


CREATURE_SUFFIX = ":criatura"
TRIBE_PREFIX = "tipo:"


def parse_wanted(items) -> tuple[list[str], set[str], list[str]]:
    """
    Itens escolhidos pelo usuário -> (tags válidas, tags só de criatura, tipos de criatura).

    "tag", "tag:criatura" (só vale em criaturas) e "tipo:vampire".
    """

    wanted, creature_only, tribes = [], set(), []

    for item in items or []:
        item = str(item).strip().lower().replace(" ", "-")
        if item.startswith(TRIBE_PREFIX):
            tribe = re.sub(r"[^a-z]", "", item[len(TRIBE_PREFIX):])
            if tribe and tribe not in tribes:
                tribes.append(tribe)
            continue
        only = item.endswith(CREATURE_SUFFIX)
        tag = item[: -len(CREATURE_SUFFIX)] if only else item
        if tag and tag not in wanted:
            wanted.append(tag)
        if only:
            creature_only.add(tag)

    valid = set(TagStat.objects.filter(kind=TagIndex.ORACLE, label__in=wanted).values_list("label", flat=True)) if wanted else set()
    wanted = [tag for tag in wanted if tag in valid][:MAX_PRIORITY_TAGS]

    return wanted, creature_only & set(wanted), tribes[:3]


def clean_priority_tags(tags) -> list[str]:
    """Tags informadas pelo usuário: minúsculas, sem repetir e só as que existem."""

    return parse_wanted(tags)[0]


def wanted_tokens(profile: Profile) -> list[str]:
    """Os itens escolhidos, no formato do campo (para o relatório e para gerar de novo)."""

    return (
        [tag + (CREATURE_SUFFIX if tag in profile.creature_only_tags else "") for tag in profile.priority_tags]
        + [TRIBE_PREFIX + tribe for tribe in profile.user_tribes]
    )


def ramp_policy(profile: Profile, commanders: list[dict], budget: Decimal | None) -> str:
    """
    Orçamento de até R$ 10: cada centavo vale mais em sinergia, remoção e
    compra, então o deck não leva ramp, a não ser que o comandante tenha uma
    estratégia que faça o ramp valer a pena. Retorna a explicação (ou "").
    """

    if budget is None or budget > LOW_BUDGET_LIMIT:
        return ""

    limit = f"Orçamento de até R$ {LOW_BUDGET_LIMIT}"

    if profile.land_synergy:
        # O ramp de terreno já é o único que conta num deck de terrenos
        profile.skip_mana_rocks = True
        return (f"{limit}: ramp só com mágicas que põem terrenos em jogo ou deixam jogar mais de um "
                f"terreno no turno, porque o comandante tem sinergia com terrenos "
                f"({', '.join(profile.land_synergy[:3])}).")

    if profile.artifact_synergy:
        return (f"{limit}: o ramp foi mantido, com pedras de mana, porque o comandante tem sinergia "
                f"com artefatos ({', '.join(profile.artifact_synergy[:3])}).")

    mana_value = max(float(c.get("cmc") or 0) for c in commanders)
    if mana_value >= EXPENSIVE_COMMANDER_MV:
        return f"{limit}: o ramp foi mantido porque o comandante custa {mana_value:g} de mana."

    profile.skip_ramp = True
    profile.skip_mana_rocks = True
    profile.role_targets["ramp"] = 0
    return f"{limit}: sem ramp; essas vagas foram para sinergia, remoção e compra."


def plan_deck(commander_names: list[str], budget: Decimal | None, rate,
              client: ScryfallClient | None = None, overrides: dict | None = None,
              priority_tags: list[str] | None = None) -> Plan:
    names = [n.strip() for n in commander_names if n and n.strip()]

    if not names:
        raise BuildError("Escolha o comandante.")
    if len(names) > 2:
        raise BuildError("No máximo dois comandantes.")

    commanders = resolve_commanders(names, client)
    identity = identity_letters("".join(card_fields(c)["color_identity"] for c in commanders))
    slots = DECK_SIZE - BASIC_LANDS_COUNT - len(commanders)

    notes = []

    if last_sync(TagIndex.ORACLE) is None:
        notes.append("As tags do Scryfall ainda não foram baixadas: os papéis (ramp, remoção…) não foram identificados.")

    # Com orçamento, as cartas e o deck médio dos decks baratos do comandante
    budget_deck = budget is not None and budget <= BUDGET_POOL_LIMIT
    pool = commander_pool([c["name"] for c in commanders], budget=budget_deck)
    if not pool.found:
        notes.append("O EDHREC não tem página para este comandante: só cartas do Scryfall com as funções do deck.")

    profile = build_profile(commanders, pool, slots, budget=budget_deck)
    profile.priority_tags, profile.creature_only_tags, profile.user_tribes = parse_wanted(priority_tags)
    profile.tribes = list(dict.fromkeys([*profile.user_tribes, *profile.tribes]))

    strategy = []
    ramp_note = ramp_policy(profile, commanders, budget)
    if ramp_note:
        strategy.append(ramp_note)
    if profile.voltron:
        strategy.append("Comandante voltron: equipamentos e cartas que dão proteção valem mais.")

    # O que o construtor calculou, antes dos ajustes do usuário (série "Perfil")
    profile_targets = {
        **{key: profile.role_targets.get(key, 0) for key in ("removal", "draw", "sweeper", "ramp")},
        "creatures": profile.creature_target,
    }
    profile_targets["synergy"] = max(slots - sum(profile_targets[k] for k in ("removal", "draw", "sweeper", "ramp")), 0)

    if overrides:
        apply_overrides(profile, overrides, slots, notes)

    candidates = build_candidates(commanders, identity, pool, profile, rate, budget, slots, notes)
    cheapest = budget is not None and budget / slots < CHEAP_AVERAGE

    for tag in profile.priority_tags:
        found = len({c.base_oracle for c in candidates if tag in c.tags})
        if found == 0:
            notes.append(f"Nenhuma carta com a tag {tag} cabe nas regras (cores, legalidade e orçamento).")

    if len(candidates) < slots:
        raise BuildError(
            f"Só encontrei {len(candidates)} cartas que cabem nas regras"
            f"{' e no orçamento' if budget is not None else ''}; preciso de {slots}."
        )

    picked = select(candidates, slots, profile)

    # Terrenos que geram duas cores (no lugar de básicos): com orçamento, uma
    # reserva separada antes de ajustar as mágicas; sem orçamento, até o limite
    reserve = (budget * FIXING_BUDGET_SHARE).quantize(Decimal("0.01")) if budget is not None else None
    fixing = pick_fixing_lands(identity, reserve, rate, notes, cheapest)
    lands_spent = sum((land.price for land in fixing), Decimal(0))
    spell_budget = budget - lands_spent if budget is not None else None

    fits = True
    if budget is not None:
        fits = fit_with_release(picked, candidates, spell_budget, profile, notes)
        if not fits and fixing:
            # Não coube: devolve o dinheiro dos terrenos às mágicas e tenta de novo
            fixing, lands_spent, spell_budget = [], Decimal(0), budget
            fits = fit_with_release(picked, candidates, spell_budget, profile, notes)
        if not fits:
            notes.append("Não foi possível chegar ao orçamento com as cartas encontradas.")

    if budget is not None and fits:
        # Sobra do orçamento em cartas melhores da mesma função
        use_leftover(picked, candidates, spell_budget, levels=(0, 1))

    # Tribal: criaturas da tribo no lugar de cartas da mesma função
    ceiling = spell_budget if fits else (sum((c.price for c in picked), Decimal(0)) if budget is not None else None)
    swaps = tribal_swaps(picked, candidates, profile, ceiling)
    if profile.tribes:
        strategy.append(
            f"Deck tribal ({', '.join(profile.tribes)}): {swaps} carta{'s' if swaps != 1 else ''} trocada{'s' if swaps != 1 else ''} "
            "por criaturas da tribo com a mesma função."
        )

    lands = split_basic_lands(pip_weights([c.data for c in picked], commanders, identity),
                              total=BASIC_LANDS_COUNT - len(fixing))

    spells_spent = sum((c.price for c in picked), Decimal(0))
    spent = spells_spent + lands_spent
    curve = {b: sum(c.bucket == b for c in picked) for b in CURVE_BUCKETS}

    report = {
        "commanders": [c["name"] for c in commanders],
        "identity": identity,
        "slots": slots,
        "budget": str(budget) if budget is not None else None,
        "spent": str(spent.quantize(Decimal("0.01"))),
        "spells_spent": str(spells_spent.quantize(Decimal("0.01"))),
        "lands_spent": str(lands_spent.quantize(Decimal("0.01"))),
        "average": str((spells_spent / slots).quantize(Decimal("0.01"))),
        "fits": fits,
        "profile_source": profile.source,
        "profile_sample": profile.sample_size,
        # Na ordem de prioridade (remoção, compra, board wipe, ramp)
        "roles": [
            {"label": ROLE_LABELS[key], "count": sum(c.assigned == key for c in picked),
             "target": profile.role_targets.get(key, 0)}
            for key in ROLE_PRIORITY
        ],
        "synergy_slots": sum(c.assigned is None for c in picked),
        "profile_targets": profile_targets,
        "overrides": overrides or None,
        "multi_function": sum(c.functions >= 2 for c in picked),
        "wanted_tags": wanted_tokens(profile),
        "user_tribes": profile.user_tribes,
        "wanted": [
            {"tag": tag,
             "candidates": len({c.base_oracle for c in candidates if tag in c.tags}),
             "in_deck": len({c.base_oracle for c in picked if tag in c.tags})}
            for tag in profile.priority_tags
        ],
        "tribes": profile.tribes,
        "tribe_members": sum(c.tribe_member for c in picked if c.is_creature),
        "voltron": profile.voltron,
        "voltron_pieces": sum(c.voltron_piece for c in picked),
        "relentless": [
            {"name": name, "copies": sum(c.name == name for c in picked)}
            for name in sorted({c.name for c in picked if "relentless" in c.tags})
        ],
        "creatures": sum(c.is_creature for c in picked),
        "creature_target": profile.creature_target,
        "curve": [
            {"cmc": f"{b}+" if b == 7 else str(b), "count": curve[b], "target": profile.curve_targets.get(b)}
            for b in CURVE_BUCKETS
        ],
        "themes": [
            {"tag": t["tag"], "share": round(t["share"] * 100), "lift": t["lift"],
             "count": sum(t["tag"] in c.tags for c in picked)}
            for t in profile.themes
        ],
        "rules": strategy,
        "from_edhrec": sum(c.source == "edhrec" for c in picked),
        "from_scryfall": sum(c.source == "scryfall" for c in picked),
        "estimated": sum(c.estimated for c in picked),
        "lands": [{"color": color, "name": BASIC_BY_COLOR[color], "count": n} for color, n in lands.items()],
        "fixing_lands": [{"name": land.data["name"], "price": str(land.price)} for land in fixing],
        "candidates": len(candidates),
        "notes": notes,
    }

    return Plan(commanders=commanders, identity=identity, picked=picked, lands=lands, report=report,
                fixing_lands=fixing)


def _basic_land_data(colors, client: ScryfallClient | None = None) -> dict[str, dict]:
    names = [BASIC_BY_COLOR[color] for color in colors]
    found = (client or ScryfallClient()).collection([{"name": name} for name in names])
    store_cards([data for data in found if data])
    return {color: data for color, data in zip(colors, found) if data}


def _decklist_text(plan: Plan) -> str:
    lines = ["Comandante"] + [f"1 {c['name']}" for c in plan.commanders] + ["", "Deck"]
    counts = {}
    for c in plan.picked:
        counts[c.name] = counts.get(c.name, 0) + 1
    lines += [f"{n} {name}" for name, n in counts.items()]
    lines += [f"1 {land.data['name']}" for land in plan.fixing_lands]
    lines += [f"{n} {BASIC_BY_COLOR[color]}" for color, n in plan.lands.items()]
    return "\n".join(lines)


@transaction.atomic
def save_plan(plan: Plan, budget: Decimal | None, deck: Deck | None = None,
              client: ScryfallClient | None = None) -> Deck:
    lands = _basic_land_data(list(plan.lands), client)

    if deck is None:
        deck = Deck.objects.create(raw_text="")
    else:
        deck.cards.all().delete()

    rows = []

    for data in plan.commanders:
        rows.append(DeckCard(name=data["name"], input_name=data["name"], scryfall_id=data["id"],
                             oracle_id=card_fields(data)["oracle_id"], is_commander=True))

    # Cópias de cartas "relentless" viram uma linha com a quantidade
    grouped = {}
    for candidate in sorted(plan.picked, key=lambda c: (c.copy_index, c.category, c.name)):
        grouped.setdefault(candidate.base_oracle, []).append(candidate)

    for copies in sorted(grouped.values(), key=lambda cs: (cs[0].category, cs[0].name)):
        first = copies[0]
        rows.append(DeckCard(quantity=len(copies), name=first.name, input_name=first.name,
                             scryfall_id=first.data["id"], oracle_id=first.base_oracle,
                             category=first.category))

    for land in plan.fixing_lands:
        rows.append(DeckCard(name=land.data["name"], input_name=land.data["name"],
                             scryfall_id=land.data["id"], oracle_id=card_fields(land.data)["oracle_id"],
                             category="Terreno"))

    for color, count in plan.lands.items():
        data = lands.get(color)
        name = BASIC_BY_COLOR[color]
        rows.append(DeckCard(quantity=count, name=data["name"] if data else "", input_name=name,
                             scryfall_id=data["id"] if data else "",
                             oracle_id=card_fields(data)["oracle_id"] if data else "",
                             category="Terreno básico"))

    for position, row in enumerate(rows):
        row.deck = deck
        row.position = position

    DeckCard.objects.bulk_create(rows)

    if not deck.name_locked:
        deck.name = " + ".join(c["name"] for c in plan.commanders)[:255]
    deck.raw_text = _decklist_text(plan)
    deck.budget = budget
    deck.build_report = plan.report
    deck.save()

    # Preços reais da LigaMagic para o que entrou com estimativa
    price_queue.request([c.name for c in plan.picked if c.estimated]
                        + [land.data["name"] for land in plan.fixing_lands if land.estimated])

    return deck


def build_deck(commander_names, budget, rate, deck: Deck | None = None,
               client: ScryfallClient | None = None, overrides: dict | None = None,
               priority_tags: list[str] | None = None) -> Deck:
    plan = plan_deck(commander_names, budget, rate, client, overrides, priority_tags)
    return save_plan(plan, budget, deck, client)
