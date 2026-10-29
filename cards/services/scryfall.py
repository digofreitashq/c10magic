import threading
import time

import requests
from django.conf import settings


SCRYFALL_PAGE_SIZE = 175
COLLECTION_BATCH = 75

# O Scryfall limita /cards/search, /cards/named e /cards/collection a ~2
# requisições por segundo. O intervalo vale para todas as threads.
SEARCH_INTERVAL = 0.55
_search_lock = threading.Lock()
_last_search = 0.0


def _throttle_search():
    global _last_search

    with _search_lock:
        wait = _last_search + SEARCH_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_search = time.monotonic()


class ScryfallQueryError(ValueError):
    """O Scryfall recusou a busca (erro de sintaxe)."""


class ScryfallClient:

    def __init__(self, delay: float | None = None):
        self.delay = settings.SCRYFALL_DELAY if delay is None else delay

        self.session = requests.Session()

        self.session.headers.update({
            "User-Agent": settings.USER_AGENT,
            "Accept": "application/json",
        })

    def _get(self, url, retries=3, **kwargs):
        for attempt in range(retries):
            if "/cards/named" in url or "/cards/search" in url:
                _throttle_search()

            response = self.session.get(url, timeout=30, **kwargs)

            if response.status_code == 429:
                # Rate limit: espera e tenta de novo
                time.sleep(2 * (attempt + 1))
                continue

            if response.status_code == 404:
                time.sleep(self.delay)
                return None

            response.raise_for_status()

            time.sleep(self.delay)

            return response.json()

        raise RuntimeError("Scryfall rate limit atingido.")

    def search_page(
        self,
        query: str,
        page: int = 1,
        unique: str = "cards",
        order: str = "name",
        direction: str = "auto",
    ) -> dict:
        """
        Uma página (até 175 cartas) de /cards/search.

        Retorna {"data": [...], "total_cards": N, "has_more": bool,
        "warnings": [...]}; busca sem resultado volta com data vazia.
        """

        params = {
            "q": query,
            "page": page,
            "unique": unique,
            "order": order,
            "dir": direction,
        }

        for attempt in range(4):
            _throttle_search()

            response = self.session.get(
                f"{settings.SCRYFALL_API}/cards/search", params=params, timeout=30
            )

            if response.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue

            time.sleep(self.delay)

            if response.status_code == 404:
                return {"data": [], "total_cards": 0, "has_more": False, "warnings": []}

            if response.status_code == 400:
                body = response.json()
                details = body.get("details") or "Busca inválida."
                warnings = body.get("warnings") or []
                raise ScryfallQueryError(" ".join([details, *warnings]))

            response.raise_for_status()

            body = response.json()
            body.setdefault("warnings", [])

            return body

        raise RuntimeError("Scryfall rate limit atingido.")

    def collection(self, identifiers: list[dict]) -> list[dict | None]:
        """
        /cards/collection em lotes de 75.

        Retorna uma lista do mesmo tamanho de identifiers, com o JSON da carta
        ou None quando não encontrada.
        """

        results = []

        for start in range(0, len(identifiers), COLLECTION_BATCH):
            batch = identifiers[start:start + COLLECTION_BATCH]

            for attempt in range(3):
                _throttle_search()
                response = self.session.post(
                    f"{settings.SCRYFALL_API}/cards/collection",
                    json={"identifiers": batch},
                    timeout=60,
                )
                if response.status_code != 429:
                    break
                time.sleep(2 * (attempt + 1))

            response.raise_for_status()
            time.sleep(self.delay)

            body = response.json()
            not_found = body.get("not_found") or []
            found = iter(body.get("data") or [])

            # O Scryfall devolve as encontradas na ordem pedida, sem as que faltaram
            for identifier in batch:
                if identifier in not_found:
                    results.append(None)
                else:
                    results.append(next(found, None))

        return results

    def named_fuzzy(self, name: str):
        """Nome aproximado; também reconhece nomes impressos em outros idiomas."""

        return self._get(f"{settings.SCRYFALL_API}/cards/named", params={"fuzzy": name})

    def get_by_id(self, scryfall_id: str):
        return self._get(f"{settings.SCRYFALL_API}/cards/{scryfall_id}")

    def get_card(
        self,
        name: str,
        set_code: str | None = None,
        collector_number: str | None = None,
    ):
        """
        Busca uma carta.

        Quando edição + collector number estão disponíveis,
        tenta encontrar exatamente aquela impressão.
        """

        if set_code and collector_number:
            data = self._get(
                f"{settings.SCRYFALL_API}/cards/"
                f"{set_code.lower()}/{collector_number}"
            )

            # A numeração da LigaMagic nem sempre bate com a do
            # Scryfall: só aceita se o nome conferir.
            if data and data.get("object") == "card" and _same_name(data, name):
                return data

        if set_code:
            data = self._get(
                f"{settings.SCRYFALL_API}/cards/named",
                params={"fuzzy": name, "set": set_code.lower()},
            )

            if data:
                return data

        # Fallback pelo nome
        return self._get(
            f"{settings.SCRYFALL_API}/cards/named",
            params={"fuzzy": name},
        )

    def creature_types(self) -> list[str]:
        data = self._get(f"{settings.SCRYFALL_API}/catalog/creature-types")

        return (data or {}).get("data", [])

    def autocomplete(self, query: str):
        data = self._get(
            f"{settings.SCRYFALL_API}/cards/autocomplete",
            params={"q": query},
        )

        return (data or {}).get("data", [])


def _same_name(data: dict, name: str) -> bool:
    wanted = name.strip().lower()

    names = [data.get("name", "")]
    names += [face.get("name", "") for face in data.get("card_faces") or []]

    return any(n.lower() == wanted for n in names if n)


def _image_uris(data: dict):
    if data.get("image_uris"):
        return data["image_uris"], None

    faces = data.get("card_faces") or []

    front = faces[0].get("image_uris") if faces else None
    back = faces[1].get("image_uris") if len(faces) > 1 else None

    return front or {}, back


def _face_value(data: dict, key: str, joiner: str = "\n\n"):
    """Valor do campo na carta, ou dos lados juntos (cartas de dois lados)."""

    if data.get(key):
        return data[key]

    faces = data.get("card_faces") or []

    values = [face.get(key) for face in faces if face.get(key)]

    return joiner.join(values) if values else ""


def card_fields(data: dict) -> dict:
    """Converte o JSON do Scryfall nos campos do modelo Card."""

    from cards.models import normalize_colors, parse_stat, pipe_join

    front, back = _image_uris(data)

    faces = data.get("card_faces") or []
    first_face = faces[0] if faces else {}

    colors = data.get("colors")
    if colors is None:
        colors = sorted({c for face in faces for c in face.get("colors") or []})

    prices = data.get("prices") or {}

    power = data.get("power") or first_face.get("power") or ""
    toughness = data.get("toughness") or first_face.get("toughness") or ""

    legal_formats = [
        fmt
        for fmt, status in (data.get("legalities") or {}).items()
        if status in ("legal", "restricted")
    ]

    return {
        "scryfall_id": data.get("id") or "",
        "oracle_id": data.get("oracle_id") or first_face.get("oracle_id") or "",
        "illustration_id": (
            data.get("illustration_id") or first_face.get("illustration_id") or ""
        ),
        "printed_name": data.get("printed_name") or "",
        "mana_cost": _face_value(data, "mana_cost", " // "),
        "cmc": data.get("cmc"),
        "type_line": data.get("type_line") or _face_value(data, "type_line", " // "),
        "oracle_text": _face_value(data, "oracle_text"),
        "flavor_text": _face_value(data, "flavor_text"),
        "power": power,
        "toughness": toughness,
        "loyalty": data.get("loyalty") or first_face.get("loyalty") or "",
        "power_num": parse_stat(power) if power else None,
        "toughness_num": parse_stat(toughness) if toughness else None,
        "colors": normalize_colors(colors),
        "color_identity": normalize_colors(data.get("color_identity")),
        "keywords": pipe_join(data.get("keywords")),
        "legal_formats": pipe_join(legal_formats),
        "rarity": data.get("rarity") or "",
        "set_code": data.get("set") or "",
        "set_name": data.get("set_name") or "",
        "collector_number": data.get("collector_number") or "",
        "released_at": data.get("released_at"),
        "artist": data.get("artist") or "",
        "image_url": front.get("normal") or "",
        "image_url_large": front.get("large") or front.get("png") or "",
        "image_url_back": (back or {}).get("normal") or "",
        "scryfall_url": data.get("scryfall_uri") or "",
        "price_usd": prices.get("usd") or prices.get("usd_etched"),
        "price_usd_foil": prices.get("usd_foil"),
        "raw_scryfall": data,
    }
