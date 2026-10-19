"""
Construtor: monta um deck de Commander a partir do(s) comandante(s).

1. Perfil do comandante (cards/services/profile.py): proporção de papéis,
   curva de mana, criaturas e temas tirados do deck médio do EDHREC, além
   das funções do próprio comandante (ex.: reduz o custo de criaturas).
2. Candidatas: o pool do EDHREC + as cartas mais populares do Scryfall para
   cada papel, tema e faixa de custo importante, dentro da identidade de cor.
3. Nota: % de decks do comandante que usam a carta e sinergia (EDHREC);
   bônus para temas do deck e para o que as funções do comandante favorecem.
4. Esqueleto: cotas de papel do perfil; o resto por nota, seguindo a curva
   de mana e a quantidade de criaturas dos decks reais.
5. Orçamento (só as mágicas; comandante e básicos não contam): troca as
   cartas que mais economizam perdendo menos nota, mantendo papel, "é
   criatura" e faixa de custo, até caber; depois usa a sobra.
6. 40 terrenos básicos divididos pelos símbolos de mana do deck.
"""

import hashlib
import math
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from decimal import Decimal

import requests
from django.conf import settings
from django.core.cache import cache
from django.db import transaction

from cards.models import COLOR_ORDER, Deck, DeckCard, TagIndex
from cards.services.catalog import get_cards_data, store_cards
from cards.services.deck import can_be_commander, group_of, identity_letters, is_basic_land
from cards.services.edhrec import commander_pool
from cards.services.exchange import usd_to_brl
from cards.services.prices import known_prices, price_queue
from cards.services.profile import (
    COMMANDER_RULES,
    CURVE_BUCKETS,
    ROLE_LABELS,
    ROLE_PRIORITY,
    LAND_RAMP_TAGS,
    LAND_SYNERGY_TAGS,
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

# Orçamento até este valor: o deck não leva ramp nem pedras de mana (cada
# centavo vai para sinergia, remoção e compra), a não ser que o comandante
# tenha sinergia com terrenos (landfall, terreno extra…)
LOW_BUDGET_LIMIT = Decimal("10")

# Média por carta abaixo disto: também procura as cartas mais baratas de cada papel
CHEAP_AVERAGE = Decimal("0.40")

# Nenhuma carta sozinha pode levar mais que esta fração do orçamento
MAX_SHARE_OF_BUDGET = Decimal("0.3")

# Cartas que só vieram do Scryfall: nota máxima (as do EDHREC vão de ~0,15 a ~2)
SCRYFALL_SCORE = 0.15
EDHREC_BONUS = 0.15

# Sinergia vale mais que qualquer papel: peso da sinergia do EDHREC e dos temas
SYNERGY_WEIGHT = 0.9
THEME_BONUS = 0.07          # um tema já vale mais que o melhor papel (remoção, 0,06)
THEME_BONUS_MAX = 0.21

# Bônus do melhor papel da carta, na ordem de prioridade (ramp por último)
ROLE_BONUS = {"removal": 0.06, "draw": 0.045, "sweeper": 0.03, "ramp": 0.0}

# Cada função útil além da primeira (papel, tema do deck, o que o comandante favorece)
MULTI_FUNCTION_BONUS = 0.05
MULTI_FUNCTION_MAX = 0.15

# Deck tribal: criaturas da tribo (ou com Changeling) valem mais que um tema;
# cartas que citam a tribo no texto (payoffs), um pouco menos
TRIBE_MEMBER_BONUS = 0.12
TRIBE_PAYOFF_BONUS = 0.06
# Pelo menos esta fração das criaturas do deck deve ser da tribo (se houver cartas)
TRIBE_MIN_SHARE = 0.5

# Cartas "relentless" (o deck pode ter quantas quiser, ex.: Tempest Hawk,
# Relentless Rats) com sinergia com o deck entram com várias cópias
RELENTLESS_COPIES = 8
RELENTLESS_MIN_SYNERGY = 0.05
# Fica melhor quanto mais cópias (cada uma busca a próxima): bônus para entrar com as cópias
RELENTLESS_BONUS = 0.25

IRREGULAR_PLURALS = {"elf": "elves", "dwarf": "dwarves", "wolf": "wolves", "werewolf": "werewolves",
                     "fungus": "fungi", "octopus": "octopi", "mouse": "mice", "ox": "oxen"}

# Quanto vale proteger a vaga ao economizar / melhorar com a sobra do orçamento:
# sinergia (None) > remoção > compra > board wipe > ramp
PRIORITY_WEIGHT = {None: 1.6, "removal": 1.3, "draw": 1.15, "sweeper": 1.0, "ramp": 0.7}

CREATURE_TOLERANCE = 3
CURVE_TOLERANCE = 1

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
    functions: int = 0            # papéis + temas + o que o comandante favorece
    tribe_member: bool = False    # criatura da tribo do deck (ou Changeling)
    tribe_payoff: bool = False    # cita a tribo no texto
    copy_index: int = 0           # cópia extra de carta "relentless" (0 = original)
    locked: bool = False          # pacote "relentless": não sai para melhorar nota nem curva

    @property
    def category(self):
        return ROLE_LABELS.get(self.assigned, FILLER_LABEL)

    @property
    def priority(self) -> float:
        return PRIORITY_WEIGHT.get(self.assigned, 1.0)

    @property
    def base_oracle(self) -> str:
        """oracle_id sem o sufixo das cópias extras ("abc#2" → "abc")."""
        return self.oracle_id.split("#")[0]

    @property
    def pure_ramp(self) -> bool:
        """Só ramp, sem tema nem outra função: entra apenas pela cota de ramp."""
        return self.roles == {"ramp"} and self.functions <= 1

    @property
    def bucket(self):
        return bucket_of(self.cmc)


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
        return usd_to_brl(Decimal(usd), rate), True

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

    return True


def candidate_queries(identity: str, max_usd: Decimal | None, profile: Profile,
                      cheapest: bool = False) -> list[tuple[str, str]]:
    """
    Buscas no Scryfall, como (busca, ordem): papéis, temas e faixas de custo
    importantes para o comandante, das mais populares. Com cheapest=True
    (orçamento muito baixo), também as mais baratas de cada papel e tema.
    """

    base = [f"id<={identity.lower()}" if identity else "id:c", "f:commander", "game:paper", "-t:land"]
    if max_usd is not None:
        base.append(f"usd<={max_usd}")

    def query(*terms):
        return " ".join([*terms, *base])

    land_deck = bool(profile.land_synergy)
    queries = [
        query(f"({' or '.join(f'otag:{t}' for t in sorted(ramp_tags_for(land_deck) if key == 'ramp' else tags))})")
        for key, _, tags, _ in ROLES
        if not (profile.skip_ramp and key == "ramp")
    ]
    queries += [query(f"otag:{theme['tag']}") for theme in profile.themes[:3]]

    if profile.creature_target and profile.creature_target >= 18:
        queries.append(query("t:creature", "mv<=4"))
    if profile.high_curve_share >= 0.2 or profile.rules:
        queries.append(query("t:creature" if profile.creature_target else "", "mv>=5"))

    # Deck tribal: criaturas da tribo e cartas "relentless" (várias cópias)
    for tribe in profile.tribes:
        queries.append(query(f"t:{tribe}"))
    if profile.tribes:
        queries.append(query("otag:relentless"))

    queries.append(query())

    queries = [q.replace("  ", " ").strip() for q in queries]
    ordered = [(q, "edhrec") for q in queries]

    if cheapest:
        # Papéis, temas e o geral, das mais baratas (as mais populares
        # raramente são as de R$ 0,05 que um orçamento assim precisa)
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


def apply_bonuses(candidate: Candidate, themes: set[str], rules: list[dict]):
    """Soma à nota: sinergia (temas, comandante), melhor papel e várias funções."""

    # Sinergia: temas do deck e o que as funções do comandante favorecem
    matched_themes = len(candidate.tags & themes)
    candidate.score += min(THEME_BONUS * matched_themes, THEME_BONUS_MAX)

    favored = 0
    for rule in rules:
        if rule["applies"](candidate):
            candidate.score += rule["bonus"](candidate)
            favored += 1

    # Deck tribal: a tribo é a sinergia principal
    if candidate.tribe_member:
        candidate.score += TRIBE_MEMBER_BONUS
    elif candidate.tribe_payoff:
        candidate.score += TRIBE_PAYOFF_BONUS
    tribal = candidate.tribe_member or candidate.tribe_payoff

    # Papel: vale o melhor (remoção > compra > board wipe > ramp)
    candidate.score += max((ROLE_BONUS[role] for role in candidate.roles), default=0)

    # Mais de uma função útil ao deck
    candidate.functions = len(candidate.roles) + matched_themes + favored + tribal
    candidate.score += min(MULTI_FUNCTION_BONUS * max(candidate.functions - 1, 0), MULTI_FUNCTION_MAX)


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
            candidate.score += RELENTLESS_BONUS
            candidate.functions += 1
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

    # Média abaixo de R$ 0,40 por carta: busca também as mais baratas
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

    themes = profile.theme_tags
    rules = [COMMANDER_RULES[rule["index"]] for rule in profile.rules]
    land_deck = bool(profile.land_synergy)
    # Em deck de terrenos, sinergia com terrenos conta como tema
    bonus_themes = themes | LAND_SYNERGY_TAGS if land_deck else themes

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

        pool_card = pool_cards.get(data["id"])
        if pool_card:
            score = EDHREC_BONUS + (pool_card.inclusion or 0) + SYNERGY_WEIGHT * max(pool_card.synergy or 0, 0)
            source = "edhrec"
        else:
            rank = data.get("edhrec_rank") or 30000
            score = SCRYFALL_SCORE * (1 - min(rank, 30000) / 30000)
            source = "scryfall"

        card_tags = tags.get(fields["oracle_id"], frozenset())
        roles = {key for key, _, role_tags, _ in ROLES if card_tags & role_tags}

        if land_deck and "ramp" in roles and not card_tags & LAND_RAMP_TAGS:
            # Deck de terrenos: mana dork / pedra de mana não é o ramp que o deck quer.
            # Só fica se tiver outra função ou sinergia com terrenos (ex.: Lotus Cobra).
            roles.discard("ramp")
            if not roles and not card_tags & (themes | LAND_SYNERGY_TAGS):
                continue

        if profile.skip_mana_rocks and is_mana_rock(card_tags):
            continue

        if profile.skip_ramp:
            # Ramp só se a carta fizer outra coisa útil (e aí o ramp não conta como função)
            if roles == {"ramp"} and not card_tags & themes:
                continue
            roles.discard("ramp")

        candidate = Candidate(
            data=data,
            name=data["name"],
            oracle_id=fields["oracle_id"],
            roles=roles,
            score=score,
            price=price,
            estimated=estimated,
            source=source,
            is_creature="Creature" in fields["type_line"].split(" // ")[0],
            cmc=float(data.get("cmc") or 0),
            tags=card_tags,
            synergy=pool_card.synergy if pool_card else None,
            inclusion=pool_card.inclusion if pool_card else None,
            tribe_member=_is_tribe_member(data, fields, profile.tribes),
            tribe_payoff=_mentions_tribe(fields, profile.tribes),
        )

        apply_bonuses(candidate, bonus_themes, rules)

        current = by_oracle.get(candidate.oracle_id)
        if current is None or candidate.score > current.score:
            by_oracle[candidate.oracle_id] = candidate

    return add_relentless_copies(list(by_oracle.values()), themes)


# ============================================================
# ESCOLHA
# ============================================================

class Shape:
    """Contagem de criaturas e da curva enquanto o deck é montado."""

    def __init__(self, profile: Profile):
        self.profile = profile
        self.creatures = 0
        self.curve = {b: 0 for b in CURVE_BUCKETS}

    def add(self, c: Candidate):
        self.creatures += c.is_creature
        self.curve[c.bucket] += 1

    def remove(self, c: Candidate):
        self.creatures -= c.is_creature
        self.curve[c.bucket] -= 1

    def fits(self, c: Candidate, curve_slack: int | None = CURVE_TOLERANCE) -> bool:
        target = self.profile.creature_target
        if target is not None and c.is_creature and self.creatures >= target + CREATURE_TOLERANCE:
            return False

        if curve_slack is not None and self.profile.curve_targets:
            if self.curve[c.bucket] >= self.profile.curve_targets.get(c.bucket, 0) + curve_slack:
                return False

        return True


def select(candidates: list[Candidate], slots: int, profile: Profile) -> list[Candidate]:
    ranked = sorted(candidates, key=lambda c: -c.score)
    picked, used = [], set()
    shape = Shape(profile)

    def take(candidate, role=None):
        candidate.assigned = role
        picked.append(candidate)
        used.add(candidate.oracle_id)
        shape.add(candidate)

    # 1. Cotas de papel na ordem de prioridade (remoção, compra, board wipe,
    #    ramp por último), já respeitando a curva com folga. Uma carta que
    #    serve a dois papéis fica no mais importante.
    for key in ROLE_PRIORITY:
        quota = profile.role_targets.get(key, 0)
        for slack in (CURVE_TOLERANCE + 1, None):
            for candidate in ranked:
                if len(picked) >= slots or sum(c.assigned == key for c in picked) >= quota:
                    break
                if candidate.oracle_id in used or key not in candidate.roles:
                    continue
                if shape.fits(candidate, slack):
                    take(candidate, key)

    # 1b. Pacote "relentless": as cópias da melhor carta que o deck pode ter
    #     repetida (ex.: Tempest Hawk) entram juntas, sem o limite de curva e
    #     de criaturas, porque o deck é montado em torno dela
    package = [c for c in ranked if c.copy_index and c.oracle_id not in used]
    if package:
        best_base = max(package, key=lambda c: c.score).base_oracle
        for candidate in ranked:
            if len(picked) >= slots:
                break
            if candidate.base_oracle == best_base and candidate.oracle_id not in used:
                take(candidate)
                candidate.locked = True

    # 2. O resto (vagas de sinergia) pela nota, seguindo a curva e as
    #    criaturas; depois afrouxa. Ramp puro só entra pela cota de ramp.
    for slack in (CURVE_TOLERANCE, CURVE_TOLERANCE + 2, None):
        for candidate in ranked:
            if len(picked) >= slots:
                break
            if candidate.oracle_id in used or candidate.pure_ramp:
                continue
            if shape.fits(candidate, slack):
                take(candidate)

    # 3. Faltou: completa com o que houver
    for candidate in ranked:
        if len(picked) >= slots:
            break
        if candidate.oracle_id not in used:
            take(candidate)

    # 4. Criaturas de menos: troca sinergias não-criatura pelas melhores criaturas
    target = profile.creature_target
    if target is not None:
        while shape.creatures < target - CREATURE_TOLERANCE:
            spare = [c for c in picked if c.assigned is None and not c.is_creature]
            options = [c for c in ranked if c.is_creature and c.oracle_id not in used]
            if not spare or not options:
                break
            worst = min(spare, key=lambda c: c.score)
            best = options[0]
            picked[picked.index(worst)] = best
            used.discard(worst.oracle_id)
            used.add(best.oracle_id)
            shape.remove(worst)
            shape.add(best)
            best.assigned = None

    # 5. Deck tribal com poucas criaturas da tribo: troca criaturas de fora
    #    (nas vagas de sinergia) pelas melhores criaturas da tribo
    if profile.tribes:
        while True:
            creatures = [c for c in picked if c.is_creature]
            members = sum(c.tribe_member for c in creatures)
            if not creatures or members >= TRIBE_MIN_SHARE * len(creatures):
                break
            spare = [c for c in creatures if c.assigned is None and not c.tribe_member]
            options = [c for c in ranked if c.tribe_member and c.is_creature and c.oracle_id not in used]
            if not spare or not options:
                break
            worst = min(spare, key=lambda c: c.score)
            best = options[0]
            picked[picked.index(worst)] = best
            used.discard(worst.oracle_id)
            used.add(best.oracle_id)
            shape.remove(worst)
            shape.add(best)
            best.assigned = None

    return picked


def _compatible(out: Candidate, new: Candidate, level: int) -> bool:
    """
    Troca que mantém o papel e o "é criatura" da carta que sai, e a faixa
    de custo: mesma faixa (nível 0), faixa vizinha (1) ou qualquer (2).
    """

    if out.assigned is not None and out.assigned not in new.roles:
        return False
    if out.assigned is None and new.pure_ramp:
        return False
    if out.tribe_member and not new.tribe_member:
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
    """Troca cartas até caber no orçamento. Retorna se coube."""

    used = {c.oracle_id for c in picked}
    pool = sorted((c for c in candidates if c.oracle_id not in used), key=lambda c: c.price)

    total = sum((c.price for c in picked), Decimal(0))

    while total > budget:
        best = None

        # Primeiro tenta manter a faixa de custo e o "é criatura"; só afrouxa
        # se não houver troca. No último nível (3), mantém só o papel e aceita
        # criatura por não-criatura se a contagem de criaturas seguir na tolerância.
        for level in (0, 1, 2, 3):
            for out in picked:
                for new in pool:
                    if new.price >= out.price:
                        break
                    if level < 3 and not _compatible(out, new, level):
                        continue
                    if level == 3:
                        if out.assigned is not None and out.assigned not in new.roles:
                            continue
                        if out.assigned is None and new.pure_ramp:
                            continue
                        if out.tribe_member and not new.tribe_member:
                            continue
                        if not _keeps_creature_count(picked, out, new, creature_target):
                            continue
                    saved = float(out.price - new.price)
                    # Perder qualidade em ramp custa menos que em sinergia
                    lost = max(out.score - new.score, 0.0005) * out.priority
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
    """Com sobra no orçamento, troca por cartas de nota maior que ainda caibam."""

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


def rebalance_curve(picked: list[Candidate], candidates: list[Candidate], profile: Profile,
                    budget: Decimal | None, rounds: int = 60):
    """
    Aproxima a curva da dos decks reais: tira cartas das faixas que sobram e
    põe nas que faltam (só quando a diferença passa da tolerância), mantendo
    papel, "é criatura" e o orçamento.
    """

    targets = profile.curve_targets
    if not targets:
        return

    used = {c.oracle_id for c in picked}
    pool = [c for c in candidates if c.oracle_id not in used]
    total = sum((c.price for c in picked), Decimal(0))
    creature_target = profile.creature_target

    def keeps_creatures(out, new):
        """Pode trocar criatura por não-criatura se a contagem seguir na tolerância."""

        if out.is_creature == new.is_creature:
            return True
        if creature_target is None:
            return False
        creatures = sum(c.is_creature for c in picked) - out.is_creature + new.is_creature
        return abs(creatures - creature_target) <= CREATURE_TOLERANCE

    for _ in range(rounds):
        counts = {b: 0 for b in CURVE_BUCKETS}
        for c in picked:
            counts[c.bucket] += 1

        under = {b for b in CURVE_BUCKETS if counts[b] < targets.get(b, 0) - CURVE_TOLERANCE}
        over = {b for b in CURVE_BUCKETS if counts[b] > targets.get(b, 0) + CURVE_TOLERANCE}

        if not under or not over:
            break

        best = None

        for out in picked:
            if out.bucket not in over or out.locked:
                continue
            for new in pool:
                if new.bucket not in under:
                    continue
                if out.assigned is not None and out.assigned not in new.roles:
                    continue
                if out.assigned is None and new.pure_ramp:
                    continue
                if out.tribe_member and not new.tribe_member:
                    continue
                if not keeps_creatures(out, new):
                    continue
                if budget is not None and total - out.price + new.price > budget:
                    continue
                change = new.score - out.score
                if best is None or change > best[0]:
                    best = (change, out, new)

        if best is None:
            break

        _, out, new = best
        new.assigned = out.assigned
        picked[picked.index(out)] = new
        pool.remove(new)
        pool.append(out)
        total += new.price - out.price


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


def plan_deck(commander_names: list[str], budget: Decimal | None, rate,
              client: ScryfallClient | None = None, overrides: dict | None = None) -> Plan:
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

    pool = commander_pool([c["name"] for c in commanders])
    if not pool.found:
        notes.append("O EDHREC não tem página para este comandante: só cartas populares do Scryfall.")

    profile = build_profile(commanders, pool, slots)

    if budget is not None and budget <= LOW_BUDGET_LIMIT:
        profile.skip_mana_rocks = True
        if profile.land_synergy:
            notes.append(
                f"Orçamento de até R$ {LOW_BUDGET_LIMIT}: sem pedras de mana; o ramp de terreno foi mantido "
                f"porque o comandante tem sinergia com terrenos ({', '.join(profile.land_synergy[:3])})."
            )
        else:
            profile.skip_ramp = True
            profile.role_targets["ramp"] = 0
            notes.append(
                f"Orçamento de até R$ {LOW_BUDGET_LIMIT}: sem ramp nem pedras de mana; "
                "essas vagas foram para sinergia, remoção e compra."
            )

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

    # Curva antes de gastar a sobra: o ajuste precisa de folga no orçamento
    # Sem caber no orçamento, o ajuste não pode encarecer o deck
    ceiling = spell_budget if fits else (sum((c.price for c in picked), Decimal(0)) if budget is not None else None)
    rebalance_curve(picked, candidates, profile, ceiling)

    if budget is not None and fits:
        # Sobra do orçamento em cartas melhores, sem mexer na curva
        use_leftover(picked, candidates, spell_budget, levels=(0,))

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
        "tribes": profile.tribes,
        "tribe_members": sum(c.tribe_member for c in picked if c.is_creature),
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
        "rules": [rule["label"] for rule in profile.rules],
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
               client: ScryfallClient | None = None, overrides: dict | None = None) -> Deck:
    plan = plan_deck(commander_names, budget, rate, client, overrides)
    return save_plan(plan, budget, deck, client)
