"""
Leitura de listas de deck coladas como texto.

Formatos aceitos (um por linha):

    LigaMagic:  1 Fuga de Mana
                1x Sol Ring
    Archidekt:  1x Vren, the Relentless (blb) [Commander{top}]
                1x Sol Ring (cmm) 410 [Ramp] ^Have,#37d67a^
    Moxfield:   1 Arcane Signet (CMM) 371 *F*

Cabeçalhos de seção ("Commander", "Comandante", "Deck", "Sideboard"...)
mudam a seção das linhas seguintes. Cartas na seção de comandante ou com
a categoria "Commander" (Archidekt) são marcadas como comandante.
"""

import re
from dataclasses import dataclass


@dataclass
class Entry:
    quantity: int
    name: str
    line: str
    set_code: str = ""
    collector_number: str = ""
    category: str = ""
    is_commander: bool = False
    section: str = ""


SECTION_RE = re.compile(
    r"^(?://\s*)?(commanders?|comandantes?|commander zone|deck|main\s*deck|mainboard|main|"
    r"maybeboard|maybe|considering|sideboard|side|tokens?|companions?)\s*:?\s*(?:\(\d+\))?$",
    re.IGNORECASE,
)

COMMANDER_SECTIONS = {"commander", "commanders", "comandante", "comandantes", "commander zone"}
IGNORED_SECTIONS = {"maybeboard", "maybe", "considering", "sideboard", "side", "token", "tokens"}

QUANTITY_RE = re.compile(r"^(\d+)\s*[xX]?\s+(.+)$")
CATEGORY_RE = re.compile(r"\s*\[([^\]]*)\]\s*$")
COLOR_TAG_RE = re.compile(r"\s*\^[^^]*\^\s*")
FOIL_RE = re.compile(r"\s*\*[A-Z]\*\s*")
SET_RE = re.compile(r"\s*\(([A-Za-z0-9]{2,6})\)\s*([A-Za-z0-9★\-]+)?\s*$")


def _clean_section(raw: str) -> str:
    return re.sub(r"\s+", " ", raw.strip().lower())


def parse_line(line: str, section: str = "") -> Entry | None:
    text = line.strip()

    if not text or text.startswith("#") or text.startswith("//"):
        return None

    match = QUANTITY_RE.match(text)
    quantity, rest = (int(match.group(1)), match.group(2)) if match else (1, text)

    rest = COLOR_TAG_RE.sub(" ", rest).strip()

    category = ""
    category_match = CATEGORY_RE.search(rest)
    if category_match:
        category = category_match.group(1).strip()
        rest = rest[: category_match.start()].strip()

    rest = FOIL_RE.sub(" ", rest).strip()

    set_code = collector_number = ""
    set_match = SET_RE.search(rest)
    if set_match:
        set_code = set_match.group(1).lower()
        collector_number = set_match.group(2) or ""
        rest = rest[: set_match.start()].strip()

    if not rest:
        return None

    # Archidekt: "Commander{top}", "Ramp,Commander{noDeck}"
    categories = [re.sub(r"\{.*?\}", "", c).strip().lower() for c in category.split(",")]

    return Entry(
        quantity=quantity,
        name=rest,
        line=text,
        set_code=set_code,
        collector_number=collector_number,
        category=category,
        is_commander=("commander" in categories) or section in COMMANDER_SECTIONS,
        section=section,
    )


def parse_decklist(text: str) -> list[Entry]:
    entries = []
    section = ""

    for line in (text or "").splitlines():
        stripped = line.strip()
        header = SECTION_RE.match(stripped)

        if header:
            section = _clean_section(header.group(1))
            continue

        entry = parse_line(stripped, section)

        if entry is None or section in IGNORED_SECTIONS:
            continue

        # Archidekt: cartas fora do deck ficam na categoria "Maybeboard"/"Sideboard"
        if entry.category and entry.category.split("{")[0].strip().lower() in IGNORED_SECTIONS:
            continue

        entries.append(entry)

    return entries
