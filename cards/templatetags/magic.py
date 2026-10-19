import re
from decimal import Decimal

from django import template
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe

from cards.models import COLOR_NAMES


register = template.Library()

SYMBOL_RE = re.compile(r"\{([^}]+)\}")

SYMBOL_URL = "https://svgs.scryfall.io/card-symbols/{}.svg"


def _symbol_html(symbol: str) -> str:
    code = symbol.replace("/", "").upper()

    return (
        f'<img class="mana" src="{SYMBOL_URL.format(escape(code))}" '
        f'alt="{{{escape(symbol)}}}" title="{{{escape(symbol)}}}" loading="lazy">'
    )


@register.filter
def mana(value):
    """{2}{U}{U} -> imagens dos símbolos."""

    if not value:
        return ""

    html = SYMBOL_RE.sub(lambda m: _symbol_html(m.group(1)), escape(value))

    return mark_safe(html)


@register.filter
def oracle(value):
    """Texto da carta com símbolos e quebras de linha."""

    if not value:
        return ""

    paragraphs = escape(value).split("\n")

    html = "".join(
        f"<p>{SYMBOL_RE.sub(lambda m: _symbol_html(m.group(1)), p)}</p>"
        for p in paragraphs
        if p.strip()
    )

    # Texto de lembrete em itálico
    html = re.sub(r"\(([^)]*)\)", r'<span class="reminder">(\1)</span>', html)

    return mark_safe(html)


@register.filter
def color_pips(value):
    """"UR" -> símbolos de cor; "" -> incolor."""

    letters = value or "C"

    return mark_safe("".join(_symbol_html(c) for c in letters))


@register.filter
def color_name(code):
    return COLOR_NAMES.get(code, "Incolor")


def _format_money(value, prefix):
    if value is None or value == "":
        return "—"

    value = Decimal(value).quantize(Decimal("0.01"))

    # 1234.5 -> 1.234,50
    text = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    return f"{prefix} {text}"


@register.filter
def brl(value):
    return _format_money(value, "R$")


@register.filter
def brl_abs(value):
    if value is None or value == "":
        return "—"
    return _format_money(abs(Decimal(value)), "R$")


@register.filter
def usd(value):
    return _format_money(value, "US$")


@register.filter
def to_brl(usd_value, rate):
    """Converte US$ para R$ pela cotação."""

    if usd_value is None or not rate:
        return None

    return Decimal(usd_value) * Decimal(rate)


@register.filter
def price_slider_max(reference_price):
    """Fim da barra de preço: 3× o preço da referência, no mínimo R$ 20, arredondado."""

    value = max(Decimal(reference_price or 0) * 3, Decimal(20))

    step = 10 if value <= 100 else 50

    return int((value / step).to_integral_value(rounding="ROUND_CEILING") * step)


@register.filter
def legality_label(status):
    return {
        "legal": "Legal",
        "not_legal": "Não",
        "banned": "Banida",
        "restricted": "Restrita",
    }.get(status, status)


@register.filter
def rarity_label(rarity):
    return {
        "common": "Comum",
        "uncommon": "Incomum",
        "rare": "Rara",
        "mythic": "Mítica",
        "special": "Especial",
        "bonus": "Bônus",
    }.get(rarity, rarity or "—")


@register.simple_tag
def page_url(querystring, number):
    if querystring:
        return format_html("?{}&amp;page={}", querystring, number)

    return format_html("?page={}", number)


@register.simple_tag
def view_url(querystring, mode):
    from django.http import QueryDict

    params = QueryDict(querystring, mutable=True)
    params["view"] = mode

    return "?" + params.urlencode()
