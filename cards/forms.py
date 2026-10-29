from django import forms

from cards.models import FORMAT_CHOICES, RARITY_CHOICES
from cards.services.similar import PRIMARY_TYPES


COLOR_CHOICES = [
    ("W", "Branco"),
    ("U", "Azul"),
    ("B", "Preto"),
    ("R", "Vermelho"),
    ("G", "Verde"),
    ("C", "Incolor"),
]

COLOR_MODE_CHOICES = [
    ("any", "Qualquer uma"),
    ("all", "Todas (pode ter outras)"),
    ("exact", "Exatamente essas"),
    ("atmost", "No máximo essas"),
]

TYPE_CHOICES = [
    ("Creature", "Criatura"),
    ("Instant", "Mágica instantânea"),
    ("Sorcery", "Feitiço"),
    ("Artifact", "Artefato"),
    ("Enchantment", "Encantamento"),
    ("Planeswalker", "Planeswalker"),
    ("Land", "Terreno"),
    ("Battle", "Batalha"),
    ("Legendary", "Lendário"),
    ("Tribal", "Tribal / Kindred"),
]

SCOPE_CHOICES = [
    ("scryfall", "Todas as cartas"),
    ("collection", "Minha coleção"),
]

SORT_CHOICES = [
    ("name", "Nome"),
    ("edhrec", "Popularidade (EDHREC)"),
    ("price_brl", "Preço (R$)"),
    ("price_usd", "Preço (US$)"),
    ("cmc", "Valor de mana"),
    ("rarity", "Raridade"),
    ("set", "Edição"),
    ("released", "Lançamento"),
    ("power", "Poder"),
    ("toughness", "Resistência"),
    ("quantity", "Quantidade"),
]

ORDER_CHOICES = [("asc", "Crescente"), ("desc", "Decrescente")]

VIEW_CHOICES = [("grid", "Imagens"), ("list", "Lista")]


def _number(label, step="1", **kwargs):
    return forms.DecimalField(
        label=label,
        required=False,
        min_value=0,
        widget=forms.NumberInput(attrs={"step": step, "placeholder": label}),
        **kwargs,
    )


class SearchForm(forms.Form):

    scope = forms.ChoiceField(
        required=False, choices=SCOPE_CHOICES, initial="scryfall",
        widget=forms.RadioSelect,
    )

    unique = forms.BooleanField(
        label="Mostrar todas as impressões", required=False,
    )

    q = forms.CharField(
        label="Busca",
        required=False,
        widget=forms.TextInput(attrs={
            "placeholder": 'Nome ou sintaxe: t:creature c:ug mv<=3 brl<5 o:"draw a card"',
            "autocomplete": "off",
            "autofocus": True,
        }),
    )

    name = forms.CharField(label="Nome", required=False)
    text = forms.CharField(
        label="Texto da carta",
        required=False,
        help_text="Separe vários trechos com ;",
        widget=forms.TextInput(attrs={"placeholder": "draw a card; flying"}),
    )

    types = forms.MultipleChoiceField(
        label="Tipos", required=False, choices=TYPE_CHOICES,
        widget=forms.CheckboxSelectMultiple,
    )
    subtype = forms.CharField(
        label="Subtipo", required=False,
        widget=forms.TextInput(attrs={"placeholder": "Elf, Dragon, Equipment..."}),
    )

    colors = forms.MultipleChoiceField(
        label="Cores", required=False, choices=COLOR_CHOICES,
        widget=forms.CheckboxSelectMultiple,
    )
    color_mode = forms.ChoiceField(
        label="Modo", required=False, choices=COLOR_MODE_CHOICES, initial="any",
    )

    identity = forms.MultipleChoiceField(
        label="Identidade de cor (Commander)", required=False,
        choices=COLOR_CHOICES[:5], widget=forms.CheckboxSelectMultiple,
    )

    rarities = forms.MultipleChoiceField(
        label="Raridade", required=False, choices=RARITY_CHOICES[:4],
        widget=forms.CheckboxSelectMultiple,
    )

    set_code = forms.CharField(
        label="Edição", required=False,
        widget=forms.TextInput(attrs={"placeholder": "Código ou nome: LTR, MH3"}),
    )

    keyword = forms.CharField(
        label="Palavra-chave", required=False,
        widget=forms.TextInput(attrs={"placeholder": "Flying, Trample, Ward..."}),
    )

    format = forms.ChoiceField(
        label="Legal no formato", required=False,
        choices=[("", "Qualquer formato")] + FORMAT_CHOICES,
    )

    cmc_min = _number("Mín.")
    cmc_max = _number("Máx.")
    power_min = _number("Mín.")
    power_max = _number("Máx.")
    toughness_min = _number("Mín.")
    toughness_max = _number("Máx.")

    price_min = _number("R$ mín.", step="0.01")
    price_max = _number("R$ máx.", step="0.01")
    usd_min = _number("US$ mín.", step="0.01")
    usd_max = _number("US$ máx.", step="0.01")

    only_priced = forms.BooleanField(label="Só cartas com preço", required=False)

    quantity_min = forms.IntegerField(
        label="Quantidade mínima (só na coleção)", required=False, min_value=1,
        widget=forms.NumberInput(attrs={"placeholder": "1"}),
    )

    sort = forms.ChoiceField(label="Ordenar por", required=False, choices=SORT_CHOICES)
    order = forms.ChoiceField(label="Ordem", required=False, choices=ORDER_CHOICES)
    view = forms.ChoiceField(required=False, choices=VIEW_CHOICES)

    # Campos avançados: o painel abre sozinho quando algum estiver preenchido
    ADVANCED = [
        "name", "text", "types", "subtype", "colors", "identity", "rarities",
        "set_code", "keyword", "format", "cmc_min", "cmc_max", "power_min",
        "power_max", "toughness_min", "toughness_max", "price_min", "price_max",
        "usd_min", "usd_max", "only_priced", "quantity_min",
    ]

    def active_filter_count(self):
        if not self.is_bound or not hasattr(self, "cleaned_data"):
            return 0

        return sum(
            1 for name in self.ADVANCED
            if self.cleaned_data.get(name) not in (None, "", [], False)
        )


SIMILAR_SORT_CHOICES = [
    ("edhrec", "Mais populares (EDHREC)"),
    ("price_brl", "Mais baratas"),
    ("cmc", "Menor valor de mana"),
    ("name", "Nome"),
]


class SimilarForm(forms.Form):
    """Controles da busca de similares; as opções dependem da carta de referência."""

    # Marca que o formulário foi enviado (senão valem os padrões)
    s = forms.CharField(required=False, widget=forms.HiddenInput, initial="1")

    tags = forms.MultipleChoiceField(
        label="Tags obrigatórias", required=False, widget=forms.CheckboxSelectMultiple,
    )
    colors = forms.MultipleChoiceField(
        label="Cores obrigatórias", required=False, widget=forms.CheckboxSelectMultiple,
    )
    only_ref_colors = forms.BooleanField(
        label="Sem cores além das da referência", required=False,
    )
    card_types = forms.MultipleChoiceField(
        label="Tipos (qualquer um dos marcados)", required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    max_price = forms.DecimalField(
        label="Preço máximo (R$)", required=False, min_value=0, decimal_places=2,
        widget=forms.NumberInput(attrs={"step": "0.01", "placeholder": "Sem limite"}),
    )
    cmc_diff = forms.DecimalField(
        label="Máxima diferença de valor de mana", required=False, min_value=0,
        widget=forms.NumberInput(attrs={"step": "1", "placeholder": "Qualquer"}),
    )

    only_owned = forms.BooleanField(label="Só cartas da minha coleção", required=False)

    sort = forms.ChoiceField(label="Ordenar", required=False, choices=SIMILAR_SORT_CHOICES)

    def __init__(self, reference, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.reference = reference

        self.fields["tags"].choices = [(tag.label, tag.text) for tag in reference.tags]
        self.fields["colors"].choices = [(c, c) for c in reference.colors]

        self.fields["card_types"].choices = PRIMARY_TYPES

        if reference.cmc is not None:
            self.fields["cmc_diff"].help_text = (
                f"A referência tem valor de mana {reference.cmc:g}. "
                "0 = mesmo valor; vazio = qualquer."
            )

    def tag_options(self):
        """[(BoundWidget, Tag)] para mostrar a contagem ao lado de cada tag."""

        by_label = {tag.label: tag for tag in self.reference.tags}

        return [(option, by_label[option.data["value"]]) for option in self["tags"]]


DECK_PLACEHOLDER = """Comandante
1 Vren, the Relentless

Deck
1 Fuga de Mana
1 Sol Ring
1x Arcane Signet (cmm) 371 [Ramp]
1x Vren, the Relentless (blb) [Commander{top}]"""


class DeckImportForm(forms.Form):

    decklist = forms.CharField(
        label="Lista do deck",
        widget=forms.Textarea(attrs={
            "rows": 16,
            "placeholder": DECK_PLACEHOLDER,
            "spellcheck": "false",
        }),
    )


class BuilderForm(forms.Form):

    commander = forms.CharField(
        label="Comandante",
        widget=forms.TextInput(attrs={
            "placeholder": "Ex.: Vren, the Relentless",
            "list": "card-suggestions",
            "autocomplete": "off",
        }),
    )
    partner = forms.CharField(
        label="Segundo comandante (opcional)",
        required=False,
        help_text="Partner, background, friends forever…",
        widget=forms.TextInput(attrs={
            "placeholder": "Ex.: Candlekeep Sage",
            "list": "card-suggestions",
            "autocomplete": "off",
        }),
    )
    budget = forms.DecimalField(
        label="Orçamento para as mágicas (R$)", required=False, min_value=0, decimal_places=2,
        help_text="Opcional. O comandante e os 40 terrenos básicos não contam.",
        widget=forms.NumberInput(attrs={"step": "1", "placeholder": "Sem limite"}),
    )
    # Tags a valorizar, separadas por vírgula (montado pela página; ver app.js)
    priority_tags = forms.CharField(required=False, widget=forms.HiddenInput(attrs={"id": "priority-tags"}))

    def clean_priority_tags(self):
        raw = self.cleaned_data.get("priority_tags") or ""
        return [tag.strip() for tag in raw.split(",") if tag.strip()]


class BudgetForm(forms.Form):

    budget = forms.DecimalField(
        label="Orçamento total (R$)", required=False, min_value=0, decimal_places=2,
        widget=forms.NumberInput(attrs={"step": "1", "placeholder": "Sem limite"}),
    )


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput(attrs={"accept": ".csv,.tsv,.txt"}))
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single = super().clean
        if isinstance(data, (list, tuple)):
            return [single(item, initial) for item in data]
        return [single(data, initial)]


class ImportForm(forms.Form):

    files = MultipleFileField(label="Arquivos CSV da LigaMagic")


class AddCardForm(forms.Form):

    name = forms.CharField(
        label="Nome da carta",
        widget=forms.TextInput(attrs={
            "placeholder": "Ex.: Lightning Bolt",
            "list": "card-suggestions",
            "autocomplete": "off",
        }),
    )
    quantity = forms.IntegerField(label="Quantidade", min_value=1, initial=1)
