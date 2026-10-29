"""
Escolha de tags a valorizar na construção do deck.

- commander_tag_options: as tags do(s) comandante(s), sem as genéricas ou sem
  significado de jogo, com as que parecem sinergia já sugeridas.
- tag_suggestions: autocompletar sobre todas as tags de função do Scryfall.
"""

import re

from django.core.cache import cache

from cards.models import TagIndex, TagStat
from cards.services.profile import _is_theme_candidate, creature_types
from cards.services.scryfall import ScryfallClient, card_fields
from cards.services.tags import last_sync, tag_counts, tag_descriptions, tags_for


# Tags que descrevem uma sinergia, e não só uma habilidade genérica
SUGGESTED_RE = re.compile(r"(synergy|matters?$|-matters-|^typal-|^tribal-)")

# Genéricas demais para ser "o plano do deck"
EXTRA_GENERIC = {
    "kindred", "typal-creature", "modal", "per-opponent", "per-player", "multiplayer",
    "saboteur", "evasion", "flying", "haste", "vigilance", "lifelink", "deathtouch",
    "gains-lifelink", "gains-menace", "gains-haste", "gains-flying",
}

# Mecânicas do texto do comandante que as tags dele nem sempre cobrem
TEXT_HINTS = [
    (re.compile(r"\bcrime"), ("repeatable-crime", "synergy-target", "unheroic")),
]

TYPES_CACHE_KEY = "c10magic:creature-types"

MIN_COUNT = 5        # menos que isso não vale uma tag
MAX_COUNT = 3500     # mais que isso é amplo demais para guiar o deck
CACHE_SECONDS = 60 * 60


def humanize(label: str) -> str:
    return label.replace("-", " ")


def _resolve(client: ScryfallClient, name: str) -> dict | None:
    """Carta pelo nome (aproximado); com só parte do nome, a 1ª sugestão do Scryfall."""

    data = client.named_fuzzy(name)

    if not data:
        suggestions = client.autocomplete(name)
        if suggestions:
            data = client.named_fuzzy(suggestions[0])

    return data


def commander_tag_options(names: list[str], client: ScryfallClient | None = None) -> dict:
    """
    {"commanders": [...], "ready": bool, "tags": [{label, text, count, suggested}]}

    Levanta LookupError se o comandante não for encontrado.
    """

    names = [n.strip() for n in names if n and n.strip()]
    key = "c10magic:tagpicker:" + "|".join(n.lower() for n in names)
    cached = cache.get(key)

    if cached is not None and cached["ready"]:
        return cached

    client = client or ScryfallClient()
    commanders, labels, types, hints = [], {}, [], []

    for name in names:
        data = _resolve(client, name)
        if not data:
            raise LookupError(f"Carta não encontrada: {name}")

        commanders.append(data["name"])
        fields = card_fields(data)
        types += [t.title() for t in sorted(creature_types(fields["type_line"])) if t.title() not in types]

        text = (fields.get("oracle_text") or "").lower()
        for pattern, extra in TEXT_HINTS:
            if pattern.search(text):
                hints += [h for h in extra if h not in hints]

        for label, _ in tags_for(fields["oracle_id"])[TagIndex.ORACLE]:
            labels[label] = True

    hinted = set(hints)
    for label in hints:
        labels.setdefault(label, True)

    counts = tag_counts(labels)
    descriptions = tag_descriptions(labels)

    tags = []
    for label in labels:
        count = counts.get(label)
        if count is None or count < MIN_COUNT or (count > MAX_COUNT and label not in hinted):
            continue
        if label in EXTRA_GENERIC or not _is_theme_candidate(label):
            continue
        tags.append({
            "label": label,
            "text": humanize(label),
            "count": count,
            "suggested": bool(SUGGESTED_RE.search(label)) or label in hinted,
            "description": descriptions.get(label, ""),
        })

    # Sugeridas primeiro; dentro de cada grupo, as mais específicas (menos cartas)
    tags.sort(key=lambda t: (not t["suggested"], t["count"], t["label"]))

    result = {
        "commanders": commanders,
        "ready": last_sync(TagIndex.ORACLE) is not None,
        "tags": tags,
        "types": types,
    }

    if result["ready"]:
        cache.set(key, result, CACHE_SECONDS)

    return result


def tag_suggestions(query: str, limit: int = 15) -> list[dict]:
    """Tags de função que combinam com o texto, as que começam com ele primeiro."""

    query = (query or "").strip().lower().replace(" ", "-")

    if len(query) < 2:
        return []

    rows = TagStat.objects.filter(
        kind=TagIndex.ORACLE, label__icontains=query, count__gte=2
    ).order_by("-count")[:300]

    ranked = sorted(rows, key=lambda t: (not t.label.startswith(query), -t.count))

    return [
        {"label": t.label, "text": humanize(t.label), "count": t.count, "description": t.description}
        for t in ranked[:limit]
    ]


def creature_type_suggestions(query: str, limit: int = 15, client: ScryfallClient | None = None) -> list[str]:
    """Tipos de criatura (catálogo do Scryfall, guardado por um dia) que combinam com o texto."""

    query = (query or "").strip().lower()

    if len(query) < 2:
        return []

    catalog = cache.get(TYPES_CACHE_KEY)
    if catalog is None:
        catalog = (client or ScryfallClient()).creature_types()
        if catalog:
            cache.set(TYPES_CACHE_KEY, catalog, 24 * CACHE_SECONDS)

    found = [t for t in catalog if query in t.lower()]

    return sorted(found, key=lambda t: (not t.lower().startswith(query), t))[:limit]
