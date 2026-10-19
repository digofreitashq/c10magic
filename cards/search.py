"""
Busca de cartas.

Duas entradas que se somam:

1. Sintaxe de busca no estilo Scryfall (campo "q"):

       t:creature c:ug mv<=3 brl<5 -o:"draw a card" f:commander

2. Filtros do formulário (cores, tipo, raridade, preço...).
"""

import re
from decimal import Decimal, InvalidOperation

from django.db.models import Case, F, IntegerField, Q, Value, When

from cards.models import COLOR_ORDER, TagIndex


# ============================================================
# CORES
# ============================================================

COLOR_WORDS = {
    "white": "W", "branco": "W", "branca": "W",
    "blue": "U", "azul": "U",
    "black": "B", "preto": "B", "preta": "B",
    "red": "R", "vermelho": "R", "vermelha": "R",
    "green": "G", "verde": "G",
    "colorless": "C", "incolor": "C",
    # Guildas / clãs / fragmentos mais usados
    "azorius": "WU", "dimir": "UB", "rakdos": "BR", "gruul": "RG",
    "selesnya": "GW", "orzhov": "WB", "izzet": "UR", "golgari": "BG",
    "boros": "RW", "simic": "GU",
    "bant": "GWU", "esper": "WUB", "grixis": "UBR", "jund": "BRG",
    "naya": "RGW", "abzan": "WBG", "jeskai": "URW", "sultai": "BGU",
    "mardu": "RWB", "temur": "GUR",
    "multicolor": "M", "multi": "M", "m": "M",
}


def parse_colors(value: str) -> str:
    value = value.strip().lower()

    if value in COLOR_WORDS:
        return COLOR_WORDS[value]

    letters = value.upper()

    if all(c in COLOR_ORDER + "C" for c in letters):
        return letters

    raise ValueError(f"cor desconhecida: {value}")


def color_q(field: str, colors: str, mode: str) -> Q:
    """
    mode:
        any    - tem pelo menos uma das cores
        all    - tem todas as cores (pode ter outras)   [c:  c>=]
        exact  - exatamente essas cores                 [c=]
        atmost - nenhuma cor fora dessas (commander)    [c<= id:]
        more   - todas e mais alguma                    [c>]
        less   - subconjunto estrito                    [c<]
    """

    colors = colors.upper()

    if colors == "M":
        # Multicolorida: duas ou mais cores
        return Q(**{f"{field}__regex": r"^.{2,}$"})

    colorless = "C" in colors
    letters = "".join(c for c in COLOR_ORDER if c in colors)
    others = [c for c in COLOR_ORDER if c not in letters]

    if not letters:
        # Só incolor
        if mode in ("any", "all", "exact", "atmost"):
            return Q(**{field: ""})
        return Q(pk__in=[])

    has = [Q(**{f"{field}__contains": c}) for c in letters]
    lacks = [~Q(**{f"{field}__contains": c}) for c in others]

    if mode == "any":
        q = Q()
        for item in has:
            q |= item
        if colorless:
            q |= Q(**{field: ""})
        return q

    if mode == "all":
        return _and(has)

    if mode == "exact":
        return Q(**{field: letters})

    if mode == "atmost":
        # Inclui incolores, como no Scryfall (id<=UR)
        return _and(lacks)

    if mode == "more":
        return _and(has) & ~Q(**{field: letters})

    if mode == "less":
        return _and(lacks) & ~Q(**{field: letters})

    raise ValueError(f"modo de cor desconhecido: {mode}")


def _and(items) -> Q:
    q = Q()
    for item in items:
        q &= item
    return q


# ============================================================
# SINTAXE DE BUSCA
# ============================================================

TOKEN_RE = re.compile(
    r"""
    (?P<neg>-)?
    (?:
        (?P<key>[a-z$]+)
        (?P<op><=|>=|!=|:|=|<|>)
        (?P<value>"[^"]*"|\S+)
      |
        (?P<exact>!)?(?P<word>"[^"]*"|\S+)
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

KEY_ALIASES = {
    "t": "type", "type": "type", "tipo": "type",
    "o": "oracle", "oracle": "oracle", "texto": "oracle", "text": "oracle",
    "c": "color", "color": "color", "cor": "color", "colors": "color",
    "id": "identity", "ci": "identity", "identity": "identity",
    "identidade": "identity", "commander": "identity",
    "mv": "cmc", "cmc": "cmc", "manavalue": "cmc", "cmm": "cmc",
    "pow": "power", "power": "power", "poder": "power",
    "tou": "toughness", "toughness": "toughness", "res": "toughness",
    "resistencia": "toughness",
    "loy": "loyalty", "loyalty": "loyalty", "lealdade": "loyalty",
    "r": "rarity", "rarity": "rarity", "raridade": "rarity",
    "s": "set", "e": "set", "set": "set", "edition": "set", "edicao": "set",
    "kw": "keyword", "keyword": "keyword", "palavra": "keyword",
    "f": "format", "format": "format", "legal": "format", "formato": "format",
    "banned": "banned",
    "brl": "brl", "r$": "brl", "real": "brl", "reais": "brl", "preco": "brl",
    "usd": "usd", "$": "usd", "dolar": "usd",
    "qtd": "quantity", "qty": "quantity", "quantidade": "quantity",
    "a": "artist", "artist": "artist", "artista": "artist",
    "m": "mana", "mana": "mana",
    "n": "name", "name": "name", "nome": "name",
    "ft": "flavor", "flavor": "flavor",
    "is": "is", "not": "not", "year": "year", "ano": "year",
    "otag": "otag", "oracletag": "otag", "function": "otag", "funcao": "otag",
    "atag": "atag", "art": "atag", "arttag": "atag", "arte": "atag",
}

RARITY_ORDER = ["common", "uncommon", "rare", "mythic", "special", "bonus"]

RARITY_WORDS = {
    "c": "common", "comum": "common", "common": "common",
    "u": "uncommon", "incomum": "uncommon", "uncommon": "uncommon",
    "r": "rare", "rara": "rare", "rare": "rare",
    "m": "mythic", "mitica": "mythic", "mítica": "mythic", "mythic": "mythic",
    "s": "special", "special": "special", "especial": "special",
    "b": "bonus", "bonus": "bonus",
}

NUMERIC_OPS = {
    ":": "exact", "=": "exact",
    "<": "lt", "<=": "lte", ">": "gt", ">=": "gte",
}

COLOR_OPS = {
    ":": "all", ">=": "all", "=": "exact",
    "<=": "atmost", ">": "more", "<": "less",
}

IDENTITY_OPS = {
    ":": "atmost", "<=": "atmost", "=": "exact",
    ">=": "all", ">": "more", "<": "less",
}

IS_FLAGS = {
    "commander": lambda: (
        Q(type_line__icontains="legendary") & Q(type_line__icontains="creature")
    ) | Q(oracle_text__icontains="can be your commander"),
    "legendary": lambda: Q(type_line__icontains="legendary"),
    "permanent": lambda: ~(
        Q(type_line__icontains="instant") | Q(type_line__icontains="sorcery")
    ),
    "spell": lambda: ~Q(type_line__icontains="land"),
    "multicolor": lambda: Q(colors__regex=r"^.{2,}$"),
    "colorless": lambda: Q(colors=""),
    "mono": lambda: Q(colors__regex=r"^.$"),
    "token": lambda: Q(type_line__icontains="token"),
    "dfc": lambda: Q(name__contains=" // "),
    "split": lambda: Q(name__contains=" // "),
    "vanilla": lambda: Q(type_line__icontains="creature") & Q(oracle_text=""),
    "priced": lambda: Q(price_brl__isnull=False),
    "unpriced": lambda: Q(price_brl__isnull=True),
    "stale": lambda: _stale_q(),
}


def _stale_q():
    from cards.models import Card

    return Card.stale_q()


class QuerySyntaxError(ValueError):
    pass


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def _number(value: str):
    try:
        return Decimal(value.replace(",", "."))
    except InvalidOperation as exc:
        raise QuerySyntaxError(f"número inválido: {value}") from exc


def _numeric_q(field: str, op: str, value: str) -> Q:
    if op == "!=":
        return ~Q(**{field: _number(value)})

    lookup = NUMERIC_OPS[op]
    suffix = "" if lookup == "exact" else f"__{lookup}"

    return Q(**{f"{field}{suffix}": _number(value)})


def _term_q(key: str, op: str, value: str) -> Q:
    field = KEY_ALIASES.get(key.lower())

    if field is None:
        raise QuerySyntaxError(f"chave desconhecida: {key}")

    value = _unquote(value)

    if field in ("type", "oracle", "artist", "name", "flavor"):
        column = {
            "type": "type_line",
            "oracle": "oracle_text",
            "artist": "artist",
            "name": "name",
            "flavor": "flavor_text",
        }[field]

        if field == "oracle":
            # ~ representa o nome da carta no Scryfall: aqui só ignoramos
            value = value.replace("~", "").strip()

        q = Q(**{f"{column}__icontains": value})

        return ~q if op == "!=" else q

    if field in ("color", "identity"):
        column = "colors" if field == "color" else "color_identity"
        ops = COLOR_OPS if field == "color" else IDENTITY_OPS

        try:
            colors = parse_colors(value)
        except ValueError as exc:
            raise QuerySyntaxError(str(exc)) from exc

        if op == "!=":
            return ~color_q(column, colors, "exact")

        return color_q(column, colors, ops[op])

    if field in ("cmc", "brl", "usd", "quantity", "power", "toughness"):
        column = {
            "cmc": "cmc",
            "brl": "price_brl",
            "usd": "price_usd",
            "quantity": "quantity",
            "power": "power_num",
            "toughness": "toughness_num",
        }[field]

        if field in ("power", "toughness") and value.lower() in ("tou", "pow"):
            other = "toughness_num" if value.lower() == "tou" else "power_num"
            lookup = NUMERIC_OPS.get(op, "exact")
            suffix = "" if lookup == "exact" else f"__{lookup}"
            return Q(**{f"{column}{suffix}": F(other)})

        return _numeric_q(column, op, value)

    if field == "loyalty":
        # Lealdade é texto ("3", "X"): só igualdade
        if op not in (":", "="):
            raise QuerySyntaxError("lealdade só aceita igualdade (loy:3)")
        return Q(loyalty=value)

    if field == "rarity":
        rarity = RARITY_WORDS.get(value.lower())

        if rarity is None:
            raise QuerySyntaxError(f"raridade desconhecida: {value}")

        if op in (":", "="):
            return Q(rarity=rarity)
        if op == "!=":
            return ~Q(rarity=rarity)

        index = RARITY_ORDER.index(rarity)
        allowed = {
            "<": RARITY_ORDER[:index],
            "<=": RARITY_ORDER[: index + 1],
            ">": RARITY_ORDER[index + 1:],
            ">=": RARITY_ORDER[index:],
        }[op]

        return Q(rarity__in=allowed)

    if field == "set":
        q = Q(set_code__iexact=value) | Q(hint_set_code__iexact=value)
        return ~q if op == "!=" else q

    if field == "keyword":
        q = Q(keywords__icontains=value.lower())
        return ~q if op == "!=" else q

    if field == "format":
        q = Q(legal_formats__contains=f"|{value.lower()}|")
        return ~q if op == "!=" else q

    if field == "banned":
        if not value.isalpha():
            raise QuerySyntaxError(f"formato inválido: {value}")
        return Q(**{f"raw_scryfall__legalities__{value.lower()}": "banned"})

    if field == "mana":
        symbols = value.upper()
        if not symbols.startswith("{"):
            symbols = "".join(f"{{{s}}}" for s in symbols)
        return Q(mana_cost__contains=symbols)

    if field in ("otag", "atag"):
        kind, column = (
            (TagIndex.ORACLE, "oracle_id") if field == "otag"
            else (TagIndex.ART, "illustration_id")
        )
        ids = TagIndex.objects.filter(
            kind=kind, labels__contains=f"|{value.lower()}|"
        ).values("target_id")
        q = Q(**{f"{column}__in": ids})
        return ~q if op == "!=" else q

    if field == "year":
        return _numeric_q("released_at__year", op, value)

    if field in ("is", "not"):
        flag = IS_FLAGS.get(value.lower())

        if flag is None:
            raise QuerySyntaxError(f"is:{value} não suportado")

        q = flag()

        return ~q if field == "not" else q

    raise QuerySyntaxError(f"chave desconhecida: {key}")


def parse_query(text: str) -> Q:
    """
    Converte a busca em Q.

    Termos separados por espaço são combinados com E;
    "or" / "ou" separa grupos combinados com OU.
    """

    text = (text or "").strip()

    if not text:
        return Q()

    groups = [Q()]

    for match in TOKEN_RE.finditer(text):
        word = match.group("word")

        if word and word.lower() in ("or", "ou") and not match.group("neg"):
            groups.append(Q())
            continue

        if match.group("key"):
            q = _term_q(match.group("key"), match.group("op"), match.group("value"))
        else:
            name = _unquote(word)

            if match.group("exact"):
                q = Q(name__iexact=name) | Q(name__istartswith=f"{name} // ")
            else:
                q = Q(name__icontains=name) | Q(printed_name__icontains=name)

        if match.group("neg"):
            q = ~q

        groups[-1] &= q

    result = Q()
    for group in groups:
        result |= group

    return result


# ============================================================
# FILTROS DO FORMULÁRIO
# ============================================================

SORT_FIELDS = {
    "name": "name_normalized",
    "price_brl": "price_brl",
    "price_usd": "price_usd",
    "cmc": "cmc",
    "rarity": "rarity_rank",
    "set": "set_code",
    "released": "released_at",
    "quantity": "quantity",
    "power": "power_num",
    "toughness": "toughness_num",
    "updated": "ligamagic_updated_at",
}


def apply_filters(queryset, data: dict):
    """data = form.cleaned_data"""

    q = Q()

    if data.get("q"):
        q &= parse_query(data["q"])

    if data.get("name"):
        q &= Q(name__icontains=data["name"]) | Q(printed_name__icontains=data["name"])

    if data.get("text"):
        for part in data["text"].split(";"):
            if part.strip():
                q &= Q(oracle_text__icontains=part.strip())

    for card_type in data.get("types") or []:
        q &= Q(type_line__icontains=card_type)

    if data.get("subtype"):
        q &= Q(type_line__icontains=data["subtype"])

    if data.get("colors"):
        q &= color_q("colors", "".join(data["colors"]), data.get("color_mode") or "any")

    if data.get("identity"):
        q &= color_q("color_identity", "".join(data["identity"]), "atmost")

    if data.get("rarities"):
        q &= Q(rarity__in=data["rarities"])

    if data.get("set_code"):
        codes = [c.strip() for c in re.split(r"[,\s]+", data["set_code"]) if c.strip()]
        set_q = Q()
        for code in codes:
            set_q |= Q(set_code__iexact=code) | Q(hint_set_code__iexact=code)
        set_q |= Q(set_name__icontains=data["set_code"].strip())
        q &= set_q

    if data.get("keyword"):
        q &= Q(keywords__icontains=data["keyword"].lower())

    if data.get("format"):
        q &= Q(legal_formats__contains=f"|{data['format']}|")

    ranges = [
        ("cmc", "cmc_min", "cmc_max"),
        ("power_num", "power_min", "power_max"),
        ("toughness_num", "toughness_min", "toughness_max"),
        ("price_brl", "price_min", "price_max"),
        ("price_usd", "usd_min", "usd_max"),
    ]

    for field, low, high in ranges:
        if data.get(low) is not None:
            q &= Q(**{f"{field}__gte": data[low]})
        if data.get(high) is not None:
            q &= Q(**{f"{field}__lte": data[high]})

    if data.get("quantity_min"):
        q &= Q(quantity__gte=data["quantity_min"])

    if data.get("only_priced"):
        q &= Q(price_brl__isnull=False)

    queryset = queryset.filter(q)

    # Ordenação
    queryset = queryset.annotate(
        rarity_rank=Case(
            *[When(rarity=r, then=Value(i)) for i, r in enumerate(RARITY_ORDER)],
            default=Value(len(RARITY_ORDER)),
            output_field=IntegerField(),
        )
    )

    column = SORT_FIELDS.get(data.get("sort") or "name", "name_normalized")
    descending = data.get("order") == "desc"

    expression = F(column).desc(nulls_last=True) if descending else F(column).asc(nulls_last=True)

    return queryset.order_by(expression, "name_normalized")


# ============================================================
# BUSCA NO SCRYFALL
# ============================================================

SCRYFALL_ORDER = {
    "name": "name",
    "price_brl": "usd",
    "price_usd": "usd",
    "cmc": "cmc",
    "rarity": "rarity",
    "set": "set",
    "released": "released",
    "power": "power",
    "toughness": "toughness",
    "edhrec": "edhrec",
    "quantity": "name",
}

BRL_TERM_RE = re.compile(
    r"(?<!\S)(-?)(?:brl|r\$|real|reais|preco)(<=|>=|!=|:|=|<|>)([\d.,]+)(?!\S)",
    re.IGNORECASE,
)


def _quote(value: str) -> str:
    value = value.strip().replace('"', "")
    return f'"{value}"' if re.search(r"\s", value) else value


def _any(terms) -> str:
    terms = list(terms)
    return terms[0] if len(terms) == 1 else "(" + " or ".join(terms) + ")"


def convert_brl_terms(query: str, rate: Decimal) -> str:
    """brl<10 -> usd<1.85 (o Scryfall só conhece preços em dólar)."""

    def replace(match):
        neg, op, value = match.groups()
        usd = (Decimal(value.replace(",", ".")) / rate).quantize(Decimal("0.01"))
        return f"{neg}usd{op}{usd}"

    return BRL_TERM_RE.sub(replace, query)


def scryfall_colors(colors: list[str], mode: str) -> str:
    letters = "".join(c for c in COLOR_ORDER if c in colors).lower()
    colorless = "C" in colors

    if not letters:
        return "c:c" if colorless else ""

    if mode == "any":
        terms = [f"c:{c}" for c in letters]
        if colorless:
            terms.append("c:c")
        return _any(terms)

    operator = {"all": ">=", "exact": "=", "atmost": "<="}.get(mode, ">=")

    return f"c{operator}{letters}"


def to_scryfall_query(data: dict, rate: Decimal) -> str:
    """Monta a busca do Scryfall a partir da busca em texto + filtros."""

    terms = []

    if data.get("q"):
        terms.append(convert_brl_terms(data["q"].strip(), rate))

    if data.get("name"):
        terms.append(_quote(data["name"]))

    if data.get("text"):
        terms += [f"o:{_quote(p)}" for p in data["text"].split(";") if p.strip()]

    terms += [f"t:{_quote(t.lower())}" for t in data.get("types") or []]

    if data.get("subtype"):
        terms.append(f"t:{_quote(data['subtype'])}")

    if data.get("colors"):
        terms.append(scryfall_colors(data["colors"], data.get("color_mode") or "any"))

    if data.get("identity"):
        letters = "".join(c for c in COLOR_ORDER if c in data["identity"]).lower()
        terms.append(f"id<={letters}")

    if data.get("rarities"):
        terms.append(_any(f"r:{r}" for r in data["rarities"]))

    if data.get("set_code"):
        codes = [c for c in re.split(r"[,\s]+", data["set_code"]) if c]
        terms.append(_any(f"s:{c.lower()}" for c in codes))

    if data.get("keyword"):
        terms.append(f"kw:{_quote(data['keyword'])}")

    if data.get("format"):
        terms.append(f"f:{data['format']}")

    ranges = [
        ("mv", "cmc_min", "cmc_max"),
        ("pow", "power_min", "power_max"),
        ("tou", "toughness_min", "toughness_max"),
        ("usd", "usd_min", "usd_max"),
    ]

    for key, low, high in ranges:
        if data.get(low) is not None:
            terms.append(f"{key}>={data[low]}")
        if data.get(high) is not None:
            terms.append(f"{key}<={data[high]}")

    if data.get("price_min") is not None:
        terms.append(f"usd>={(data['price_min'] / rate).quantize(Decimal('0.01'))}")
    if data.get("price_max") is not None:
        terms.append(f"usd<={(data['price_max'] / rate).quantize(Decimal('0.01'))}")

    if data.get("only_priced"):
        terms.append("usd>0")

    return " ".join(t for t in terms if t)
