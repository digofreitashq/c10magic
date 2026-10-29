"""
Dados do EDHREC (https://edhrec.com).

O EDHREC não tem API oficial: o site carrega arquivos JSON públicos em
json.edhrec.com/pages/... Cada página é guardada no banco (EdhrecPage) e
só é baixada de novo depois de uma semana.
"""

import logging
import re
import unicodedata
from dataclasses import dataclass, field

import requests
from django.conf import settings
from django.utils import timezone

from cards.models import EdhrecPage


log = logging.getLogger(__name__)

EDHREC_JSON = "https://json.edhrec.com/pages"

CATEGORY_LABELS = {
    "newcards": "Novidades",
    "highsynergycards": "Alta sinergia",
    "highliftcards": "Alta sinergia",
    "topcards": "Mais jogadas",
    "gamechangers": "Game changers",
    "creatures": "Criaturas",
    "instants": "Mágicas instantâneas",
    "sorceries": "Feitiços",
    "utilityartifacts": "Artefatos",
    "manaartifacts": "Artefatos de mana",
    "enchantments": "Encantamentos",
    "planeswalkers": "Planeswalkers",
    "battles": "Batalhas",
    "utilitylands": "Terrenos utilitários",
    "lands": "Terrenos",
}

TYPE_LABELS = {
    "Creature": "Criaturas",
    "Instant": "Mágicas instantâneas",
    "Sorcery": "Feitiços",
    "Artifact": "Artefatos",
    "Enchantment": "Encantamentos",
    "Planeswalker": "Planeswalkers",
    "Battle": "Batalhas",
    "Land": "Terrenos",
}


def slugify(name: str) -> str:
    """'Atraxa, Praetors' Voice' -> 'atraxa-praetors-voice'."""

    front = name.split(" // ")[0]
    ascii_name = unicodedata.normalize("NFKD", front).encode("ascii", "ignore").decode()
    ascii_name = re.sub(r"['’,.]", "", ascii_name.lower())

    return re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")


def commander_path(commander_names: list[str]) -> str:
    # Duplas (partner/background): o EDHREC junta os nomes em ordem alfabética
    slugs = sorted(slugify(name) for name in commander_names)
    return "commanders/" + "-".join(slugs)


def get_page(path: str, _hops: int = 0) -> dict | None:
    """
    JSON da página, do banco se tiver menos de uma semana.

    O EDHREC às vezes responde só {"redirect": "/commanders/..."} (por
    exemplo, para duplas de comandantes em outra ordem): segue o redirect.
    """

    data = _get_page(path)

    if data and set(data) == {"redirect"} and _hops < 3:
        return get_page(str(data["redirect"]).strip("/"), _hops + 1)

    return data


def _get_page(path: str) -> dict | None:
    page = EdhrecPage.objects.filter(path=path).first()

    if page and not page.is_stale():
        return page.data

    try:
        response = requests.get(
            f"{EDHREC_JSON}/{path}.json",
            headers={"User-Agent": settings.USER_AGENT, "Accept": "application/json"},
            timeout=30,
        )
    except requests.RequestException as exc:
        log.warning("EDHREC indisponível (%s): %s", path, exc)
        # Melhor o dado antigo do que nada
        return page.data if page else None

    if response.status_code in (403, 404):
        data, error = None, "Página não encontrada no EDHREC."
    elif not response.ok:
        return page.data if page else None
    else:
        data, error = response.json(), ""

    EdhrecPage.objects.update_or_create(
        path=path,
        defaults={"data": data, "error": error, "updated_at": timezone.now()},
    )

    return data


@dataclass
class PoolCard:
    scryfall_id: str
    name: str
    category: str
    synergy: float | None
    inclusion: float | None
    num_decks: int | None


@dataclass
class Pool:
    found: bool
    url: str = ""
    num_decks: int | None = None
    categories: list[tuple[str, str, list[PoolCard]]] = field(default_factory=list)
    type_distribution: dict[str, int] = field(default_factory=dict)
    # {"1": 7, "2": 16, ...}: quantas mágicas de cada valor de mana no deck típico
    mana_curve: dict[str, int] = field(default_factory=dict)
    # Temas dos decks do comandante, do mais comum ao menos: [("Voltron", 120), ...]
    themes: list[tuple[str, int]] = field(default_factory=list)
    budget: bool = False      # página dos decks baratos (commanders/<nome>/budget)

    @property
    def cards(self) -> dict[str, PoolCard]:
        """Uma entrada por carta (a primeira categoria em que aparece)."""

        unique = {}
        for _, _, cards in self.categories:
            for card in cards:
                unique.setdefault(card.scryfall_id, card)
        return unique


def commander_pool(commander_names: list[str], budget: bool = False) -> Pool:
    """
    Cartas jogadas com o comandante. Com budget=True, a página dos decks
    baratos do EDHREC (as porcentagens são entre esses decks); se ela não
    existir, a página normal.
    """

    if budget:
        pool = _commander_pool(commander_names, "/budget")
        if pool.found:
            return pool

    return _commander_pool(commander_names)


def _commander_pool(commander_names: list[str], suffix: str = "") -> Pool:
    def has_cards(page):
        return bool(((page or {}).get("container") or {}).get("json_dict", {}).get("cardlists"))

    path = commander_path(commander_names) + suffix
    data = get_page(path)

    # Dupla sem página própria: usa o primeiro comandante
    if not has_cards(data) and len(commander_names) > 1:
        path = commander_path(commander_names[:1]) + suffix
        data = get_page(path)

    if not has_cards(data):
        return Pool(found=False)

    container = (data.get("container") or {}).get("json_dict") or {}
    card = container.get("card") or {}

    categories = []

    for cardlist in container.get("cardlists") or []:
        tag = cardlist.get("tag") or ""
        cards = [
            PoolCard(
                scryfall_id=view.get("id") or "",
                name=view.get("name") or "",
                category=tag,
                synergy=view.get("synergy"),
                inclusion=(
                    view["num_decks"] / view["potential_decks"]
                    if view.get("num_decks") and view.get("potential_decks") else None
                ),
                num_decks=view.get("num_decks"),
            )
            for view in cardlist.get("cardviews") or []
            if view.get("id")
        ]

        if cards:
            label = CATEGORY_LABELS.get(tag, cardlist.get("header") or tag)
            categories.append((tag, label, cards))

    panels = data.get("panels") or {}
    piechart = (panels.get("piechart") or {}).get("content") or []
    mana_curve = panels.get("mana_curve") if isinstance(panels.get("mana_curve"), dict) else {}
    taglinks = panels.get("taglinks") if isinstance(panels.get("taglinks"), list) else []

    return Pool(
        found=True,
        url="https://edhrec.com/" + path,
        num_decks=card.get("num_decks"),
        categories=categories,
        type_distribution={item["label"]: item["value"] for item in piechart if "label" in item},
        mana_curve={str(k): int(v) for k, v in mana_curve.items() if str(v).isdigit()},
        themes=[(t["value"], int(t.get("count") or 0)) for t in taglinks if isinstance(t, dict) and t.get("value")],
        budget=bool(suffix),
    )
