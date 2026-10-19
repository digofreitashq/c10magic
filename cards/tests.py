import uuid
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from cards.models import (
    BrlPrice,
    CachedCard,
    Card,
    Deck,
    DeckCard,
    EdhrecPage,
    SyncState,
    TagIndex,
    TagStat,
)
from cards.search import (
    QuerySyntaxError,
    apply_filters,
    parse_query,
    scryfall_colors,
    to_scryfall_query,
)
from cards.services.catalog import ScryfallResults, from_scryfall
from cards.services.decklist import parse_decklist
from cards.services.edhrec import slugify as edhrec_slugify
from cards.services.prices import fetch_price
from cards.services.similar import build_reference, collection_queryset
from cards.services.similar import scryfall_query as similar_query
from cards.services.tags import is_stale as tags_is_stale
from cards.services.tags import sync_kind, tags_for
from cards.services.importer import read_collection
from cards.services.scryfall import card_fields
from cards.services.updater import CardUpdater, import_csv_content, stale_cards


SCRYFALL_BOLT = {
    "object": "card",
    "id": "abc",
    "oracle_id": "o-bolt",
    "name": "Lightning Bolt",
    "mana_cost": "{R}",
    "cmc": 1.0,
    "type_line": "Instant",
    "oracle_text": "Lightning Bolt deals 3 damage to any target.",
    "colors": ["R"],
    "color_identity": ["R"],
    "keywords": [],
    "legalities": {"modern": "legal", "standard": "not_legal", "pauper": "legal"},
    "rarity": "common",
    "set": "m10",
    "set_name": "Magic 2010",
    "collector_number": "146",
    "image_uris": {"normal": "https://img/bolt.jpg", "large": "https://img/bolt-l.jpg"},
    "prices": {"usd": "1.50", "usd_foil": "5.00"},
    "scryfall_uri": "https://scryfall.com/card/m10/146",
}


def make(name, **fields):
    now = timezone.now()
    defaults = {
        "name_normalized": name.lower(),
        "scryfall_updated_at": now,
        "ligamagic_updated_at": now,
    }
    defaults.update(fields)
    return Card.objects.create(name=name, **defaults)


def names(q):
    return sorted(Card.objects.filter(q).values_list("name", flat=True))


class SearchSyntaxTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        make("Llanowar Elves", type_line="Creature — Elf Druid", colors="G",
             color_identity="G", cmc=1, power_num=1, toughness_num=1,
             rarity="common", price_brl=Decimal("0.50"), price_usd=Decimal("0.20"),
             keywords="|", legal_formats="|commander|modern|pauper|",
             oracle_text="{T}: Add {G}.", mana_cost="{G}")
        make("Counterspell", type_line="Instant", colors="U", color_identity="U",
             cmc=2, rarity="uncommon", price_brl=Decimal("3.00"),
             legal_formats="|commander|pauper|", mana_cost="{U}{U}",
             oracle_text="Counter target spell.")
        make("Niv-Mizzet, Parun", type_line="Legendary Creature — Dragon Wizard",
             colors="UR", color_identity="UR", cmc=6, power_num=5, toughness_num=5,
             rarity="rare", price_brl=Decimal("12.00"), keywords="|flying|",
             legal_formats="|commander|", oracle_text="Flying\nWhenever you draw a card, ...")
        make("Sol Ring", type_line="Artifact", colors="", color_identity="", cmc=1,
             rarity="uncommon", price_brl=Decimal("8.00"),
             legal_formats="|commander|", oracle_text="{T}: Add {C}{C}.")
        make("Sliver Queen", type_line="Legendary Creature — Sliver", colors="WUBRG",
             color_identity="WUBRG", cmc=7, power_num=7, toughness_num=7, rarity="rare",
             price_brl=None)

    def test_bare_word_matches_name(self):
        self.assertEqual(names(parse_query("bolt")), [])
        self.assertEqual(names(parse_query("ring")), ["Sol Ring"])

    def test_exact_name(self):
        self.assertEqual(names(parse_query('!"sol ring"')), ["Sol Ring"])

    def test_type(self):
        self.assertEqual(names(parse_query("t:creature t:legendary")), ["Niv-Mizzet, Parun", "Sliver Queen"])

    def test_color_contains(self):
        self.assertEqual(names(parse_query("c:u")), ["Counterspell", "Niv-Mizzet, Parun", "Sliver Queen"])

    def test_color_exact(self):
        self.assertEqual(names(parse_query("c=u")), ["Counterspell"])

    def test_color_guild_name(self):
        self.assertEqual(names(parse_query("c=izzet")), ["Niv-Mizzet, Parun"])

    def test_colorless_and_multicolor(self):
        self.assertEqual(names(parse_query("c:c")), ["Sol Ring"])
        self.assertEqual(names(parse_query("c:m")), ["Niv-Mizzet, Parun", "Sliver Queen"])

    def test_identity_is_at_most(self):
        # Cabe num deck Izzet: azul, vermelho, U/R e incolor
        self.assertEqual(names(parse_query("id:ur")), ["Counterspell", "Niv-Mizzet, Parun", "Sol Ring"])

    def test_numeric_comparisons(self):
        self.assertEqual(names(parse_query("mv<=1")), ["Llanowar Elves", "Sol Ring"])
        self.assertEqual(names(parse_query("pow>=5")), ["Niv-Mizzet, Parun", "Sliver Queen"])
        self.assertEqual(names(parse_query("brl<5")), ["Counterspell", "Llanowar Elves"])

    def test_rarity_range(self):
        self.assertEqual(names(parse_query("r>=rare")), ["Niv-Mizzet, Parun", "Sliver Queen"])

    def test_negation_and_or(self):
        self.assertEqual(names(parse_query("-t:creature")), ["Counterspell", "Sol Ring"])
        self.assertEqual(names(parse_query("t:elf or t:sliver")), ["Llanowar Elves", "Sliver Queen"])

    def test_oracle_keyword_format(self):
        self.assertEqual(names(parse_query('o:"draw a card"')), ["Niv-Mizzet, Parun"])
        self.assertEqual(names(parse_query("kw:flying")), ["Niv-Mizzet, Parun"])
        self.assertEqual(names(parse_query("f:pauper")), ["Counterspell", "Llanowar Elves"])

    def test_mana_symbols(self):
        self.assertEqual(names(parse_query("m:uu")), ["Counterspell"])

    def test_is_flags(self):
        self.assertEqual(names(parse_query("is:commander")), ["Niv-Mizzet, Parun", "Sliver Queen"])
        self.assertEqual(names(parse_query("is:unpriced")), ["Sliver Queen"])

    def test_unknown_key_raises(self):
        with self.assertRaises(QuerySyntaxError):
            parse_query("xyz:1")

    def test_form_filters_and_sort(self):
        result = apply_filters(
            Card.objects.all(),
            {"colors": ["U", "R"], "color_mode": "any", "price_max": Decimal("20"),
             "sort": "price_brl", "order": "desc"},
        )
        self.assertEqual([c.name for c in result], ["Niv-Mizzet, Parun", "Counterspell"])

        result = apply_filters(Card.objects.all(), {"colors": ["C"], "color_mode": "exact"})
        self.assertEqual([c.name for c in result], ["Sol Ring"])

        result = apply_filters(Card.objects.all(), {"identity": ["G"]})
        self.assertEqual([c.name for c in result], ["Llanowar Elves", "Sol Ring"])


class WeeklyRefreshTests(TestCase):

    def setUp(self):
        self.updater = CardUpdater()
        self.updater.scryfall = mock.Mock()
        self.updater.scryfall.get_card.return_value = SCRYFALL_BOLT
        self.updater.ligamagic = mock.Mock()
        self.updater.ligamagic.get_price.return_value = {
            "price_brl": 2.5, "foil_price_brl": None, "url": "https://liga/bolt",
        }

    def test_first_load_fetches_both_sources(self):
        card = Card.objects.create(name="Lightning Bolt", name_normalized="lightning bolt")

        done = self.updater.refresh(card)

        self.assertEqual(done, {"scryfall": True, "ligamagic": True})
        card.refresh_from_db()
        self.assertEqual(card.price_brl, Decimal("2.50"))
        self.assertEqual(card.price_usd, Decimal("1.50"))
        self.assertEqual(card.colors, "R")
        self.assertEqual(card.image_url, "https://img/bolt.jpg")
        self.assertFalse(card.is_stale())

    def test_fresh_card_is_not_fetched_again(self):
        card = make("Lightning Bolt", scryfall_updated_at=timezone.now() - timedelta(days=6))

        done = self.updater.refresh(card)

        self.assertEqual(done, {"scryfall": False, "ligamagic": False})
        self.updater.scryfall.get_card.assert_not_called()
        self.updater.ligamagic.get_price.assert_not_called()

    def test_card_older_than_a_week_is_fetched(self):
        old = timezone.now() - timedelta(days=8)
        card = make("Lightning Bolt", scryfall_updated_at=old, ligamagic_updated_at=timezone.now())

        done = self.updater.refresh(card)

        self.assertEqual(done, {"scryfall": True, "ligamagic": False})

    def test_stale_queryset(self):
        make("Fresh")
        make("Old", ligamagic_updated_at=timezone.now() - timedelta(days=10))
        Card.objects.create(name="New", name_normalized="new")

        self.assertEqual(sorted(c.name for c in stale_cards()), ["New", "Old"])

    def test_network_error_does_not_mark_as_updated(self):
        import requests

        self.updater.ligamagic.get_price.side_effect = requests.ConnectionError("offline")
        card = Card.objects.create(name="Lightning Bolt", name_normalized="lightning bolt")

        self.updater.refresh(card)
        card.refresh_from_db()

        self.assertIsNone(card.ligamagic_updated_at)
        self.assertIn("offline", card.ligamagic_error)


class ScryfallFieldsTests(TestCase):

    def test_double_faced_card(self):
        data = {
            "id": "x", "name": "Delver of Secrets // Insectile Aberration", "cmc": 1,
            "type_line": "Creature — Human Wizard // Creature — Human Insect",
            "color_identity": ["U"], "rarity": "common", "set": "isd",
            "card_faces": [
                {"name": "Delver of Secrets", "mana_cost": "{U}", "colors": ["U"],
                 "oracle_text": "At the beginning...", "power": "1", "toughness": "1",
                 "image_uris": {"normal": "front.jpg"}},
                {"name": "Insectile Aberration", "mana_cost": "", "colors": ["U"],
                 "oracle_text": "Flying", "power": "3", "toughness": "2",
                 "image_uris": {"normal": "back.jpg"}},
            ],
            "prices": {},
        }

        fields = card_fields(data)

        self.assertEqual(fields["image_url"], "front.jpg")
        self.assertEqual(fields["image_url_back"], "back.jpg")
        self.assertEqual(fields["colors"], "U")
        self.assertEqual(fields["power_num"], 1.0)
        self.assertIn("Flying", fields["oracle_text"])


class ImporterTests(TestCase):

    CSV = (
        "Quantity,Name,Simple Name,Set,Card Number,Set Code,Printing\n"
        "2,Errand-Rider of Gondor,Errand-Rider of Gondor,LTR,11,LTR,Normal\n"
        "1,Errand-Rider of Gondor,Errand-Rider of Gondor,LTR,11,LTR,Foil\n"
        "1,Sol Ring,Sol Ring,CMR,472,CMR,Normal\n"
    )

    def test_reads_set_and_number(self):
        rows = read_collection(self.CSV)
        self.assertEqual(rows[0], {
            "name": "Errand-Rider of Gondor", "quantity": 2,
            "set_code": "LTR", "collector_number": "11",
        })

    def test_import_sums_quantities(self):
        result = import_csv_content(self.CSV)

        self.assertEqual(result, {"rows": 3, "created": 2})
        card = Card.objects.get(name_normalized="errand-rider of gondor")
        self.assertEqual(card.quantity, 3)
        self.assertEqual(card.hint_set_code, "LTR")


class OfflineMixin:
    """Sem rede nos testes: cotação fixa e fila de preços simulada."""

    def setUp(self):
        super().setUp()

        for target in ("cards.views.usd_brl",):
            patcher = mock.patch(target, return_value=Decimal("5"))
            patcher.start()
            self.addCleanup(patcher.stop)

        patcher = mock.patch("cards.views.price_queue")
        self.price_queue = patcher.start()
        self.price_queue.size.return_value = 0
        self.price_queue.has_failed.return_value = False
        self.addCleanup(patcher.stop)

        patcher = mock.patch("cards.views.background_tags")
        self.background_tags = patcher.start()
        self.background_tags.running = False
        self.addCleanup(patcher.stop)

        cache.clear()


class ViewTests(OfflineMixin, TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.card = make("Counterspell", type_line="Instant", colors="U", cmc=2,
                        mana_cost="{U}{U}", rarity="uncommon",
                        price_brl=Decimal("3.00"), image_url="https://img/cs.jpg",
                        raw_scryfall={"legalities": {"commander": "legal", "modern": "not_legal"}})

    def test_search_page(self):
        response = self.client.get(reverse("cards:search"), {"q": "c:u brl<=5", "scope": "collection"})
        self.assertContains(response, "Counterspell")
        self.assertContains(response, "R$ 3,00")
        self.assertContains(response, "https://img/cs.jpg")

    def test_search_list_view(self):
        response = self.client.get(reverse("cards:search"), {"q": "counter", "view": "list", "scope": "collection"})
        self.assertContains(response, "card-table")

    def test_search_syntax_error_is_shown(self):
        response = self.client.get(reverse("cards:search"), {"q": "zzz:1", "scope": "collection"})
        self.assertContains(response, "chave desconhecida")

    def test_landing_page_without_query(self):
        response = self.client.get(reverse("cards:search"))
        self.assertContains(response, "Encontre qualquer carta")

    def test_detail_page_does_not_refetch_fresh_card(self):
        with mock.patch("cards.views.refresh_card") as refresh:
            response = self.client.get(reverse("cards:detail", args=[self.card.pk]))

        refresh.assert_not_called()
        self.assertContains(response, "Counterspell")
        self.assertContains(response, "Commander")

    def test_manage_page(self):
        response = self.client.get(reverse("cards:manage"))
        self.assertContains(response, "Importar CSV")

    def test_refresh_respects_week(self):
        with mock.patch("cards.services.updater.CardUpdater.update_scryfall") as update:
            response = self.client.post(reverse("cards:refresh", args=[self.card.pk]), follow=True)

        update.assert_not_called()
        self.assertContains(response, "menos de 7 dias")


def uid(value):
    """IDs do Scryfall são UUIDs."""

    try:
        return str(uuid.UUID(value))
    except ValueError:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, value))


def sf_card(name, oracle_id, scryfall_id, usd="1.00", **extra):
    scryfall_id = uid(scryfall_id)
    data = {
        "object": "card", "id": scryfall_id, "oracle_id": oracle_id, "name": name,
        "mana_cost": "{U}", "cmc": 1, "type_line": "Instant", "colors": ["U"],
        "color_identity": ["U"], "rarity": "common", "set": "tst",
        "collector_number": "1", "legalities": {},
        "image_uris": {"normal": f"https://img/{scryfall_id}.jpg"},
        "prices": {"usd": usd},
    }
    data.update(extra)
    return data


class ScryfallQueryTests(TestCase):

    RATE = Decimal("5")

    def test_brl_terms_become_usd(self):
        self.assertEqual(to_scryfall_query({"q": "t:elf brl<10"}, self.RATE), "t:elf usd<2.00")
        self.assertEqual(to_scryfall_query({"q": "-brl>=2,5"}, self.RATE), "-usd>=0.50")

    def test_form_filters(self):
        query = to_scryfall_query({
            "colors": ["U", "R"], "color_mode": "any",
            "identity": ["G", "B"],
            "types": ["Creature"],
            "rarities": ["rare", "mythic"],
            "format": "commander",
            "cmc_max": Decimal("3"),
            "price_max": Decimal("20"),
        }, self.RATE)

        self.assertEqual(
            query,
            "t:creature (c:u or c:r) id<=bg (r:rare or r:mythic) f:commander mv<=3 usd<=4.00",
        )

    def test_color_modes(self):
        self.assertEqual(scryfall_colors(["U", "R"], "exact"), "c=ur")
        self.assertEqual(scryfall_colors(["U", "R"], "atmost"), "c<=ur")
        self.assertEqual(scryfall_colors(["U", "R"], "all"), "c>=ur")
        self.assertEqual(scryfall_colors(["C"], "any"), "c:c")

    def test_empty(self):
        self.assertEqual(to_scryfall_query({}, self.RATE), "")


class ScryfallResultsTests(TestCase):

    def setUp(self):
        cache.clear()

    def test_slices_across_scryfall_pages(self):
        client = mock.Mock()

        def page(query, number, *args):
            start = (number - 1) * 175
            count = min(175, 400 - start)
            return {
                "data": [sf_card(f"Card {start + i}", f"o{start + i}", f"id-{start + i}") for i in range(count)],
                "total_cards": 400,
            }

        client.search_page.side_effect = page

        results = ScryfallResults("t:instant", client=client)

        self.assertEqual(len(results), 400)
        chunk = results[150:210]
        self.assertEqual([c["name"] for c in chunk[:1] + chunk[-1:]], ["Card 150", "Card 209"])
        # Página 1 (contagem + início) e página 2
        self.assertEqual(client.search_page.call_count, 2)

        # De novo: vem do cache
        results = ScryfallResults("t:instant", client=client)
        results[0:10]
        self.assertEqual(client.search_page.call_count, 2)

        # Cartas vistas ficam guardadas
        self.assertTrue(CachedCard.objects.filter(scryfall_id=uid("id-200")).exists())

    def test_marks_owned_cards(self):
        make("Counterspell", oracle_id="o-cs", quantity=3, scryfall_id="other-print")
        # Coleção guarda só o nome da frente
        make("Delver of Secrets", quantity=1)

        cards = [
            sf_card("Counterspell", "o-cs", "cs-1"),
            sf_card("Delver of Secrets // Insectile Aberration", "o-delver", "d-1"),
            sf_card("Brainstorm", "o-bs", "bs-1", usd=None),
        ]

        items = {i.name: i for i in from_scryfall(cards, Decimal("5"))}

        self.assertEqual(items["Counterspell"].owned, 3)
        self.assertFalse(items["Counterspell"].owned_print)
        self.assertEqual(items["Delver of Secrets // Insectile Aberration"].owned, 1)
        self.assertEqual(items["Brainstorm"].owned, 0)

    def test_brl_from_collection_cache_or_estimate(self):
        make("Counterspell", price_brl=Decimal("3.00"))
        BrlPrice.objects.create(name="Opt", name_normalized="opt", price_brl=Decimal("0.25"),
                                updated_at=timezone.now())
        BrlPrice.objects.create(name="Ponder", name_normalized="ponder", price_brl=Decimal("1"),
                                updated_at=timezone.now() - timedelta(days=8))

        cards = [
            sf_card("Counterspell", "o1", "a"),
            sf_card("Opt", "o2", "b"),
            sf_card("Ponder", "o3", "c", usd="0.40"),
        ]

        items = {i.name: i for i in from_scryfall(cards, Decimal("5"))}

        self.assertEqual((items["Counterspell"].brl_status, items["Counterspell"].price_brl), ("ok", Decimal("3.00")))
        self.assertEqual((items["Opt"].brl_status, items["Opt"].price_brl), ("ok", Decimal("0.25")))
        # Preço com mais de uma semana: busca de novo, mostra estimativa
        self.assertEqual(items["Ponder"].brl_status, "pending")
        self.assertEqual(items["Ponder"].brl_estimate, Decimal("2.00"))


class ScryfallViewTests(OfflineMixin, TestCase):

    def setUp(self):
        super().setUp()

        patcher = mock.patch("cards.services.catalog.ScryfallClient")
        client = patcher.start().return_value
        self.addCleanup(patcher.stop)

        self.cards = [
            sf_card("Counterspell", "o-cs", "11111111-1111-1111-1111-111111111111"),
            sf_card("Brainstorm", "o-bs", "22222222-2222-2222-2222-222222222222"),
        ]
        client.search_page.return_value = {"data": self.cards, "total_cards": 2, "warnings": []}
        client.get_by_id.side_effect = lambda sid: next(c for c in self.cards if c["id"] == sid)
        self.client_mock = client

        make("Counterspell", oracle_id="o-cs", quantity=2, price_brl=Decimal("3"))

    def test_search_marks_collection_and_queues_prices(self):
        response = self.client.get(reverse("cards:search"), {"q": "t:instant brl<5"})

        self.client_mock.search_page.assert_called_once()
        self.assertEqual(self.client_mock.search_page.call_args.args[0], "t:instant usd<1.00")

        self.assertContains(response, "Brainstorm")
        self.assertContains(response, "owned-badge", count=1)
        self.assertContains(response, "×2")
        # Counterspell já tem preço (coleção); só Brainstorm vai para a fila
        self.assertContains(response, "R$ 3,00")
        self.price_queue.request.assert_called_once_with(["Brainstorm"])

    def test_scryfall_error_is_shown(self):
        from cards.services.scryfall import ScryfallQueryError

        self.client_mock.search_page.side_effect = ScryfallQueryError("All of your terms were ignored.")
        response = self.client.get(reverse("cards:search"), {"q": "zzz:1"})
        self.assertContains(response, "All of your terms were ignored.")

    def test_detail_and_add_to_collection(self):
        sid = self.cards[1]["id"]
        BrlPrice.objects.create(name="Brainstorm", name_normalized="brainstorm",
                                price_brl=Decimal("0.50"), updated_at=timezone.now())

        response = self.client.get(reverse("cards:scryfall_detail", args=[sid]))
        self.assertContains(response, "não está na sua coleção")
        self.assertContains(response, "R$ 0,50")

        response = self.client.post(reverse("cards:scryfall_add", args=[sid]), {"quantity": 4}, follow=True)
        card = Card.objects.get(name="Brainstorm")
        self.assertEqual(card.quantity, 4)
        self.assertEqual(card.price_brl, Decimal("0.50"))
        self.assertFalse(card.is_stale())
        self.assertContains(response, "adicionada")

    def test_add_existing_card_sums_quantity(self):
        sid = self.cards[0]["id"]
        self.client.post(reverse("cards:scryfall_add", args=[sid]), {"quantity": 1})
        self.assertEqual(Card.objects.get(name="Counterspell").quantity, 3)

    def test_prices_api(self):
        BrlPrice.objects.create(name="Opt", name_normalized="opt", price_brl=Decimal("0.25"),
                                updated_at=timezone.now())

        response = self.client.get(reverse("cards:prices"), {"n": ["Opt", "Ponder"]})

        prices = response.json()["prices"]
        self.assertEqual(prices["Opt"], {"status": "ok", "brl": "0.25", "display": "R$ 0,25", "url": ""})
        self.assertEqual(prices["Ponder"], {"status": "pending"})
        self.price_queue.request.assert_called_once_with(["Ponder"])


class TagTests(OfflineMixin, TestCase):

    ORACLE = [
        {"label": "removal", "oracle_ids": ["o-bolt", "o-doom"]},
        {"label": "burn", "oracle_ids": ["o-bolt"]},
        {"label": "ramp", "oracle_ids": ["o-elves"]},
    ]
    ART = [
        {"label": "lightning", "illustration_ids": ["i-bolt"]},
        {"label": "fire", "illustration_ids": ["i-bolt"]},
    ]

    def sync(self):
        session = mock.Mock()

        def get(url, **kwargs):
            response = mock.Mock()
            response.json.return_value = {"data": self.ORACLE if url.endswith("/oracle") else self.ART}
            return response

        session.get.side_effect = get

        return {kind: sync_kind(kind, session) for kind in ("oracle", "illustration")}

    def test_sync_builds_index(self):
        self.assertEqual(self.sync(), {"oracle": 3, "illustration": 1})

        tags = tags_for("o-bolt", "i-bolt")
        self.assertEqual([label for label, _ in tags["oracle"]], ["burn", "removal"])
        self.assertEqual([label for label, _ in tags["illustration"]], ["fire", "lightning"])
        self.assertTrue(tags["oracle_ready"])

    def test_resync_replaces_old_tags(self):
        self.sync()
        self.ORACLE = [{"label": "removal", "oracle_ids": ["o-doom"]}]
        self.sync()

        self.assertEqual(tags_for("o-bolt")["oracle"], [])

    def test_weekly_rule(self):
        self.assertTrue(tags_is_stale())
        self.sync()
        self.assertFalse(tags_is_stale())

        SyncState.objects.update(updated_at=timezone.now() - timedelta(days=8))
        self.assertTrue(tags_is_stale())

    def test_collection_search_by_tag(self):
        self.sync()
        make("Lightning Bolt", oracle_id="o-bolt", illustration_id="i-bolt")
        make("Llanowar Elves", oracle_id="o-elves")

        self.assertEqual(names(parse_query("otag:removal")), ["Lightning Bolt"])
        self.assertEqual(names(parse_query("-otag:removal")), ["Llanowar Elves"])
        self.assertEqual(names(parse_query("art:fire")), ["Lightning Bolt"])

    def test_detail_shows_tags(self):
        self.sync()
        card = make("Lightning Bolt", oracle_id="o-bolt", illustration_id="i-bolt")

        response = self.client.get(reverse("cards:detail", args=[card.pk]))

        self.assertContains(response, "?q=otag%3Aremoval")
        self.assertContains(response, "?q=art%3Alightning")
        self.background_tags.start_if_stale.assert_called_once()

    def test_detail_while_first_sync_runs(self):
        self.background_tags.running = True
        card = make("Lightning Bolt", oracle_id="o-bolt")

        response = self.client.get(reverse("cards:detail", args=[card.pk]))

        self.assertContains(response, "Baixando as tags do Scryfall")


class ClearCollectionTests(OfflineMixin, TestCase):

    def setUp(self):
        super().setUp()

        patcher = mock.patch("cards.views.background")
        self.background = patcher.start()
        self.background.status.return_value = {"running": False, "pending": 0, "total": 0, "done": 0}
        self.addCleanup(patcher.stop)

        make("Counterspell", price_brl=Decimal("3.00"), ligamagic_url="https://liga/cs")
        make("Old Card", price_brl=Decimal("9"), ligamagic_updated_at=timezone.now() - timedelta(days=10))
        CachedCard.objects.create(scryfall_id=uid("x"), name="Counterspell",
                                  name_normalized="counterspell", data={}, updated_at=timezone.now())

    def test_requires_confirmation(self):
        response = self.client.post(reverse("cards:manage"), {"action": "clear", "confirm": "sim"}, follow=True)

        self.assertEqual(Card.objects.count(), 2)
        self.assertContains(response, "Digite LIMPAR")

    def test_clears_collection_and_keeps_caches(self):
        response = self.client.post(reverse("cards:manage"), {"action": "clear", "confirm": "LIMPAR"}, follow=True)

        self.assertEqual(Card.objects.count(), 0)
        self.assertContains(response, "2 cartas removidas")
        self.background.stop.assert_called_once()

        # Preço ainda válido é preservado; o vencido não
        self.assertEqual(BrlPrice.objects.get().price_brl, Decimal("3.00"))
        self.assertEqual(BrlPrice.objects.get().name, "Counterspell")
        self.assertEqual(CachedCard.objects.count(), 1)

    def test_button_hidden_when_empty(self):
        Card.objects.all().delete()
        response = self.client.get(reverse("cards:manage"))
        self.assertNotContains(response, "clear-form")


class SimilarTests(OfflineMixin, TestCase):

    BOLT_ID = "11111111-1111-1111-1111-111111111111"

    def setUp(self):
        super().setUp()

        TagIndex.objects.create(kind="oracle", target_id="o-bolt",
                                labels="|burn|creature-removal|removal|triggered-ability|unique-thing|")
        for label, count in [("burn", 900), ("creature-removal", 2500), ("removal", 6000),
                             ("triggered-ability", 15000), ("unique-thing", 1)]:
            TagStat.objects.create(kind="oracle", label=label, count=count)
        SyncState.objects.create(key="tags:oracle", updated_at=timezone.now())

        self.bolt = sf_card("Lightning Bolt", "o-bolt", self.BOLT_ID, usd="2.00",
                            type_line="Instant", colors=["R"], mana_cost="{R}")
        BrlPrice.objects.create(name="Lightning Bolt", name_normalized="lightning bolt",
                                price_brl=Decimal("6.00"), updated_at=timezone.now())

        patcher = mock.patch("cards.services.catalog.ScryfallClient")
        self.client_mock = patcher.start().return_value
        self.addCleanup(patcher.stop)
        self.client_mock.get_by_id.return_value = self.bolt
        self.client_mock.search_page.return_value = {
            "data": [sf_card("Shock", "o-shock", "s-1", usd="0.10", colors=["R"])],
            "total_cards": 1, "warnings": [],
        }

    def reference(self):
        return build_reference(self.bolt, Decimal("5"))

    def test_reference_tags_and_defaults(self):
        ref = self.reference()

        # Tag que só aparece numa carta é escondida; ordem: mais específica primeiro
        self.assertEqual([t.label for t in ref.tags], ["burn", "creature-removal", "removal", "triggered-ability"])
        self.assertEqual(ref.main_type, "Instant")
        self.assertEqual(ref.price_brl, Decimal("6.00"))

        defaults = ref.defaults()
        # Uma tag só: o papel mais específico (burn vem antes de removal)
        self.assertEqual(defaults["tags"], ["burn"])
        self.assertEqual(defaults["colors"], ["R"])
        self.assertEqual(defaults["max_price"], Decimal("6.00"))

    def test_scryfall_query(self):
        ref = self.reference()
        query = similar_query(ref, ref.defaults(), Decimal("5"))

        self.assertEqual(query, "otag:burn c>=r c<=r t:instant usd<=1.20 game:paper -oracleid:o-bolt")

        query = similar_query(ref, {"tags": ["removal"], "colors": [], "only_ref_colors": False,
                                    "card_types": [], "max_price": None}, Decimal("5"))
        self.assertEqual(query, "otag:removal game:paper -oracleid:o-bolt")

    def test_cmc_difference(self):
        # sf_card usa cmc 1
        ref = self.reference()
        base = {"tags": [], "colors": [], "only_ref_colors": False, "card_types": [], "max_price": None}

        self.assertNotIn("mv", similar_query(ref, base, Decimal("5")))
        self.assertEqual(
            similar_query(ref, {**base, "cmc_diff": Decimal("0")}, Decimal("5")),
            "mv=1 game:paper -oracleid:o-bolt",
        )
        # Não pede mv>=0 (seria inútil)
        query = similar_query(ref, {**base, "cmc_diff": Decimal("2")}, Decimal("5"))
        self.assertIn("mv<=3", query)
        self.assertNotIn("mv>=", query)

        ref.cmc = 4.0
        query = similar_query(ref, {**base, "cmc_diff": Decimal("1")}, Decimal("5"))
        self.assertIn("mv>=3 mv<=5", query)

    def test_card_types(self):
        ref = self.reference()
        self.assertEqual(ref.defaults()["card_types"], ["Instant"])

        base = {"tags": [], "colors": [], "only_ref_colors": False, "max_price": None}
        query = similar_query(ref, {**base, "card_types": ["Instant", "Sorcery"]}, Decimal("5"))
        self.assertEqual(query, "(t:instant or t:sorcery) game:paper -oracleid:o-bolt")

    def test_card_types_in_collection(self):
        make("Shock", oracle_id="o-shock", type_line="Instant")
        make("Lava Spike", oracle_id="o-spike", type_line="Sorcery — Arcane")
        make("Prodigal Pyromancer", oracle_id="o-pyro", type_line="Creature — Human Wizard")

        values = {"tags": [], "colors": [], "only_ref_colors": False, "card_types": ["Instant", "Sorcery"],
                  "max_price": None, "sort": "name"}

        self.assertEqual(
            [c.name for c in collection_queryset(self.reference(), values)],
            ["Lava Spike", "Shock"],
        )

    def test_card_types_form(self):
        response = self.client.get(reverse("cards:similar", args=[self.BOLT_ID]), {
            "s": "1", "card_types": ["Instant", "Sorcery"], "partial": "1",
        })
        self.assertIn("(t:instant or t:sorcery)", self.client_mock.search_page.call_args.args[0])

        page = self.client.get(reverse("cards:similar", args=[self.BOLT_ID])).content.decode()
        self.assertRegex(page, r'value="Instant" id="id_card_types_\d+" checked')
        self.assertNotRegex(page, r'value="Creature" id="id_card_types_\d+" checked')
        self.assertEqual(page.count('name="card_types"'), 8)

    def test_cmc_difference_in_collection(self):
        for name, cmc in [("Shock", 1), ("Lightning Strike", 2), ("Fireball", 1), ("Banefire", 5)]:
            make(name, oracle_id=f"o-{name}", colors="R", cmc=cmc)

        ref = self.reference()
        ref.cmc = 2.0
        values = {"tags": [], "colors": [], "only_ref_colors": False, "card_types": [],
                  "max_price": None, "cmc_diff": Decimal("1"), "sort": "name"}

        self.assertEqual(
            [c.name for c in collection_queryset(ref, values)],
            ["Fireball", "Lightning Strike", "Shock"],
        )

    def test_colorless_reference(self):
        ref = build_reference(sf_card("Sol Ring", "o-sol", "sol", colors=[], type_line="Artifact"), Decimal("5"))
        query = similar_query(ref, ref.defaults(), Decimal("5"))
        self.assertIn("c:c", query)
        self.assertIn("t:artifact", query)

    def test_page_with_defaults(self):
        response = self.client.get(reverse("cards:similar", args=[self.BOLT_ID]))

        self.assertContains(response, "Cartas similares")
        self.assertContains(response, "Shock")
        self.assertContains(response, "Máxima diferença de valor de mana")
        self.assertContains(response, "A referência tem valor de mana 1.")
        self.assertRegex(response.content.decode(), r'<input type="number" name="cmc_diff"(?![^>]*value=)')
        html = response.content.decode()
        self.assertRegex(html, r'value="burn" id="id_tags_\d+" checked')
        self.assertNotRegex(html, r'value="removal" id="id_tags_\d+" checked')
        self.assertEqual(
            self.client_mock.search_page.call_args.args[0],
            "otag:burn c>=r c<=r t:instant usd<=1.20 game:paper -oracleid:o-bolt",
        )

    def test_default_skips_non_functional_tags(self):
        TagIndex.objects.filter(target_id="o-bolt").update(labels="|meme|burn-with-set-mechanic|cycle-boon|removal|")
        for label, count in [("meme", 130), ("burn-with-set-mechanic", 200), ("cycle-boon", 60)]:
            TagStat.objects.create(kind="oracle", label=label, count=count)

        self.assertEqual(self.reference().defaults()["tags"], ["removal"])

    def test_no_role_tag_means_no_default(self):
        TagIndex.objects.filter(target_id="o-bolt").update(labels="|meme|triggered-ability|")
        TagStat.objects.create(kind="oracle", label="meme", count=130)

        self.assertEqual(self.reference().defaults()["tags"], [])

    def test_partial_update_with_changed_controls(self):
        response = self.client.get(reverse("cards:similar", args=[self.BOLT_ID]), {
            "s": "1", "tags": ["removal"], "max_price": "20", "sort": "price_brl", "partial": "1",
        })

        self.assertNotContains(response, "<html")
        self.assertContains(response, "Shock")
        args = self.client_mock.search_page.call_args.args
        # Desmarcar tudo vale: sem cores, sem "mesmo tipo"
        self.assertEqual(args[0], "otag:removal usd<=4.00 game:paper -oracleid:o-bolt")
        self.assertEqual(args[3:], ("usd", "asc"))

    def test_only_owned(self):
        make("Shock", oracle_id="o-shock", colors="R", type_line="Instant", price_brl=Decimal("0.50"))
        make("Lightning Bolt", oracle_id="o-bolt", colors="R", type_line="Instant")
        make("Giant Growth", oracle_id="o-gg", colors="G", type_line="Instant", price_brl=Decimal("0.5"))
        TagIndex.objects.create(kind="oracle", target_id="o-shock", labels="|burn|")

        response = self.client.get(reverse("cards:similar", args=[self.BOLT_ID]), {
            "s": "1", "tags": ["burn"], "colors": ["R"], "only_ref_colors": "on",
            "only_owned": "on", "max_price": "5",
        })

        self.client_mock.search_page.assert_not_called()
        self.assertEqual([i.name for i in response.context["items"]], ["Shock"])
        self.assertContains(response, "na sua coleção")

    def test_detail_has_button(self):
        card = make("Lightning Bolt", scryfall_id=self.BOLT_ID)
        response = self.client.get(reverse("cards:detail", args=[card.pk]))
        self.assertContains(response, reverse("cards:similar", args=[self.BOLT_ID]))


class DecklistParserTests(TestCase):

    def test_ligamagic_format(self):
        entries = parse_decklist("Comandante\n1 Vren, the Relentless\n\nDeck\n1 Fuga de Mana\n10 Island\n")

        self.assertEqual([(e.quantity, e.name, e.is_commander) for e in entries], [
            (1, "Vren, the Relentless", True),
            (1, "Fuga de Mana", False),
            (10, "Island", False),
        ])

    def test_archidekt_format(self):
        entries = parse_decklist(
            "1x Vren, the Relentless (blb) [Commander{top}]\n"
            "1x Arcane Signet (cmm) 371 [Ramp] ^Have,#37d67a^\n"
            "1x Sol Ring (cmm) 410 *F* [Ramp]\n"
            "1x Brainstorm (sta) 13 [Maybeboard{noDeck}{noPrice}]\n"
        )

        self.assertEqual(len(entries), 3)
        vren, signet, ring = entries
        self.assertEqual((vren.name, vren.set_code, vren.is_commander), ("Vren, the Relentless", "blb", True))
        self.assertEqual((signet.name, signet.set_code, signet.collector_number, signet.is_commander),
                         ("Arcane Signet", "cmm", "371", False))
        self.assertEqual((ring.name, ring.collector_number), ("Sol Ring", "410"))

    def test_sections_and_noise(self):
        entries = parse_decklist(
            "// Comentário\nCommander:\n1 Atraxa, Praetors' Voice\nMainboard\n1 Sol Ring\n"
            "Sideboard\n1 Counterspell\n"
        )
        self.assertEqual([(e.name, e.is_commander) for e in entries],
                         [("Atraxa, Praetors' Voice", True), ("Sol Ring", False)])

    def test_without_commander(self):
        entries = parse_decklist("1 Sol Ring\n1 Vren, the Relentless")
        self.assertFalse(any(e.is_commander for e in entries))

    def test_edhrec_slug(self):
        self.assertEqual(edhrec_slugify("Atraxa, Praetors' Voice"), "atraxa-praetors-voice")
        self.assertEqual(edhrec_slugify("Vren, the Relentless"), "vren-the-relentless")
        self.assertEqual(edhrec_slugify("Esika, God of the Tree // The Prismatic Bridge"), "esika-god-of-the-tree")
        self.assertEqual(edhrec_slugify("Lim-Dûl the Necromancer"), "lim-dul-the-necromancer")


EDHREC_VREN = {
    "container": {"json_dict": {
        "card": {"name": "Vren, the Relentless", "num_decks": 12893},
        "cardlists": [
            {"header": "Top Cards", "tag": "topcards", "cardviews": [
                {"id": uid("arcane-signet"), "name": "Arcane Signet", "num_decks": 100, "potential_decks": 200, "synergy": 0.0},
                {"id": uid("counterspell"), "name": "Counterspell", "num_decks": 90, "potential_decks": 200, "synergy": 0.1},
            ]},
            {"header": "Instants", "tag": "instants", "cardviews": [
                {"id": uid("mana-drain"), "name": "Mana Drain", "num_decks": 50, "potential_decks": 200, "synergy": 0.3},
                {"id": uid("counterspell"), "name": "Counterspell", "num_decks": 90, "potential_decks": 200, "synergy": 0.1},
            ]},
        ],
    }},
    "panels": {"piechart": {"content": [{"label": "Land", "value": 35}, {"label": "Instant", "value": 15}]}},
}


class ExpertTests(OfflineMixin, TestCase):

    def setUp(self):
        super().setUp()

        self.cards = {
            "vren, the relentless": sf_card("Vren, the Relentless", "o-vren", "vren", usd="1.00",
                            type_line="Legendary Creature — Rat Rogue", colors=["U", "B"],
                            color_identity=["U", "B"], cmc=4),
            "mana leak": sf_card("Mana Leak", "o-leak", "mana-leak", usd="0.20",
                                 type_line="Instant", printed_name="Fuga de Mana"),
            "sol ring": sf_card("Sol Ring", "o-sol", "sol-ring", usd="1.50", type_line="Artifact",
                                colors=[], color_identity=[]),
            "island": sf_card("Island", "o-island", "island", usd="0.10", type_line="Basic Land — Island",
                              colors=[], color_identity=["U"]),
            "arcane signet": sf_card("Arcane Signet", "o-signet", "arcane-signet", usd="0.50",
                                     type_line="Artifact", colors=[]),
            "counterspell": sf_card("Counterspell", "o-cs", "counterspell", usd="1.00", type_line="Instant"),
            "mana drain": sf_card("Mana Drain", "o-drain", "mana-drain", usd="40.00", type_line="Instant"),
            "negate": sf_card("Negate", "o-negate", "negate", usd="0.10", type_line="Instant"),
        }
        self.by_id = {data["id"]: data for data in self.cards.values()}

        patcher = mock.patch("cards.services.scryfall.ScryfallClient.collection", autospec=True)
        self.collection = patcher.start()
        self.addCleanup(patcher.stop)

        def collection(client, identifiers):
            out = []
            for ident in identifiers:
                if "id" in ident:
                    out.append(self.by_id.get(ident["id"]))
                else:
                    out.append(self.cards.get(ident.get("name", "").lower()))
            return out

        self.collection.side_effect = collection

        patcher = mock.patch("cards.services.scryfall.ScryfallClient.named_fuzzy", autospec=True)
        fuzzy = patcher.start()
        self.addCleanup(patcher.stop)
        fuzzy.side_effect = lambda client, name: {"fuga de mana": self.cards["mana leak"]}.get(name.lower())

        patcher = mock.patch("cards.services.scryfall.ScryfallClient.get_by_id", autospec=True)
        patcher.start().side_effect = lambda client, sid: self.by_id.get(sid)
        self.addCleanup(patcher.stop)

        patcher = mock.patch("cards.services.scryfall.ScryfallClient.search_page", autospec=True)
        self.search_page = patcher.start()
        self.addCleanup(patcher.stop)
        self.search_page.return_value = {
            "data": [self.cards["counterspell"], self.cards["negate"], self.cards["mana leak"]],
            "total_cards": 3, "warnings": [],
        }

        patcher = mock.patch("cards.services.edhrec.requests.get")
        edhrec_get = patcher.start()
        self.addCleanup(patcher.stop)
        edhrec_get.return_value = mock.Mock(status_code=200, ok=True, json=lambda: EDHREC_VREN)
        self.edhrec_get = edhrec_get

        for name, price in [("Mana Leak", "1.00"), ("Sol Ring", "7.00"), ("Island", "0.10"),
                            ("Vren, the Relentless", "5.00"), ("Counterspell", "4.00"),
                            ("Mana Drain", "200.00"), ("Negate", "0.50"), ("Arcane Signet", "2.00")]:
            BrlPrice.objects.create(name=name, name_normalized=name.lower(), price_brl=Decimal(price),
                                    updated_at=timezone.now())

        TagIndex.objects.create(kind="oracle", target_id="o-leak", labels="|counterspell|soft-counter|")
        TagIndex.objects.create(kind="oracle", target_id="o-drain", labels="|counterspell|")
        TagIndex.objects.create(kind="oracle", target_id="o-cs", labels="|counterspell|")
        TagStat.objects.create(kind="oracle", label="counterspell", count=563)
        TagStat.objects.create(kind="oracle", label="soft-counter", count=142)
        SyncState.objects.create(key="tags:oracle", updated_at=timezone.now())

    DECK = "Comandante\n1 Vren, the Relentless\n\nDeck\n1 Fuga de Mana\n1 Sol Ring\n10 Island\n1 Carta Inventada\n"

    def import_deck(self, text=None):
        """Deck importado pelo serviço (o fluxo pela interface tem teste próprio)."""
        from cards.services.deck import import_deck

        return import_deck(text or self.DECK)

    def test_import_resolves_portuguese_and_commander(self):
        deck = self.import_deck()

        cards = {c.input_name: c for c in deck.cards.all()}
        self.assertEqual(cards["Fuga de Mana"].name, "Mana Leak")
        self.assertTrue(cards["Vren, the Relentless"].is_commander)
        self.assertEqual(cards["Island"].quantity, 10)
        self.assertEqual(cards["Carta Inventada"].scryfall_id, "")
        self.assertEqual(deck.name, "Vren, the Relentless")

    def test_deck_page(self):
        deck = self.import_deck()
        response = self.client.get(reverse("cards:expert_deck", args=[deck.pk]))

        self.assertContains(response, "commander-preview")
        self.assertContains(response, reverse("cards:scryfall_detail", args=[self.cards["vren, the relentless"]["id"]]))
        # 5 + 1 + 7; terreno básico não custa nada
        self.assertContains(response, "R$ 13,00")
        self.assertContains(response, "Carta Inventada")
        self.assertContains(response, "14 cartas")

        # Pool: sem as cartas do deck, com categorias traduzidas
        pool = response.context["pool"]
        self.assertEqual([label for _, label, _ in pool.categories], ["Mais jogadas", "Mágicas instantâneas"])
        self.assertEqual(
            sorted(p.result.name for p in pool.by_id.values()),
            ["Arcane Signet", "Counterspell", "Mana Drain"],
        )
        self.assertEqual(pool.type_comparison, [("Terrenos", 10, 35), ("Mágicas instantâneas", 1, 15)])

    def test_deck_without_commander_asks(self):
        deck = self.import_deck("1 Sol Ring\n1 Vren, the Relentless\n1 Fuga de Mana")
        response = self.client.get(reverse("cards:expert_deck", args=[deck.pk]))

        self.assertContains(response, "Quem é o comandante?")
        self.assertEqual([i.card.name for i in response.context["candidates"]], ["Vren, the Relentless"])

        vren = deck.cards.get(name="Vren, the Relentless")
        self.client.post(reverse("cards:expert_commander", args=[deck.pk]), {"commander": [vren.pk]})

        vren.refresh_from_db()
        self.assertTrue(vren.is_commander)
        self.assertEqual(Deck.objects.get(pk=deck.pk).name, "Vren, the Relentless")

    def test_budget_ignores_commander(self):
        deck = self.import_deck()
        self.client.post(reverse("cards:expert_budget", args=[deck.pk]), {"budget": "7"})

        response = self.client.get(reverse("cards:expert_deck", args=[deck.pk]))
        view = response.context["view"]

        # Total R$ 13 (com Vren, R$ 5; básicos não custam); orçamento conta só R$ 8
        self.assertEqual((view.total, view.commander_total, view.budget_total),
                         (Decimal("13.00"), Decimal("5.00"), Decimal("8.00")))
        self.assertContains(response, "R$ 13,00")
        self.assertContains(response, "R$ 8,00")
        self.assertContains(response, "R$ 1,00 acima do orçamento")
        self.assertContains(response, 'data-commander="1"', count=1)

        self.client.post(reverse("cards:expert_budget", args=[deck.pk]), {"budget": "10"})
        response = self.client.get(reverse("cards:expert_deck", args=[deck.pk]))
        self.assertEqual(response.context["view"].remaining, Decimal("2.00"))
        self.assertNotContains(response, "acima do orçamento")

    def test_suggestions_respect_identity_budget_and_role(self):
        deck = self.import_deck()
        deck.budget = Decimal("15")
        deck.save()
        leak = deck.cards.get(name="Mana Leak")

        response = self.client.get(reverse("cards:expert_suggestions", args=[deck.pk, leak.pk]))
        s = response.context["s"]

        self.assertEqual(s.role, "counterspell")
        # Orçamento: 15 - 8 (deck sem o comandante) + 1 (preço da Mana Leak) = 8
        self.assertEqual(s.max_price, Decimal("8"))
        # Mana Drain (R$ 200) fica fora pelo orçamento
        self.assertEqual([i.result.name for i in s.pool_items], ["Counterspell"])
        self.assertEqual(s.hidden_by_budget, 1)
        self.assertEqual(s.pool_items[0].difference, Decimal("3.00"))
        # Do Scryfall: sem o que já está no deck nem o que já veio do pool
        self.assertEqual([i.result.name for i in s.other_items], ["Negate"])

        query = self.search_page.call_args.args[1]
        self.assertIn("otag:counterspell", query)
        self.assertIn("id<=ub", query)
        self.assertIn("f:commander", query)
        self.assertIn("usd<=1.60", query)

        self.assertContains(response, "Trocar")

    def test_suggestions_with_max_price(self):
        deck = self.import_deck()
        leak = deck.cards.get(name="Mana Leak")
        url = reverse("cards:expert_suggestions", args=[deck.pk, leak.pk])

        # Sem orçamento: só o limite do modal (Counterspell R$ 4 sai, Negate R$ 0,50 fica)
        response = self.client.get(url, {"max_price": "1"})
        s = response.context["s"]
        self.assertEqual((s.max_price, s.budget_max, s.user_max), (Decimal("1"), None, Decimal("1")))
        self.assertEqual([i.result.name for i in s.pool_items + s.other_items], ["Negate"])
        self.assertIn("usd<=0.20", self.search_page.call_args.args[1])
        self.assertContains(response, 'name="max_price"')
        self.assertContains(response, 'value="1"')

        # Com orçamento (15 - 8 + 1 = 8): vale o menor dos dois
        deck.budget = Decimal("15")
        deck.save()
        s = self.client.get(url, {"max_price": "100"}).context["s"]
        self.assertEqual((s.max_price, s.budget_max), (Decimal("8"), Decimal("8")))
        self.assertContains(self.client.get(url, {"max_price": "100"}), "limitado pelo orçamento")

        s = self.client.get(url, {"max_price": "2,5"}).context["s"]
        self.assertEqual(s.max_price, Decimal("2.5"))

        # Valor inválido: ignorado
        s = self.client.get(url, {"max_price": "abc"}).context["s"]
        self.assertIsNone(s.user_max)

    def test_suggestions_tag_toggles(self):
        deck = self.import_deck()
        leak = deck.cards.get(name="Mana Leak")
        url = reverse("cards:expert_suggestions", args=[deck.pk, leak.pk])

        # Padrão: só o papel principal marcado
        response = self.client.get(url)
        s = response.context["s"]
        self.assertEqual(s.selected_tags, ["counterspell"])
        self.assertEqual([t.label for t in s.tags], ["counterspell", "soft-counter"])
        self.assertRegex(response.content.decode(), r'name="tags" value="counterspell" checked')
        self.assertNotRegex(response.content.decode(), r'name="tags" value="soft-counter" checked')

        # Trocar para outra tag
        s = self.client.get(url, {"tags_set": "1", "tags": ["soft-counter"]}).context["s"]
        self.assertEqual(s.selected_tags, ["soft-counter"])
        self.assertEqual(s.pool_items, [])  # nenhuma carta do pool tem soft-counter
        self.assertIn("otag:soft-counter", s.query)
        self.assertNotIn("otag:counterspell", s.query)

        # As duas: todas obrigatórias
        s = self.client.get(url, {"tags_set": "1", "tags": ["counterspell", "soft-counter"]}).context["s"]
        self.assertIn("otag:counterspell otag:soft-counter", s.query)

        # Nenhuma tag e nenhum tipo: qualquer carta da identidade
        response = self.client.get(url, {"tags_set": "1"})
        s = response.context["s"]
        self.assertEqual((s.selected_tags, s.selected_types), ([], []))
        self.assertNotIn("otag:", s.query)
        self.assertNotIn("t:", s.query)
        self.assertEqual([i.result.name for i in s.pool_items], ["Mana Drain", "Counterspell", "Arcane Signet"])
        self.assertContains(response, "nenhuma marcada")

        # Tag que a carta não tem é ignorada
        s = self.client.get(url, {"tags_set": "1", "tags": ["burn", "counterspell"]}).context["s"]
        self.assertEqual(s.selected_tags, ["counterspell"])

    def test_suggestions_type_toggles(self):
        deck = self.import_deck()
        leak = deck.cards.get(name="Mana Leak")
        url = reverse("cards:expert_suggestions", args=[deck.pk, leak.pk])

        # Com papel conhecido: nenhum tipo marcado por padrão, tipo da carta destacado
        response = self.client.get(url)
        self.assertEqual(response.context["s"].selected_types, [])
        html = response.content.decode()
        self.assertEqual(html.count('name="types"'), 8)
        self.assertRegex(html, r'class="chip is-reference"[^>]*>\s*<input type="checkbox" name="types" value="Instant"')

        # Só artefatos: Arcane Signet (pool) sem tag; com a tag counterspell, nada no pool
        s = self.client.get(url, {"tags_set": "1", "types": ["Artifact"]}).context["s"]
        self.assertEqual([i.result.name for i in s.pool_items], ["Arcane Signet"])
        self.assertIn("t:artifact", s.query)

        s = self.client.get(url, {"tags_set": "1", "tags": ["counterspell"], "types": ["Artifact"]}).context["s"]
        self.assertEqual(s.pool_items, [])

        # Vários tipos: qualquer um deles
        s = self.client.get(url, {"tags_set": "1", "types": ["Instant", "Sorcery", "Bogus"]}).context["s"]
        self.assertEqual(s.selected_types, ["Instant", "Sorcery"])
        self.assertIn("(t:instant or t:sorcery)", s.query)

    def test_suggestions_default_type_without_role(self):
        deck = self.import_deck()
        sol = deck.cards.get(name="Sol Ring")  # sem tags de papel nos dados de teste

        s = self.client.get(reverse("cards:expert_suggestions", args=[deck.pk, sol.pk])).context["s"]

        self.assertEqual((s.selected_tags, s.selected_types), ([], ["Artifact"]))
        self.assertIn("t:artifact", s.query)

    def test_swap(self):
        deck = self.import_deck()
        leak = deck.cards.get(name="Mana Leak")

        response = self.client.post(
            reverse("cards:expert_swap", args=[deck.pk, leak.pk]),
            {"scryfall_id": self.cards["counterspell"]["id"]},
        )

        self.assertRedirects(response, reverse("cards:expert_deck", args=[deck.pk]) + f"#card-{leak.pk}",
                             fetch_redirect_response=False)
        leak.refresh_from_db()
        self.assertEqual((leak.name, leak.oracle_id), ("Counterspell", "o-cs"))

    def test_no_suggestion_button_for_commander_or_basics(self):
        deck = self.import_deck()
        response = self.client.get(reverse("cards:expert_deck", args=[deck.pk]))
        html = response.content.decode()

        for name in ("Vren, the Relentless", "Island"):
            card = deck.cards.get(name=name)
            self.assertNotIn(reverse("cards:expert_suggestions", args=[deck.pk, card.pk]), html)

        sol = deck.cards.get(name="Sol Ring")
        self.assertIn(reverse("cards:expert_suggestions", args=[deck.pk, sol.pk]), html)

    def test_edhrec_page_cached_for_a_week(self):
        deck = self.import_deck()
        self.client.get(reverse("cards:expert_deck", args=[deck.pk]))
        self.client.get(reverse("cards:expert_deck", args=[deck.pk]))
        self.assertEqual(self.edhrec_get.call_count, 1)

        EdhrecPage.objects.update(updated_at=timezone.now() - timedelta(days=8))
        self.client.get(reverse("cards:expert_deck", args=[deck.pk]))
        self.assertEqual(self.edhrec_get.call_count, 2)

    def test_empty_list(self):
        self.client.post(reverse("cards:deck_create"))
        deck = Deck.objects.get()
        response = self.client.post(reverse("cards:deck_import", args=[deck.pk]),
                                    {"decklist": "Deck\n\n// nada"}, follow=True)
        self.assertContains(response, "Não encontrei nenhuma carta")
        self.assertFalse(deck.cards.exists())

    def test_delete(self):
        deck = self.import_deck()
        response = self.client.post(reverse("cards:expert_delete", args=[deck.pk]), follow=True)

        self.assertFalse(Deck.objects.exists())
        self.assertFalse(DeckCard.objects.exists())
        self.assertRedirects(response, reverse("cards:decks"))
        self.assertContains(response, "excluído")

    def test_delete_buttons_and_redirects(self):
        deck = self.import_deck()
        delete_url = reverse("cards:expert_delete", args=[deck.pk])

        # Botão no editor e na grade de decks
        self.assertContains(self.client.get(reverse("cards:expert_deck", args=[deck.pk])), "Excluir deck")
        self.assertContains(self.client.get(reverse("cards:decks")), delete_url)

        # Volta para a página indicada
        response = self.client.post(delete_url, {"next": reverse("cards:builder")})
        self.assertRedirects(response, reverse("cards:builder"), fetch_redirect_response=False)

    def test_delete_ignores_external_next(self):
        deck = self.import_deck()
        response = self.client.post(reverse("cards:expert_delete", args=[deck.pk]),
                                    {"next": "https://example.com/"})
        self.assertRedirects(response, reverse("cards:decks"), fetch_redirect_response=False)

    def test_delete_requires_post(self):
        deck = self.import_deck()
        self.assertEqual(self.client.get(reverse("cards:expert_delete", args=[deck.pk])).status_code, 405)
        self.assertTrue(Deck.objects.exists())

    # ---------------- Decks: grade e ações

    def test_decks_grid(self):
        deck = self.import_deck()
        html = self.client.get(reverse("cards:decks")).content.decode()

        # Primeiro item: "+" com as duas opções
        self.assertLess(html.index('class="deck-tile deck-new"'), html.index(reverse("cards:expert_deck", args=[deck.pk])))
        self.assertIn(reverse("cards:builder"), html)
        self.assertIn(reverse("cards:deck_create"), html)
        self.assertIn("Gerar deck", html)
        self.assertIn("Cadastrar deck", html)
        # Item do deck: capa (arte do comandante), nome, cores e datas
        self.assertIn("Vren, the Relentless", html)
        self.assertIn("art_crop" if "art_crop" in html else "https://img/", html)
        self.assertIn("svgs.scryfall.io/card-symbols/U.svg", html)
        self.assertIn("Criado", html)
        # Ações no menu do item
        for name in ("deck_rename", "expert_budget", "deck_clone", "expert_delete"):
            self.assertIn(reverse(f"cards:{name}", args=[deck.pk]), html)
        for fmt in ("ligamagic", "moxfield", "archidekt", "texto"):
            self.assertIn(reverse("cards:deck_export", args=[deck.pk, fmt]), html)

    def test_menu_shows_decks_only(self):
        html = self.client.get(reverse("cards:decks")).content.decode()
        self.assertIn(f'href="{reverse("cards:decks")}"', html)
        self.assertNotIn(">Especialista<", html)
        self.assertNotIn(">Construtor<", html)

    def test_create_empty_deck_then_import(self):
        response = self.client.post(reverse("cards:deck_create"))
        deck = Deck.objects.get()
        self.assertRedirects(response, reverse("cards:expert_deck", args=[deck.pk]),
                             fetch_redirect_response=False)

        page = self.client.get(reverse("cards:expert_deck", args=[deck.pk]))
        self.assertContains(page, "Importar lista")
        self.assertContains(page, reverse("cards:deck_import", args=[deck.pk]))

        self.client.post(reverse("cards:deck_import", args=[deck.pk]), {"decklist": self.DECK})
        deck.refresh_from_db()
        self.assertTrue(deck.cards.filter(is_commander=True, name="Vren, the Relentless").exists())
        # Nome dado ao cadastrar ("Novo deck") fica até o usuário mudar
        self.assertEqual(deck.name, "Novo deck")
        self.assertEqual(Deck.objects.count(), 1)

    def test_replace_list_keeps_deck(self):
        deck = self.import_deck()
        self.client.post(reverse("cards:deck_import", args=[deck.pk]), {"decklist": "Comandante\n1 Vren, the Relentless\n1 Sol Ring"})
        self.assertEqual(sorted(c.name for c in deck.cards.all()), ["Sol Ring", "Vren, the Relentless"])
        self.assertEqual(Deck.objects.count(), 1)

    def test_rename_locks_name_and_empty_restores_commander_name(self):
        deck = self.import_deck()
        url = reverse("cards:deck_rename", args=[deck.pk])

        self.client.post(url, {"name": "Ratos do Vren"})
        deck.refresh_from_db()
        self.assertEqual((deck.name, deck.name_locked), ("Ratos do Vren", True))

        # Trocar comandante não sobrescreve o nome dado
        vren = deck.cards.get(name="Vren, the Relentless")
        self.client.post(reverse("cards:expert_commander", args=[deck.pk]), {"commander": [vren.pk]})
        deck.refresh_from_db()
        self.assertEqual(deck.name, "Ratos do Vren")

        # Vazio: volta ao nome do comandante
        response = self.client.post(url, {"name": "", "next": reverse("cards:decks")})
        deck.refresh_from_db()
        self.assertEqual((deck.name, deck.name_locked), ("Vren, the Relentless", False))
        self.assertRedirects(response, reverse("cards:decks"), fetch_redirect_response=False)

    def test_budget_from_grid_returns_to_grid(self):
        deck = self.import_deck()
        response = self.client.post(reverse("cards:expert_budget", args=[deck.pk]),
                                    {"budget": "50", "next": reverse("cards:decks")})
        self.assertRedirects(response, reverse("cards:decks"), fetch_redirect_response=False)
        deck.refresh_from_db()
        self.assertEqual(deck.budget, Decimal("50"))

    def test_clone(self):
        deck = self.import_deck()
        deck.budget = Decimal("20")
        deck.save()

        response = self.client.post(reverse("cards:deck_clone", args=[deck.pk]))
        copy = Deck.objects.exclude(pk=deck.pk).get()

        self.assertRedirects(response, reverse("cards:expert_deck", args=[copy.pk]), fetch_redirect_response=False)
        self.assertEqual(copy.name, "Cópia de Vren, the Relentless")
        self.assertEqual(copy.budget, Decimal("20"))
        self.assertEqual(
            sorted((c.name, c.quantity, c.is_commander) for c in copy.cards.all()),
            sorted((c.name, c.quantity, c.is_commander) for c in deck.cards.all()),
        )
        # Independentes
        copy.cards.all().delete()
        self.assertTrue(deck.cards.exists())

    def test_cover(self):
        deck = self.import_deck()
        sol = deck.cards.get(name="Sol Ring")
        page = self.client.get(reverse("cards:expert_deck", args=[deck.pk]))
        self.assertContains(page, reverse("cards:deck_cover", args=[deck.pk]))

        self.client.post(reverse("cards:deck_cover", args=[deck.pk]), {"scryfall_id": sol.scryfall_id})
        deck.refresh_from_db()
        self.assertEqual(deck.cover_scryfall_id, sol.scryfall_id)

        # Carta fora do deck: recusa
        self.client.post(reverse("cards:deck_cover", args=[deck.pk]), {"scryfall_id": "outra"})
        deck.refresh_from_db()
        self.assertEqual(deck.cover_scryfall_id, sol.scryfall_id)

    def test_export_formats(self):
        deck = self.import_deck()

        def export(fmt, **params):
            return self.client.get(reverse("cards:deck_export", args=[deck.pk, fmt]), params)

        liga = export("ligamagic").content.decode()
        self.assertTrue(liga.startswith("Comandante\n1 Vren, the Relentless\n\nDeck\n"))
        self.assertIn("10 Island", liga)

        mox = export("moxfield").content.decode()
        self.assertTrue(mox.startswith("Commander\n1 Vren, the Relentless (TST) 1\n"))
        self.assertIn("1 Mana Leak (TST) 1", mox)

        archi = export("archidekt").content.decode()
        self.assertIn("1x Vren, the Relentless (tst) 1 [Commander{top}]", archi)

        text = export("texto").content.decode()
        self.assertEqual(text.splitlines()[0], "1 Vren, the Relentless")
        # Carta não encontrada no Scryfall também sai (não some da lista)
        self.assertIn("1 Carta Inventada", text)

        download = export("moxfield", download="1")
        self.assertIn("attachment", download["Content-Disposition"])
        self.assertEqual(export("pdf").status_code, 404)

        # A exportação reimporta igual (a simulação do Scryfall resolve por nome)
        clone = self.import_deck(liga)
        self.assertEqual(sum(c.quantity for c in clone.cards.all()),
                         sum(c.quantity for c in deck.cards.all()))
        self.assertEqual(clone.cards.get(is_commander=True).name, "Vren, the Relentless")


class LigaMagicNameTests(TestCase):

    def scraper(self, found_names):
        from cards.services.ligamagic import LigaMagicScraper, PriceNotFound

        scraper = LigaMagicScraper()
        tried = []

        def scrape(url):
            tried.append(url)
            for name in found_names:
                if scraper.find_card_url(name) == url:
                    return {"price_brl": 1.0, "foil_price_brl": None, "url": url}
            raise PriceNotFound("variável cards_editions não encontrada")

        scraper.scrape_card_page = scrape
        return scraper, tried

    def test_split_card_uses_full_name(self):
        scraper, tried = self.scraper(["Spite // Malice"])

        result = scraper.get_price("Spite // Malice")

        self.assertEqual(result["url"], scraper.find_card_url("Spite // Malice"))
        self.assertEqual(len(tried), 1)

    def test_double_faced_card_falls_back_to_front(self):
        scraper, tried = self.scraper(["Delver of Secrets"])

        result = scraper.get_price("Delver of Secrets // Insectile Aberration")

        self.assertEqual(result["url"], scraper.find_card_url("Delver of Secrets"))
        self.assertEqual(len(tried), 2)

    def test_not_found_raises(self):
        from cards.services.ligamagic import PriceNotFound

        scraper, tried = self.scraper([])

        with self.assertRaises(PriceNotFound):
            scraper.get_price("Fire // Ice")
        self.assertEqual(len(tried), 2)

    def test_single_name_tries_once(self):
        scraper, tried = self.scraper([])

        with self.assertRaises(Exception):
            scraper.get_price("Sol Ring")
        self.assertEqual(len(tried), 1)


def candidate(name, score, price, roles=(), creature=False, source="edhrec", cmc=2):
    from cards.services.builder import Candidate

    return Candidate(
        data={"id": uid(name), "name": name, "mana_cost": "{B}"}, name=name, oracle_id=f"o-{name}",
        roles=set(roles), score=score, price=Decimal(str(price)), estimated=False, source=source,
        is_creature=creature, cmc=cmc,
    )


def profile(roles=None, creatures=None, curve=None, themes=(), rules=()):
    from cards.services.profile import ROLES, Profile

    return Profile(
        source="teste", sample_size=0,
        role_targets=roles if roles is not None else {key: quota for key, _, _, quota in ROLES},
        creature_target=creatures, curve_targets=curve or {}, themes=list(themes), rules=list(rules),
    )


class BuilderAlgorithmTests(TestCase):

    def test_select_fills_role_quotas_first(self):
        from cards.services.builder import select

        cards = [candidate(f"ramp{i}", 0.1, 1, ["ramp"]) for i in range(10)]
        cards += [candidate(f"good{i}", 0.9, 1) for i in range(60)]

        picked = select(cards, 59, profile(roles={"ramp": 6}))

        self.assertEqual(len(picked), 59)
        self.assertEqual(sum(c.assigned == "ramp" for c in picked), 6)
        self.assertEqual(len({c.oracle_id for c in picked}), 59)

    def test_select_keeps_creatures_near_target(self):
        from cards.services.builder import CREATURE_TOLERANCE, select

        cards = [candidate(f"creature{i}", 0.9, 1, creature=True) for i in range(60)]
        cards += [candidate(f"spell{i}", 0.5, 1) for i in range(60)]

        picked = select(cards, 59, profile(roles={}, creatures=20))

        self.assertEqual(sum(c.is_creature for c in picked), 20 + CREATURE_TOLERANCE)

    def test_select_follows_mana_curve(self):
        from cards.services.builder import CURVE_TOLERANCE, select

        # As de custo 2 têm nota maior, mas o deck típico tem 10 cartas de custo 7+
        cards = [candidate(f"two{i}", 0.9, 1, cmc=2) for i in range(80)]
        cards += [candidate(f"big{i}", 0.3, 1, cmc=8) for i in range(30)]
        curve = {1: 0, 2: 49, 3: 0, 4: 0, 5: 0, 6: 0, 7: 10}

        picked = select(cards, 59, profile(roles={}, curve=curve))

        big = sum(c.bucket == 7 for c in picked)
        self.assertGreaterEqual(big, 10 - CURVE_TOLERANCE)
        self.assertLessEqual(big, 10 + CURVE_TOLERANCE)

    def test_bonuses_follow_priority_and_multiple_functions(self):
        from cards.services.builder import apply_bonuses

        def scored(roles=(), tags=()):
            c = candidate("x", 0.1, 1, roles)
            c.tags = frozenset(tags)
            apply_bonuses(c, {"sac-outlet", "tokens"}, [])
            return c

        removal, draw, sweeper, ramp = (scored([r]) for r in ("removal", "draw", "sweeper", "ramp"))
        self.assertGreater(removal.score, draw.score)
        self.assertGreater(draw.score, sweeper.score)
        self.assertGreater(sweeper.score, ramp.score)

        # Sinergia (tema do deck) vale mais que qualquer papel sozinho
        self.assertGreater(scored(tags=["sac-outlet"]).score, removal.score)

        # Várias funções: remoção + compra + tema > só remoção
        multi = scored(["removal", "draw"], ["tokens"])
        self.assertEqual(multi.functions, 3)
        self.assertGreater(multi.score, removal.score + 0.09)

        # Ramp + remoção conta o bônus de remoção (o melhor), não a soma
        self.assertAlmostEqual(scored(["ramp", "removal"]).score, removal.score + 0.05)

    def test_select_assigns_most_important_role(self):
        from cards.services.builder import select

        both = candidate("removal-and-ramp", 0.9, 1, ["removal", "ramp"])
        cards = [both] + [candidate(f"ramp{i}", 0.5, 1, ["ramp"]) for i in range(5)]
        cards += [candidate(f"filler{i}", 0.1, 1) for i in range(20)]

        select(cards, 10, profile(roles={"removal": 1, "ramp": 2}))

        self.assertEqual(both.assigned, "removal")

    def test_budget_cuts_ramp_before_synergy(self):
        from cards.services.builder import fit_budget

        ramp = candidate("pricey-ramp", 0.9, 10, ["ramp"])
        ramp.assigned = "ramp"
        synergy = candidate("pricey-synergy", 0.9, 10)
        picked = [ramp, synergy]
        pool = picked + [candidate("cheap-ramp", 0.5, 1, ["ramp"]), candidate("cheap-synergy", 0.5, 1)]

        # Só dá para baratear uma: a de ramp perde
        self.assertTrue(fit_budget(picked, pool, Decimal("11")))
        self.assertEqual(sorted(c.name for c in picked), ["cheap-ramp", "pricey-synergy"])

    def test_leftover_goes_to_synergy_first(self):
        from cards.services.builder import use_leftover

        ramp = candidate("ramp", 0.3, 1, ["ramp"])
        ramp.assigned = "ramp"
        synergy = candidate("synergy", 0.3, 1)
        picked = [ramp, synergy]
        pool = picked + [candidate("better-ramp", 0.8, 5, ["ramp"]), candidate("better-synergy", 0.75, 5)]

        # Sobra para uma melhora só: vai para a vaga de sinergia (e não com uma
        # carta só de ramp, mesmo de nota maior)
        use_leftover(picked, pool, Decimal("6"))
        self.assertEqual(sorted(c.name for c in picked), ["better-synergy", "ramp"])

    def test_pure_ramp_only_through_its_quota(self):
        from cards.services.builder import select

        cards = [candidate(f"ramp{i}", 0.9, 1, ["ramp"]) for i in range(10)]
        cards += [candidate(f"filler{i}", 0.2, 1) for i in range(20)]
        # Ramp que também remove: pode entrar como sinergia
        multi = candidate("ramp-and-removal", 0.8, 1, ["ramp", "removal"])
        multi.functions = 2
        cards.append(multi)
        for c in cards:
            c.functions = max(c.functions, len(c.roles))

        picked = select(cards, 15, profile(roles={"ramp": 3}))

        self.assertEqual(sum(c.pure_ramp for c in picked), 3)
        self.assertIn("ramp-and-removal", [c.name for c in picked])

    def test_profile_cuts_ramp_first_when_quotas_dont_fit(self):
        from cards.services.edhrec import Pool
        from cards.services.profile import MAX_ROLE_SHARE, build_profile

        # Deck médio em que toda carta é ramp, compra, remoção e board wipe ao mesmo tempo
        sample = [sf_card(f"All {i}", f"o-all-{i}", f"all-{i}") for i in range(20)]
        for data in sample:
            TagIndex.objects.create(kind="oracle", target_id=data["oracle_id"],
                                    labels="|ramp|draw|removal|sweeper|")

        with mock.patch("cards.services.profile._average_deck_sample", return_value=sample):
            result = build_profile([{"name": "X", "oracle_id": "o-x"}], Pool(found=True), 59)

        cap = int(59 * MAX_ROLE_SHARE)
        t = result.role_targets
        self.assertEqual(sum(t.values()), cap)
        # Corta todos, mas mais quem tem menos prioridade; ninguém zera
        self.assertGreater(t["removal"], t["draw"])
        self.assertGreater(t["draw"], t["sweeper"])
        self.assertGreater(t["sweeper"], t["ramp"])
        self.assertGreaterEqual(t["ramp"], 3)

    def test_trim_roles_realistic(self):
        from cards.services.profile import trim_roles

        # Proporções parecidas com as do deck médio do Rakdos
        t = trim_roles({"ramp": 10, "draw": 15, "removal": 20, "sweeper": 5}, 38)

        self.assertEqual(sum(t.values()), 38)
        self.assertGreaterEqual(t["sweeper"], 1)
        # O ramp perde a maior fração; a remoção, a menor
        lost = {k: 1 - t[k] / v for k, v in {"ramp": 10, "draw": 15, "removal": 20, "sweeper": 5}.items()}
        self.assertEqual(max(lost, key=lost.get), "ramp")
        self.assertEqual(min(lost, key=lost.get), "removal")
        # Sem excesso, não mexe
        self.assertEqual(trim_roles({"ramp": 5, "draw": 5}, 38), {"ramp": 5, "draw": 5})

    def test_apply_overrides(self):
        from cards.services.builder import apply_overrides

        p = profile(roles={"ramp": 0, "draw": 9, "removal": 8, "sweeper": 2}, creatures=20)
        p.skip_ramp = True
        notes = []

        apply_overrides(p, {"synergy": 30, "removal": 10, "draw": 10, "sweeper": 2, "ramp": 7, "creatures": 25},
                        59, notes)

        self.assertEqual(p.role_targets, {"ramp": 7, "draw": 10, "removal": 10, "sweeper": 2})
        self.assertEqual(p.creature_target, 25)
        self.assertFalse(p.skip_ramp)   # ramp pedido explicitamente
        self.assertTrue(notes)

        # Soma maior que as vagas: escala para 59
        p2 = profile(roles={})
        apply_overrides(p2, {"synergy": 59, "removal": 59, "draw": 0, "sweeper": 0, "ramp": 0}, 59, [])
        # Metade das vagas para remoção, metade para sinergia; os outros papéis zeram
        self.assertIn(p2.role_targets["removal"], (29, 30))
        self.assertEqual(sum(p2.role_targets.values()), p2.role_targets["removal"])

    def test_rebalance_curve(self):
        from cards.services.builder import rebalance_curve

        picked = [candidate(f"three{i}", 0.5, 1, cmc=3) for i in range(10)]
        options = [candidate(f"one{i}", 0.3, 1, cmc=1) for i in range(10)]
        options.append(candidate("one-expensive", 0.9, 50, cmc=1))
        curve = {1: 5, 2: 0, 3: 5, 4: 0, 5: 0, 6: 0, 7: 0}

        rebalance_curve(picked, picked + options, profile(roles={}, curve=curve), Decimal("10"))

        counts = {b: sum(c.bucket == b for c in picked) for b in (1, 3)}
        self.assertEqual(counts, {1: 4, 3: 6})   # para dentro da tolerância (±1)
        self.assertNotIn("one-expensive", [c.name for c in picked])  # orçamento
        self.assertLessEqual(sum(c.price for c in picked), Decimal("10"))

    def test_failed_scryfall_query_is_skipped(self):
        from cards.services import builder

        with mock.patch("cards.services.scryfall.ScryfallClient.search_page",
                        side_effect=RuntimeError("Scryfall rate limit atingido.")):
            notes = []
            cache.clear()
            self.assertEqual(builder.scryfall_candidates(["t:creature", "t:instant"], notes), [])

        self.assertIn("2 de 2 buscas no Scryfall falharam", notes[0])

    def test_scryfall_throttle_spaces_requests(self):
        import time

        from cards.services import scryfall

        start = time.monotonic()
        for _ in range(3):
            scryfall._throttle_search()
        self.assertGreaterEqual(time.monotonic() - start, 2 * scryfall.SEARCH_INTERVAL - 0.05)

    def test_fit_budget_prefers_same_mana_value(self):
        from cards.services.builder import fit_budget

        out = candidate("big-expensive", 0.9, 20, cmc=7)
        picked = [out]
        pool = picked + [candidate("cheap-two", 0.6, 1, cmc=2), candidate("cheap-big", 0.5, 1, cmc=7)]

        self.assertTrue(fit_budget(picked, pool, Decimal("2")))
        # Perde mais nota, mas mantém a curva
        self.assertEqual(picked[0].name, "cheap-big")

    def test_profile_curve_and_commander_rule(self):
        from cards.services.edhrec import Pool
        from cards.services.profile import bucket_of, build_profile

        TagIndex.objects.create(kind="oracle", target_id="o-rakdos", labels="|cost-reducer-creature|evasion|")
        pool = Pool(found=True, type_distribution={"Land": 35, "Creature": 33, "Instant": 31},
                    mana_curve={"1": 7, "2": 16, "3": 13, "4": 6, "5": 5, "6": 5, "7": 4, "8": 2, "9": 2,
                                "10": 2, "11": 1, "12": 1})

        with mock.patch("cards.services.profile._average_deck_sample", return_value=[]):
            result = build_profile([{"name": "Rakdos", "oracle_id": "o-rakdos"}], pool, 59)

        self.assertEqual(sum(result.curve_targets.values()), 59)
        # 7+ junta 7, 8, 9, 10, 11 e 12: 12 de 64 → ~11 de 59
        self.assertEqual(result.curve_targets[7], 11)
        # 33 criaturas em 64 mágicas → 30,4 de 59
        self.assertEqual(result.creature_target, 30)
        self.assertEqual(len(result.rules), 1)
        self.assertGreater(result.high_curve_share, 0.3)
        self.assertEqual((bucket_of(0), bucket_of(3.0), bucket_of(12)), (1, 3, 7))

    def test_profile_roles_and_themes_from_average_deck(self):
        from cards.services.edhrec import Pool
        from cards.services.profile import MAX_ROLE_SHARE, build_profile

        sample = [sf_card(f"Avg {i}", f"o-avg-{i}", f"avg-{i}") for i in range(40)]
        for i, data in enumerate(sample):
            labels = {"removal"} if i < 8 else {"ramp"} if i < 14 else set()
            if i % 2:
                labels.add("sac-outlet")
            TagIndex.objects.create(kind="oracle", target_id=data["oracle_id"], labels="|" + "|".join(labels) + "|")
        for i in range(400):
            TagIndex.objects.create(kind="oracle", target_id=f"filler-{i}", labels="|x|")
        TagStat.objects.create(kind="oracle", label="sac-outlet", count=10)
        TagStat.objects.create(kind="oracle", label="removal", count=200)

        pool = Pool(found=True, type_distribution={"Land": 36, "Creature": 20})

        with mock.patch("cards.services.profile._average_deck_sample", return_value=sample):
            result = build_profile([{"name": "X", "oracle_id": "o-x"}], pool, 59)

        self.assertEqual(result.source, "deck médio do EDHREC")
        self.assertEqual(result.role_targets["removal"], round(8 / 40 * 59))
        # 6/40 de ramp (9), menos 2 porque o deck terá 40 terrenos e não 36
        self.assertEqual(result.role_targets["ramp"], round(6 / 40 * 59) - 2)
        self.assertEqual(result.role_targets["sweeper"], 0)
        self.assertLessEqual(sum(result.role_targets.values()), int(59 * MAX_ROLE_SHARE))
        # sac-outlet: metade do deck médio e raro no geral → tema; removal: comum demais
        self.assertEqual([t["tag"] for t in result.themes], ["sac-outlet"])

    def test_tribe_detection(self):
        from cards.services.profile import creature_types, tribes_from_tags

        self.assertEqual(tribes_from_tags({"typal-bird", "tribal-bird", "kindred", "typal-creature"}), ["bird"])
        self.assertEqual(tribes_from_tags({"evasion"}), [])
        self.assertEqual(creature_types("Legendary Creature — Bird Scout"), {"bird", "scout"})
        self.assertEqual(creature_types("Kindred Instant — Bird"), set())
        self.assertEqual(creature_types("Artifact Creature — Rat // Instant"), {"rat"})

    def test_tribe_member_and_payoff(self):
        from cards.services.builder import _is_tribe_member, _mentions_tribe

        bird = {"type_line": "Creature — Bird", "oracle_text": ""}
        elf_lord = {"type_line": "Creature — Human", "oracle_text": "Other Elves you control get +1/+1."}
        self.assertTrue(_is_tribe_member({}, bird, ["bird"]))
        self.assertTrue(_is_tribe_member({"keywords": ["Changeling"]}, {"type_line": "Creature — Shapeshifter"}, ["bird"]))
        self.assertFalse(_is_tribe_member({}, bird, []))
        self.assertTrue(_mentions_tribe(elf_lord, ["elf"]))
        self.assertFalse(_mentions_tribe({"oracle_text": "Birdie"}, ["bird"]))

    def test_tribe_bonus_and_preserving_swaps(self):
        from cards.services.builder import _compatible, apply_bonuses

        member = candidate("bird", 0.3, 1, creature=True)
        member.tribe_member = True
        payoff = candidate("payoff", 0.3, 1)
        payoff.tribe_payoff = True
        other = candidate("other", 0.3, 1, creature=True)
        for c in (member, payoff, other):
            apply_bonuses(c, set(), [])

        self.assertGreater(member.score, payoff.score)
        self.assertGreater(payoff.score, other.score)
        # Criatura da tribo só é trocada por outra da tribo
        self.assertFalse(_compatible(member, other, 2))
        self.assertTrue(_compatible(other, member, 2))

    def test_relentless_copies_and_package(self):
        from cards.services.builder import RELENTLESS_COPIES, add_relentless_copies, select, use_leftover

        hawk = candidate("hawk", 0.3, "0.1", creature=True)
        hawk.tags = frozenset({"relentless"})
        hawk.tribe_member = True
        lonely = candidate("rats", 0.3, "0.1", creature=True)
        lonely.tags = frozenset({"relentless"})   # sem sinergia: uma cópia só
        others = [candidate(f"c{i}", 0.6, 1, cmc=3) for i in range(30)]

        cands = add_relentless_copies([hawk, lonely] + others, set())
        self.assertEqual(sum(c.base_oracle == hawk.oracle_id for c in cands), RELENTLESS_COPIES)
        self.assertEqual(sum(c.base_oracle == lonely.oracle_id for c in cands), 1)

        # O pacote entra inteiro, mesmo com a curva cheia na faixa dele
        picked = select(cands, 20, profile(roles={}, curve={1: 0, 2: 1, 3: 19}))
        copies = [c for c in picked if c.base_oracle == hawk.oracle_id]
        self.assertEqual(len(copies), RELENTLESS_COPIES)
        self.assertTrue(all(c.locked for c in copies))

        # E não sai para "melhorar a nota" com a sobra do orçamento
        better = [candidate(f"great{i}", 2.0, "0.1", creature=True, cmc=2) for i in range(10)]
        use_leftover(picked, cands + better, Decimal("100"))
        self.assertEqual(sum(c.base_oracle == hawk.oracle_id for c in picked), RELENTLESS_COPIES)

    def test_generic_role_tags_are_not_themes(self):
        from cards.services.profile import _themes

        # Deck médio cheio de ramp genérico e de edicts
        sample_tags = [{"ramp", "mana-ramp", "acceleration", "edict"} for _ in range(20)]
        for i in range(400):
            TagIndex.objects.create(kind="oracle", target_id=f"f-{i}", labels="|x|")
        for label in ("ramp", "mana-ramp", "acceleration", "edict"):
            TagStat.objects.create(kind="oracle", label=label, count=10)

        self.assertEqual([t["tag"] for t in _themes(sample_tags)], ["edict"])

    def test_theme_and_rule_bonus_in_score(self):
        from cards.services.builder import COMMANDER_RULES, Candidate

        rule = COMMANDER_RULES[0]
        big = candidate("big", 0.1, 1, creature=True, cmc=8)
        small = candidate("small", 0.1, 1, creature=True, cmc=3)

        self.assertTrue(rule["applies"](big))
        self.assertFalse(rule["applies"](small))
        self.assertGreater(rule["bonus"](big), 0.1)
        self.assertIsInstance(big, Candidate)

    def test_fit_budget_swaps_cheapest_loss(self):
        from cards.services.builder import fit_budget

        picked = [candidate("expensive", 0.9, 50), candidate("ok", 0.5, 2)]
        pool = picked + [candidate("cheap-good", 0.85, 1), candidate("cheap-bad", 0.1, "0.5")]

        self.assertTrue(fit_budget(picked, pool, Decimal("5")))
        self.assertEqual(sorted(c.name for c in picked), ["cheap-good", "ok"])

    def test_fit_budget_keeps_role(self):
        from cards.services.builder import fit_budget

        ramp = candidate("pricey-ramp", 0.9, 30, ["ramp"])
        ramp.assigned = "ramp"
        picked = [ramp]
        pool = picked + [candidate("cheap-not-ramp", 0.8, 1), candidate("cheap-ramp", 0.2, 1, ["ramp"])]

        self.assertTrue(fit_budget(picked, pool, Decimal("2")))
        self.assertEqual(picked[0].name, "cheap-ramp")
        self.assertEqual(picked[0].assigned, "ramp")

    def test_fit_budget_last_resort_changes_creature_flag(self):
        from cards.services.builder import fit_budget

        # Remoção-criatura cara, sem remoção-criatura mais barata: aceita uma
        # mágica de remoção se as criaturas continuarem na tolerância
        out = candidate("creature-removal", 0.9, 5, ["removal"], creature=True)
        out.assigned = "removal"
        others = [candidate(f"c{i}", 0.5, 0.1, creature=True) for i in range(10)]
        picked = [out] + others
        spell = candidate("spell-removal", 0.4, 1, ["removal"])
        pool = picked + [spell]

        self.assertTrue(fit_budget(picked, pool, Decimal("3"), creature_target=11))
        self.assertIn("spell-removal", [c.name for c in picked])
        self.assertEqual(spell.assigned, "removal")

        # Fora da tolerância (alvo 14, ficariam 10): não troca
        picked2 = [candidate("cr2", 0.9, 5, ["removal"], creature=True)] + [
            candidate(f"d{i}", 0.5, 0.1, creature=True) for i in range(10)]
        picked2[0].assigned = "removal"
        self.assertFalse(fit_budget(picked2, picked2 + [candidate("sp2", 0.4, 1, ["removal"])],
                                    Decimal("3"), creature_target=14))

    def test_cheapest_queries_for_very_low_budget(self):
        from cards.services.builder import candidate_queries

        p = profile(creatures=20)
        normal = candidate_queries("UB", Decimal("1.00"), p)
        cheap = candidate_queries("UB", Decimal("0.60"), p, cheapest=True)

        self.assertTrue(all(order == "edhrec" for _, order in normal))
        usd = [q for q, order in cheap if order == "usd"]
        self.assertTrue(usd)
        self.assertTrue(all("usd<=0.60" in q and "id<=ub" in q for q in usd))
        self.assertEqual(len(cheap), len(normal) + len(usd))

    def test_rebalance_never_raises_cost_when_budget_failed(self):
        from cards.services.builder import rebalance_curve

        picked = [candidate(f"three{i}", 0.5, 1, cmc=3) for i in range(10)]
        options = [candidate(f"one{i}", 0.9, 5, cmc=1) for i in range(10)]
        before = sum(c.price for c in picked)

        # Teto = total atual: as opções de custo 1 são mais caras e não entram
        rebalance_curve(picked, picked + options, profile(roles={}, curve={1: 5, 3: 5}), before)
        self.assertEqual(sum(c.price for c in picked), before)

    def test_release_ramp_quota_when_budget_does_not_fit(self):
        from cards.services.builder import fit_with_release

        # Duas rampas caras e nenhuma rampa mais barata disponível; há cartas
        # baratas de outro papel
        ramps = [candidate(f"ramp{i}", 0.5, 4, ["ramp"]) for i in range(2)]
        for c in ramps:
            c.assigned = "ramp"
            c.functions = 2   # não é "ramp puro" (pode ficar como sinergia)
        removal = candidate("removal", 0.9, 1, ["removal"])
        removal.assigned = "removal"
        picked = ramps + [removal]
        pool = picked + [candidate(f"cheap{i}", 0.3, "0.5") for i in range(3)]

        notes = []
        self.assertTrue(fit_with_release(picked, pool, Decimal("3"), profile(), notes))

        self.assertLessEqual(sum(c.price for c in picked), Decimal("3"))
        self.assertIn("removal", [c.name for c in picked])  # remoção não foi liberada
        self.assertIn("a cota de ramp foi liberada", notes[0])

    def test_fit_budget_reports_impossible(self):
        from cards.services.builder import fit_budget

        picked = [candidate("a", 0.5, 10)]
        self.assertFalse(fit_budget(picked, picked, Decimal("1")))

    def test_use_leftover_upgrades(self):
        from cards.services.builder import use_leftover

        picked = [candidate("meh", 0.1, 1)]
        pool = picked + [candidate("great", 0.9, 4), candidate("too-expensive", 1.0, 100)]

        use_leftover(picked, pool, Decimal("5"))
        self.assertEqual(picked[0].name, "great")

    def test_basic_land_split(self):
        from cards.services.builder import pip_weights, split_basic_lands

        cards = [{"mana_cost": "{B}{B}"}, {"mana_cost": "{U}"}, {"mana_cost": "{U/B}"}]
        weights = pip_weights(cards, [{"mana_cost": "{U}{B}"}], "UB")
        # U: 1 + 0,5 (híbrido) + 3 (comandante pesa 3×); B: 2 + 0,5 + 3
        self.assertEqual(weights, {"U": 4.5, "B": 5.5})

        lands = split_basic_lands({"U": 1, "B": 3})
        self.assertEqual(sum(lands.values()), 40)
        self.assertEqual(lands, {"U": 11, "B": 29})

        self.assertEqual(split_basic_lands({}), {"C": 40})
        self.assertEqual(sum(split_basic_lands({"W": 0, "U": 0, "B": 1}).values()), 40)

    def test_partner_rules(self):
        from cards.services.builder import _pairs

        self.assertTrue(_pairs({"oracle_text": "Partner"}, {"oracle_text": ""}))
        self.assertTrue(_pairs({"oracle_text": "Choose a Background"},
                               {"type_line": "Legendary Enchantment — Background"}))
        self.assertFalse(_pairs({"oracle_text": "Flying"}, {"oracle_text": "Trample"}))


class BuilderTests(OfflineMixin, TestCase):

    def setUp(self):
        super().setUp()

        self.commander = sf_card("Vren, the Relentless", "o-vren", "vren", usd="1.00",
                                 type_line="Legendary Creature — Rat Rogue", colors=["U", "B"],
                                 color_identity=["U", "B"], mana_cost="{2}{U}{B}",
                                 legalities={"commander": "legal"}, games=["paper"])

        def spell(i, usd, type_line="Instant", identity=("B",), cost="{1}{B}"):
            return sf_card(f"Spell {i}", f"o-spell-{i}", f"spell-{i}", usd=usd, type_line=type_line,
                           color_identity=list(identity), mana_cost=cost,
                           legalities={"commander": "legal"}, games=["paper"], edhrec_rank=i * 10)

        # 40 do EDHREC (caras) + 80 baratas do Scryfall
        self.pool_cards = [spell(i, "2.00", cost="{U}{U}" if i % 2 else "{B}") for i in range(40)]
        self.cheap = [spell(100 + i, "0.02") for i in range(80)]
        self.cheap.append(spell(999, "0.01", identity=("R",)))           # fora da identidade
        self.cheap.append(spell(998, "0.01", type_line="Land"))           # terreno
        self.cheap.append(sf_card("Banned", "o-banned", "banned", usd="0.01", type_line="Instant",
                                  color_identity=["B"], legalities={"commander": "banned"}, games=["paper"]))

        for i, data in enumerate(self.pool_cards[:10]):
            TagIndex.objects.create(kind="oracle", target_id=data["oracle_id"], labels="|removal|")
        for data in self.cheap[:30]:
            TagIndex.objects.create(kind="oracle", target_id=data["oracle_id"], labels="|ramp|draw|")
        SyncState.objects.create(key="tags:oracle", updated_at=timezone.now())

        basics = {name: sf_card(name, f"o-{name}", name, usd="0.30", type_line=f"Basic Land — {name}",
                                colors=[], color_identity=[c]) for name, c in [("Island", "U"), ("Swamp", "B")]}
        self.by_id = {d["id"]: d for d in self.pool_cards + self.cheap + list(basics.values()) + [self.commander]}

        def patch(target, **kwargs):
            patcher = mock.patch(target, autospec=True, **kwargs)
            self.addCleanup(patcher.stop)
            return patcher.start()

        patch("cards.services.scryfall.ScryfallClient.named_fuzzy",
              side_effect=lambda client, name: self.commander if "vren" in name.lower() else None)
        patch("cards.services.scryfall.ScryfallClient.collection",
              side_effect=lambda client, ids: [
                  self.by_id.get(i["id"]) if "id" in i else basics.get(i.get("name")) for i in ids
              ])
        self.search = patch("cards.services.scryfall.ScryfallClient.search_page",
                            return_value={"data": self.cheap, "total_cards": len(self.cheap), "warnings": []})

        page = {
            "container": {"json_dict": {"card": {"num_decks": 1000}, "cardlists": [
                {"header": "Instants", "tag": "instants", "cardviews": [
                    {"id": d["id"], "name": d["name"], "num_decks": 500 - i, "potential_decks": 1000,
                     "synergy": 0.2} for i, d in enumerate(self.pool_cards)
                ]},
            ]}},
            "panels": {"piechart": {"content": [{"label": "Land", "value": 36}, {"label": "Creature", "value": 20},
                                                {"label": "Instant", "value": 44}]}},
        }
        edhrec = mock.patch("cards.services.edhrec.requests.get")
        self.addCleanup(edhrec.stop)
        edhrec.start().return_value = mock.Mock(status_code=200, ok=True, json=lambda: page)

        patcher = mock.patch("cards.services.builder.price_queue")
        self.builder_queue = patcher.start()
        self.addCleanup(patcher.stop)

    def build(self, budget=""):
        return self.client.post(reverse("cards:builder"), {"commander": "Vren", "budget": budget})

    def test_builds_100_cards_without_budget(self):
        response = self.build()
        deck = Deck.objects.get()
        self.assertRedirects(response, reverse("cards:expert_deck", args=[deck.pk]) + "?construido=1",
                             fetch_redirect_response=False)

        cards = list(deck.cards.all())
        self.assertEqual(sum(c.quantity for c in cards), 100)
        self.assertEqual([c.name for c in cards if c.is_commander], ["Vren, the Relentless"])

        basics = {c.name: c.quantity for c in cards if c.category == "Terreno básico"}
        self.assertEqual(sum(basics.values()), 40)
        self.assertEqual(set(basics), {"Island", "Swamp"})

        names = {c.name for c in cards}
        self.assertNotIn("Spell 999", names)   # identidade
        self.assertNotIn("Spell 998", names)   # terreno
        self.assertNotIn("Banned", names)
        # Sem orçamento: todas as do EDHREC entram (nota maior)
        self.assertTrue(all(d["name"] in names for d in self.pool_cards))

        report = deck.build_report
        self.assertEqual(report["slots"], 59)
        self.assertEqual(report["from_edhrec"], 40)
        self.assertEqual(deck.name, "Vren, the Relentless")

    def test_respects_budget(self):
        self.build("10")
        deck = Deck.objects.get()
        report = deck.build_report

        self.assertTrue(report["fits"])
        self.assertLessEqual(Decimal(report["spent"]), Decimal("10"))
        self.assertEqual(sum(c.quantity for c in deck.cards.all()), 100)
        self.assertEqual(deck.budget, Decimal("10"))

        # Cada carta do EDHREC custa ~R$ 10 (US$ 2 × 5): nenhuma cabe
        self.assertEqual(report["from_edhrec"], 0)
        # Scryfall consultado com teto por carta (30% do orçamento = R$ 3 = US$ 0.60)
        spell_queries = [c.args[1] for c in self.search.call_args_list if "-t:basic" not in c.args[1]]
        self.assertTrue(spell_queries)
        self.assertTrue(all("usd<=0.60" in q for q in spell_queries))

        # Página do deck: básicos não pesam no total
        response = self.client.get(reverse("cards:expert_deck", args=[deck.pk]))
        view = response.context["view"]
        self.assertLessEqual(view.budget_total, Decimal("10"))
        self.assertContains(response, "Montado pelo Construtor")
        self.assertContains(response, "role-label")

    def test_profile_reported(self):
        self.build()
        report = Deck.objects.get().build_report

        # Sem deck médio no EDHREC (simulado): perfil pelas mais jogadas do pool
        self.assertEqual(report["profile_source"], "cartas mais jogadas no EDHREC")
        roles = {r["label"]: r for r in report["roles"]}
        # 10 das 40 mais jogadas são remoção → 15 de 59
        self.assertEqual(roles["Remoção"]["target"], 15)
        self.assertLessEqual(roles["Remoção"]["count"], 15)
        self.assertEqual(len(report["curve"]), 7)
        self.assertEqual(sum(c["count"] for c in report["curve"]), 59)

        response = self.client.get(reverse("cards:expert_deck", args=[Deck.objects.get().pk]))
        self.assertContains(response, "Curva de mana")

    def test_validation_errors(self):
        response = self.client.post(reverse("cards:builder"), {"commander": "Inexistente"})
        self.assertContains(response, "Carta não encontrada")
        self.assertFalse(Deck.objects.exists())

    def test_rebuild_keeps_deck(self):
        self.build()
        deck = Deck.objects.get()
        deck.budget = Decimal("6")
        deck.save()

        self.client.post(reverse("cards:builder_rebuild", args=[deck.pk]))

        deck.refresh_from_db()
        self.assertEqual(Deck.objects.count(), 1)
        self.assertEqual(Decimal(deck.build_report["budget"]), Decimal("6"))
        self.assertTrue(deck.build_report["fits"])
        self.assertLessEqual(Decimal(deck.build_report["spent"]), Decimal("6"))
        self.assertEqual(sum(c.quantity for c in deck.cards.all()), 100)

    def test_impossible_budget_is_reported(self):
        # 59 cartas a pelo menos R$ 0,10 = R$ 5,90
        self.build("5")
        deck = Deck.objects.get()

        self.assertFalse(deck.build_report["fits"])
        self.assertIn("Não foi possível chegar ao orçamento", " ".join(deck.build_report["notes"]))

        response = self.client.get(reverse("cards:expert_deck", args=[deck.pk]))
        self.assertContains(response, "não coube no orçamento")

    def tag_ramp_cards(self):
        """Uma pedra de mana e uma carta só de ramp, baratas, entre as candidatas."""

        rock, pure = self.cheap[30], self.cheap[31]
        TagIndex.objects.create(kind="oracle", target_id=rock["oracle_id"], labels="|mana-rock|ramp|")
        TagIndex.objects.create(kind="oracle", target_id=pure["oracle_id"], labels="|ramp|")
        return rock["name"], pure["name"]

    def test_low_budget_skips_ramp_and_mana_rocks(self):
        rock, pure = self.tag_ramp_cards()

        self.build("10")
        deck = Deck.objects.get()
        names = {c.name for c in deck.cards.all()}
        report = deck.build_report

        self.assertNotIn(rock, names)
        self.assertNotIn(pure, names)
        roles = {r["label"]: r for r in report["roles"]}
        self.assertEqual((roles["Ramp"]["count"], roles["Ramp"]["target"]), (0, 0))
        self.assertFalse(any(c.category == "Ramp" for c in deck.cards.all()))
        self.assertIn("sem ramp nem pedras de mana", " ".join(report["notes"]))
        # Nenhuma busca de ramp no Scryfall
        self.assertFalse(any("otag:mana-rock" in call.args[1] for call in self.search.call_args_list))
        # Cartas de ramp + compra continuam valendo pela compra
        self.assertTrue(any(c["name"] in names for c in self.cheap[:30]))

    def test_low_budget_land_commander_keeps_land_ramp_but_not_rocks(self):
        rock, _ = self.tag_ramp_cards()
        TagIndex.objects.create(kind="oracle", target_id="o-vren", labels="|landfall|lands-matter|")

        self.build("10")
        deck = Deck.objects.get()
        report = deck.build_report

        roles = {r["label"]: r for r in report["roles"]}
        self.assertGreater(roles["Ramp"]["target"], 0)
        notes = " ".join(report["notes"])
        self.assertIn("o ramp de terreno foi mantido", notes)
        self.assertIn("landfall", notes)
        self.assertNotIn(rock, {c.name for c in deck.cards.all()})

    def tag_dorks_and_land_ramp(self):
        dork, land_ramp, cobra = self.cheap[40], self.cheap[41], self.cheap[42]
        TagIndex.objects.create(kind="oracle", target_id=dork["oracle_id"], labels="|mana-dork|mana-producer|ramp|")
        TagIndex.objects.create(kind="oracle", target_id=land_ramp["oracle_id"], labels="|land-ramp|ramp|rampant-growth|")
        TagIndex.objects.create(kind="oracle", target_id=cobra["oracle_id"], labels="|landfall|mana-dork|ramp|")
        return dork["name"], land_ramp["name"], cobra["name"]

    def test_land_deck_uses_land_ramp_not_mana_dorks(self):
        from cards.services import builder

        dork, land_ramp, cobra = self.tag_dorks_and_land_ramp()
        TagIndex.objects.create(kind="oracle", target_id="o-vren", labels="|landfall|lands-matter|")

        plan = builder.plan_deck(["Vren"], None, Decimal("5"))
        by_name = {c.name: c for c in plan.picked}
        candidates = {c.name: c for c in builder.build_candidates(
            plan.commanders, plan.identity, builder.commander_pool(["Vren, the Relentless"]),
            builder.build_profile(plan.commanders, builder.commander_pool(["Vren, the Relentless"]), 59),
            Decimal("5"), None, 59, [])}

        self.assertNotIn(dork, candidates)                 # dork puro: fora
        self.assertIn("ramp", candidates[land_ramp].roles)  # ramp de terreno: é ramp
        self.assertIn(cobra, candidates)                    # dork com landfall: sinergia
        self.assertNotIn("ramp", candidates[cobra].roles)
        self.assertNotIn(dork, by_name)
        # A busca de ramp no Scryfall pede só ramp de terreno
        ramp_queries = [c.args[1] for c in self.search.call_args_list if "otag:land-ramp" in c.args[1]]
        self.assertTrue(ramp_queries)
        self.assertTrue(all("otag:mana-dork" not in q for q in ramp_queries))

    def test_non_land_deck_still_accepts_mana_dorks(self):
        from cards.services import builder

        dork, _, _ = self.tag_dorks_and_land_ramp()
        pool = builder.commander_pool(["Vren, the Relentless"])
        commanders = builder.resolve_commanders(["Vren"])
        profile_ = builder.build_profile(commanders, pool, 59)

        candidates = {c.name: c for c in builder.build_candidates(
            commanders, "UB", pool, profile_, Decimal("5"), None, 59, [])}
        self.assertIn("ramp", candidates[dork].roles)

    def test_budget_above_limit_keeps_ramp(self):
        rock, _ = self.tag_ramp_cards()

        self.build("10.01")
        report = Deck.objects.get().build_report

        self.assertNotIn("sem ramp", " ".join(report["notes"]))
        self.assertTrue(any("otag:mana-rock" in call.args[1] for call in self.search.call_args_list))

    def test_partial_commander_name_uses_autocomplete(self):
        from cards.services import builder

        # named_fuzzy (simulado no setUp) não acha "Vre"; o autocompletar sugere o nome completo
        with mock.patch("cards.services.scryfall.ScryfallClient.autocomplete", autospec=True,
                        return_value=["Vren, the Relentless"]) as autocomplete:
            commanders = builder.resolve_commanders(["Vre"])

        self.assertEqual(commanders[0]["name"], "Vren, the Relentless")
        autocomplete.assert_called_once()

    def test_tribal_deck_with_relentless_copies(self):
        from cards.services.builder import RELENTLESS_COPIES

        # Vren é marcado como tribal de rato; uma carta "relentless" que é rato
        TagIndex.objects.create(kind="oracle", target_id="o-vren", labels="|typal-rat|tribal-rat|")
        rat = sf_card("Relentless Rats", "o-rrats", "rrats", usd="0.02", type_line="Creature — Rat",
                      color_identity=["B"], legalities={"commander": "legal"}, games=["paper"])
        TagIndex.objects.create(kind="oracle", target_id="o-rrats", labels="|relentless|")
        self.cheap.append(rat)
        self.by_id[rat["id"]] = rat

        self.build("10")
        deck = Deck.objects.get()
        row = deck.cards.get(name="Relentless Rats")
        report = deck.build_report

        self.assertEqual(row.quantity, RELENTLESS_COPIES)
        self.assertEqual(sum(c.quantity for c in deck.cards.all()), 100)
        self.assertEqual(report["tribes"], ["rat"])
        self.assertEqual(report["relentless"], [{"name": "Relentless Rats", "copies": RELENTLESS_COPIES}])
        self.assertIn(f"{RELENTLESS_COPIES} Relentless Rats", deck.raw_text)
        self.assertTrue(any("t:rat" in c.args[1] for c in self.search.call_args_list))

        page = self.client.get(reverse("cards:expert_deck", args=[deck.pk]))
        self.assertContains(page, f"{RELENTLESS_COPIES} cópias de Relentless Rats")
        self.assertContains(page, "Deck tribal (rat)")

    def land(self, name, produced, usd="0.05", text="", rank=100):
        return sf_card(name, f"o-{name}", name, usd=usd, type_line="Land", colors=[], color_identity=[],
                       produced_mana=produced, oracle_text=text, edhrec_rank=rank,
                       legalities={"commander": "legal"}, games=["paper"])

    def test_leftover_budget_buys_dual_lands(self):
        lands = [
            self.land("Command Tower", ["W", "U", "B", "R", "G"], rank=2),
            self.land("Evolving Wilds", None, text="{T}, Sacrifice this land: Search your library for a basic land card.", rank=19),
            self.land("Dismal Backwater", ["U", "B"], usd="0.03", rank=1000),
            self.land("Mono Land", ["B"], rank=1),                        # uma cor só: não
            self.land("Expensive Dual", ["U", "B"], usd="50.00", rank=3),  # não cabe na sobra
        ]
        spells = self.cheap

        def search(client, query, *args):
            data = lands if "-t:basic" in query else spells
            return {"data": data, "total_cards": len(data), "warnings": []}

        self.search.side_effect = search
        for data in lands:
            self.by_id[data["id"]] = data

        self.build("30")
        deck = Deck.objects.get()
        report = deck.build_report
        names = {c.name: c for c in deck.cards.all()}

        self.assertEqual([l["name"] for l in report["fixing_lands"]],
                         ["Command Tower", "Evolving Wilds", "Dismal Backwater"])
        self.assertNotIn("Mono Land", names)
        self.assertNotIn("Expensive Dual", names)
        self.assertEqual(names["Command Tower"].category, "Terreno")

        # Os três substituem básicos: continuam 40 terrenos e 100 cartas
        basics = sum(c.quantity for c in deck.cards.all() if c.category == "Terreno básico")
        self.assertEqual(basics, 37)
        self.assertEqual(sum(c.quantity for c in deck.cards.all()), 100)

        # Terrenos contam no orçamento, que continua respeitado
        self.assertLessEqual(Decimal(report["spent"]), Decimal("30"))
        self.assertEqual(Decimal(report["spent"]),
                         Decimal(report["spells_spent"]) + Decimal(report["lands_spent"]))

        page = self.client.get(reverse("cards:expert_deck", args=[deck.pk]))
        self.assertContains(page, "Command Tower")

    def test_low_budget_prefers_many_cheap_lands(self):
        from cards.services.builder import pick_fixing_lands

        lands = [self.land("Popular Expensive", ["U", "B"], usd="0.20", rank=1)]  # R$ 1,00
        lands += [self.land(f"Tapland {i}", ["U", "B"], usd="0.02", rank=100 + i) for i in range(12)]
        self.search.side_effect = lambda client, query, *a: {"data": lands, "total_cards": len(lands), "warnings": []}

        # Reserva de R$ 1,50 para até 10 terrenos: no máximo R$ 0,45 cada
        picked = pick_fixing_lands("UB", Decimal("1.50"), Decimal("5"), [])

        self.assertEqual(len(picked), 10)
        self.assertNotIn("Popular Expensive", [l.data["name"] for l in picked])
        self.assertLessEqual(sum(l.price for l in picked), Decimal("1.50"))

    def test_mana_base_reserved_even_when_spells_would_use_everything(self):
        lands = [self.land(f"Tapland {i}", ["U", "B"], usd="0.02", rank=100 + i) for i in range(12)]

        def search(client, query, *args):
            data = lands if "-t:basic" in query else self.cheap
            return {"data": data, "total_cards": len(data), "warnings": []}

        self.search.side_effect = search
        for data in lands:
            self.by_id[data["id"]] = data

        self.build("10")
        report = Deck.objects.get().build_report

        self.assertEqual(len(report["fixing_lands"]), 10)
        self.assertTrue(report["fits"])
        self.assertLessEqual(Decimal(report["spent"]), Decimal("10"))

    def test_no_dual_lands_for_mono_color_or_without_leftover(self):
        from cards.services.builder import is_fixing_land, pick_fixing_lands

        self.assertEqual(pick_fixing_lands("B", None, Decimal("5"), []), [])
        self.assertEqual(pick_fixing_lands("UB", Decimal("0"), Decimal("5"), []), [])

        self.assertTrue(is_fixing_land(self.land("Dual", ["U", "B"]), "UB"))
        self.assertFalse(is_fixing_land(self.land("Wrong colors", ["R", "G"]), "UB"))
        self.assertTrue(is_fixing_land(self.land("Wilds", None, text="Search your library for a basic land card"), "UB"))

    def test_builder_page(self):
        response = self.client.get(reverse("cards:builder"))
        self.assertContains(response, "Construir deck")
        self.assertContains(response, 'name="partner"')

    def test_partner_field_hidden_behind_plus_button(self):
        html = self.client.get(reverse("cards:builder")).content.decode()

        self.assertIn('id="toggle-partner"', html)
        self.assertRegex(html, r'id="toggle-partner"[^>]*aria-expanded="false"[^>]*>\+</button>')
        self.assertIn('<label id="partner-field" hidden>', html)

        # Voltou com o segundo comandante preenchido (erro de validação): aberto
        response = self.client.post(reverse("cards:builder"), {"commander": "Inexistente", "partner": "Outra"})
        html = response.content.decode()
        self.assertIn('<label id="partner-field" >', html)
        self.assertRegex(html, r'id="toggle-partner"[^>]*aria-expanded="true"[^>]*>−</button>')

    def test_report_collapsed_with_radar(self):
        self.build()
        deck = Deck.objects.get()

        html = self.client.get(reverse("cards:expert_deck", args=[deck.pk]) + "?construido=1").content.decode()
        self.assertIn('<details class="panel build-report">', html)   # sempre fechado
        self.assertIn('id="radar-chart"', html)
        self.assertIn('id="radar-data"', html)
        for key in ("synergy", "removal", "draw", "sweeper", "ramp", "creatures"):
            self.assertIn(f'name="target_{key}"', html)

        radar = self.client.get(reverse("cards:expert_deck", args=[deck.pk])).context["radar"]
        self.assertEqual([a["key"] for a in radar["axes"]],
                         ["synergy", "removal", "draw", "sweeper", "ramp", "creatures"])
        current = radar["series"]["current"]
        self.assertEqual(sum(current[k] for k in ("synergy", "removal", "draw", "sweeper", "ramp")), 59)
        self.assertFalse(radar["custom"])

    def test_rebuild_with_radar_targets(self):
        self.build()
        deck = Deck.objects.get()
        url = reverse("cards:builder_rebuild", args=[deck.pk])

        # Soma 50 (≠ 59): ajustado proporcionalmente
        targets = {"target_synergy": "20", "target_removal": "15", "target_draw": "10",
                   "target_sweeper": "0", "target_ramp": "5", "target_creatures": "12"}
        self.client.post(url, targets)
        deck.refresh_from_db()
        report = deck.build_report

        self.assertEqual(report["overrides"], {"synergy": 20, "removal": 15, "draw": 10, "sweeper": 0,
                                               "ramp": 5, "creatures": 12})
        from cards.services.builder import _scale_counts

        expected = _scale_counts({"synergy": 20, "removal": 15, "draw": 10, "sweeper": 0, "ramp": 5}, 59)
        roles = {r["label"]: r["target"] for r in report["roles"]}
        self.assertEqual(roles, {"Remoção": expected["removal"], "Compra de cartas": expected["draw"],
                                 "Board wipe": 0, "Ramp": expected["ramp"]})
        self.assertEqual(sum(roles.values()) + expected["synergy"], 59)
        self.assertEqual(report["creature_target"], 12)
        self.assertIn("ajustadas por você", " ".join(report["notes"]))
        self.assertEqual(sum(c.quantity for c in deck.cards.all()), 100)

        page = self.client.get(reverse("cards:expert_deck", args=[deck.pk]))
        self.assertTrue(page.context["radar"]["custom"])
        self.assertEqual(page.context["radar"]["series"]["target"]["removal"], 15)

        # Reconstruir comum mantém os alvos
        self.client.post(url)
        deck.refresh_from_db()
        self.assertEqual(deck.build_report["overrides"]["removal"], 15)

        # Restaurar perfil
        self.client.post(url, {"reset": "1", **targets})
        deck.refresh_from_db()
        self.assertIsNone(deck.build_report["overrides"])
        self.assertNotIn("ajustadas por você", " ".join(deck.build_report["notes"]))

    def test_built_deck_in_grid_and_rebuild_keeps_custom_name(self):
        self.build()
        deck = Deck.objects.get()

        grid = self.client.get(reverse("cards:decks"))
        self.assertContains(grid, reverse("cards:expert_delete", args=[deck.pk]))
        self.assertContains(grid, "gerado")
        self.assertContains(grid, reverse("cards:builder_rebuild", args=[deck.pk]))

        self.client.post(reverse("cards:deck_rename", args=[deck.pk]), {"name": "Meu Vren"})
        self.client.post(reverse("cards:builder_rebuild", args=[deck.pk]))
        deck.refresh_from_db()
        self.assertEqual(deck.name, "Meu Vren")

        response = self.client.post(reverse("cards:expert_delete", args=[deck.pk]), follow=True)
        self.assertRedirects(response, reverse("cards:decks"))
        self.assertFalse(Deck.objects.exists())


class PriceFetchTests(TestCase):

    def test_fetch_for_card_outside_collection(self):
        scraper = mock.Mock()
        scraper.get_price.return_value = {"price_brl": 1.2, "foil_price_brl": None, "url": "u"}

        fetch_price("Opt", scraper=scraper)

        self.assertEqual(BrlPrice.objects.get(name_normalized="opt").price_brl, Decimal("1.20"))

    def test_fetch_for_collection_card_updates_card(self):
        Card.objects.create(name="Delver of Secrets", name_normalized="delver of secrets")
        scraper = mock.Mock()
        scraper.get_price.return_value = {"price_brl": 0.9, "foil_price_brl": None, "url": "u"}

        fetch_price("Delver of Secrets // Insectile Aberration", scraper=scraper, updater=CardUpdater())

        card = Card.objects.get()
        self.assertEqual(card.price_brl, Decimal("0.90"))
        self.assertFalse(BrlPrice.objects.exists())
