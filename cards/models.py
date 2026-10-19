from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone


COLOR_ORDER = "WUBRG"

COLOR_NAMES = {
    "W": "Branco",
    "U": "Azul",
    "B": "Preto",
    "R": "Vermelho",
    "G": "Verde",
}

RARITY_CHOICES = [
    ("common", "Comum"),
    ("uncommon", "Incomum"),
    ("rare", "Rara"),
    ("mythic", "Mítica"),
    ("special", "Especial"),
    ("bonus", "Bônus"),
]

FORMAT_CHOICES = [
    ("standard", "Standard"),
    ("pioneer", "Pioneer"),
    ("modern", "Modern"),
    ("legacy", "Legacy"),
    ("vintage", "Vintage"),
    ("commander", "Commander"),
    ("pauper", "Pauper"),
    ("paupercommander", "Pauper Commander"),
    ("brawl", "Brawl"),
    ("historic", "Historic"),
    ("timeless", "Timeless"),
    ("alchemy", "Alchemy"),
    ("explorer", "Explorer"),
    ("oathbreaker", "Oathbreaker"),
    ("premodern", "Premodern"),
    ("oldschool", "Old School"),
]


def normalize_colors(colors) -> str:
    """["R", "U"] -> "UR" (ordem WUBRG)."""

    letters = {c.upper() for c in colors or []}

    return "".join(c for c in COLOR_ORDER if c in letters)


def pipe_join(values) -> str:
    """["Flying", "Haste"] -> "|flying|haste|" (para buscas com LIKE)."""

    values = [v.strip().lower() for v in values or [] if v and v.strip()]

    return "|" + "|".join(values) + "|" if values else ""


def parse_stat(value):
    """Converte power/toughness ("3", "1+*", "*") para número, quando der."""

    if value is None:
        return None

    try:
        return float(value)
    except ValueError:
        pass

    digits = "".join(c for c in value if c.isdigit() or c == ".")

    try:
        return float(digits) if digits else 0.0
    except ValueError:
        return None


class Card(models.Model):

    # --------------------------------------------------------
    # Coleção
    # --------------------------------------------------------

    name = models.CharField("nome", max_length=255)
    name_normalized = models.CharField(max_length=255, unique=True)

    quantity = models.PositiveIntegerField("quantidade", default=1)

    # Impressão preferida (vinda do CSV), usada para achar a carta
    # exata no Scryfall.
    hint_set_code = models.CharField(max_length=16, blank=True)
    hint_collector_number = models.CharField(max_length=16, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    # --------------------------------------------------------
    # Scryfall
    # --------------------------------------------------------

    scryfall_id = models.CharField(max_length=64, blank=True, db_index=True)
    oracle_id = models.CharField(max_length=64, blank=True)
    illustration_id = models.CharField(max_length=64, blank=True)

    printed_name = models.CharField(max_length=255, blank=True)

    mana_cost = models.CharField(max_length=128, blank=True)
    cmc = models.FloatField("valor de mana", null=True, blank=True, db_index=True)

    type_line = models.CharField("tipo", max_length=255, blank=True)
    oracle_text = models.TextField("texto", blank=True)
    flavor_text = models.TextField(blank=True)

    power = models.CharField(max_length=16, blank=True)
    toughness = models.CharField(max_length=16, blank=True)
    loyalty = models.CharField(max_length=16, blank=True)

    power_num = models.FloatField(null=True, blank=True)
    toughness_num = models.FloatField(null=True, blank=True)

    colors = models.CharField(max_length=5, blank=True, db_index=True)
    color_identity = models.CharField(max_length=5, blank=True, db_index=True)

    keywords = models.TextField(blank=True)
    legal_formats = models.TextField(blank=True)

    rarity = models.CharField(
        "raridade",
        max_length=16,
        blank=True,
        choices=RARITY_CHOICES,
        db_index=True,
    )

    set_code = models.CharField(max_length=16, blank=True, db_index=True)
    set_name = models.CharField(max_length=255, blank=True)
    collector_number = models.CharField(max_length=16, blank=True)
    released_at = models.DateField(null=True, blank=True)

    artist = models.CharField(max_length=255, blank=True)

    image_url = models.URLField(max_length=500, blank=True)
    image_url_large = models.URLField(max_length=500, blank=True)
    image_url_back = models.URLField(max_length=500, blank=True)

    scryfall_url = models.URLField(max_length=500, blank=True)

    price_usd = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True, db_index=True
    )
    price_usd_foil = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )

    raw_scryfall = models.JSONField(null=True, blank=True)

    scryfall_updated_at = models.DateTimeField(null=True, blank=True)
    scryfall_error = models.CharField(max_length=255, blank=True)

    # --------------------------------------------------------
    # LigaMagic
    # --------------------------------------------------------

    price_brl = models.DecimalField(
        "preço (R$)",
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        db_index=True,
    )
    price_brl_foil = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )

    ligamagic_url = models.URLField(max_length=500, blank=True)

    ligamagic_updated_at = models.DateTimeField(null=True, blank=True)
    ligamagic_error = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["name_normalized"]
        verbose_name = "carta"
        verbose_name_plural = "cartas"

    def __str__(self):
        return self.name

    # --------------------------------------------------------
    # Atualização semanal
    # --------------------------------------------------------

    @staticmethod
    def refresh_interval():
        return timedelta(days=settings.CARD_REFRESH_DAYS)

    @staticmethod
    def stale_before():
        return timezone.now() - Card.refresh_interval()

    @classmethod
    def stale_q(cls):
        """Q das cartas que nunca foram carregadas ou estão vencidas."""

        limit = cls.stale_before()

        return (
            models.Q(scryfall_updated_at__isnull=True)
            | models.Q(scryfall_updated_at__lt=limit)
            | models.Q(ligamagic_updated_at__isnull=True)
            | models.Q(ligamagic_updated_at__lt=limit)
        )

    def scryfall_is_stale(self):
        return (
            self.scryfall_updated_at is None
            or self.scryfall_updated_at < self.stale_before()
        )

    def ligamagic_is_stale(self):
        return (
            self.ligamagic_updated_at is None
            or self.ligamagic_updated_at < self.stale_before()
        )

    def is_stale(self):
        return self.scryfall_is_stale() or self.ligamagic_is_stale()

    def next_refresh_at(self):
        dates = [
            d for d in (self.scryfall_updated_at, self.ligamagic_updated_at) if d
        ]

        if len(dates) < 2:
            return None

        return min(dates) + self.refresh_interval()

    # --------------------------------------------------------
    # Apresentação
    # --------------------------------------------------------

    @property
    def keyword_list(self):
        return [k for k in self.keywords.split("|") if k]

    @property
    def legalities(self):
        """[(nome do formato, status)] para os formatos conhecidos."""

        raw = (self.raw_scryfall or {}).get("legalities") or {}

        return [
            (label, raw.get(code, "not_legal"))
            for code, label in FORMAT_CHOICES
            if code in raw
        ]

    @property
    def total_brl(self):
        if self.price_brl is None:
            return None

        return self.price_brl * self.quantity


class CachedCard(models.Model):
    """
    JSON de uma carta do Scryfall já vista numa busca ou aberta no
    detalhe. Evita consultar o Scryfall de novo por uma semana.
    """

    scryfall_id = models.CharField(max_length=64, unique=True)
    oracle_id = models.CharField(max_length=64, blank=True, db_index=True)
    name = models.CharField(max_length=255)
    name_normalized = models.CharField(max_length=255, db_index=True)

    data = models.JSONField()

    updated_at = models.DateTimeField(db_index=True)

    class Meta:
        verbose_name = "carta do Scryfall (cache)"
        verbose_name_plural = "cartas do Scryfall (cache)"

    def __str__(self):
        return self.name

    def is_stale(self):
        return self.updated_at < Card.stale_before()


class TagIndex(models.Model):
    """
    Tags do Scryfall Tagger de uma carta (oracle) ou de uma ilustração (art).

    Uma linha por oracle_id / illustration_id, com as tags no formato
    "|removal|ramp|" para buscas com LIKE.
    """

    ORACLE = "oracle"
    ART = "illustration"

    KIND_CHOICES = [(ORACLE, "Função (oracle)"), (ART, "Arte (illustration)")]

    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    target_id = models.CharField(max_length=64)
    labels = models.TextField()

    class Meta:
        unique_together = [("kind", "target_id")]
        verbose_name = "tags do Scryfall"
        verbose_name_plural = "tags do Scryfall"

    def __str__(self):
        return f"{self.kind} {self.target_id}"

    @property
    def label_list(self):
        return [label for label in self.labels.split("|") if label]


class TagStat(models.Model):
    """Em quantas cartas (ou ilustrações) cada tag aparece."""

    kind = models.CharField(max_length=16, choices=TagIndex.KIND_CHOICES)
    label = models.CharField(max_length=255)
    count = models.PositiveIntegerField()

    class Meta:
        unique_together = [("kind", "label")]
        verbose_name = "contagem de tag"
        verbose_name_plural = "contagens de tags"

    def __str__(self):
        return f"{self.label} ({self.count})"


class SyncState(models.Model):
    """Quando cada sincronização em lote rodou pela última vez."""

    key = models.CharField(max_length=64, primary_key=True)
    updated_at = models.DateTimeField()
    detail = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f"{self.key}: {self.updated_at:%d/%m/%Y %H:%M}"


class EdhrecPage(models.Model):
    """JSON de uma página do EDHREC (vale por uma semana)."""

    path = models.CharField(max_length=255, unique=True)
    data = models.JSONField(null=True, blank=True)
    error = models.CharField(max_length=255, blank=True)
    updated_at = models.DateTimeField()

    class Meta:
        verbose_name = "página do EDHREC (cache)"
        verbose_name_plural = "páginas do EDHREC (cache)"

    def __str__(self):
        return self.path

    def is_stale(self):
        return self.updated_at < Card.stale_before()


class Deck(models.Model):
    """Deck de Commander importado na página Especialista."""

    name = models.CharField(max_length=255, blank=True)
    raw_text = models.TextField()
    budget = models.DecimalField(
        "orçamento (R$)", max_digits=10, decimal_places=2, null=True, blank=True
    )
    # Preenchido quando o deck foi montado pelo Construtor
    build_report = models.JSONField(null=True, blank=True)

    # Nome dado pelo usuário: não é trocado pelo nome do comandante
    name_locked = models.BooleanField(default=False)

    # Carta cuja arte é a capa do deck (vazio = o comandante)
    cover_scryfall_id = models.CharField(max_length=64, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.name or f"Deck {self.pk}"

    @property
    def commanders(self):
        return [card for card in self.cards.all() if card.is_commander]


class DeckCard(models.Model):

    deck = models.ForeignKey(Deck, on_delete=models.CASCADE, related_name="cards")
    position = models.PositiveIntegerField(default=0)

    quantity = models.PositiveIntegerField(default=1)

    # Como veio na lista ("1 Fuga de Mana")
    input_name = models.CharField(max_length=255)
    input_line = models.CharField(max_length=500, blank=True)

    # Carta encontrada no Scryfall (vazio = não encontrada)
    name = models.CharField(max_length=255, blank=True)
    scryfall_id = models.CharField(max_length=64, blank=True)
    oracle_id = models.CharField(max_length=64, blank=True)

    is_commander = models.BooleanField(default=False)
    category = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["position", "pk"]

    def __str__(self):
        return f"{self.quantity} {self.name or self.input_name}"


class BrlPrice(models.Model):
    """
    Preço da LigaMagic de cartas que não estão na coleção.

    (As cartas da coleção guardam o preço no próprio Card.)
    """

    name = models.CharField(max_length=255)
    name_normalized = models.CharField(max_length=255, unique=True)

    price_brl = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    url = models.URLField(max_length=500, blank=True)
    error = models.CharField(max_length=255, blank=True)

    updated_at = models.DateTimeField(db_index=True)

    class Meta:
        verbose_name = "preço LigaMagic"
        verbose_name_plural = "preços LigaMagic"

    def __str__(self):
        return f"{self.name}: {self.price_brl}"
