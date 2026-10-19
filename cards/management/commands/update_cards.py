from django.conf import settings
from django.core.management.base import BaseCommand

from cards.models import Card
from cards.services.updater import CardUpdater, stale_cards


class Command(BaseCommand):
    help = (
        "Carrega dados do Scryfall e preços da LigaMagic. Por padrão só "
        f"atualiza cartas nunca carregadas ou com mais de "
        f"{settings.CARD_REFRESH_DAYS} dias."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--force", action="store_true",
            help="Ignora a regra de uma semana e atualiza tudo.",
        )
        parser.add_argument("--limit", type=int, help="Máximo de cartas nesta execução.")
        parser.add_argument("--name", help="Só cartas cujo nome contém este texto.")

    def handle(self, *args, **options):
        force = options["force"]

        cards = Card.objects.order_by("name_normalized") if force else stale_cards()

        if options["name"]:
            cards = cards.filter(name__icontains=options["name"])

        if options["limit"]:
            cards = cards[: options["limit"]]

        cards = list(cards)
        updater = CardUpdater()

        self.stdout.write(f"{len(cards)} carta(s) para atualizar.")

        for index, card in enumerate(cards, 1):
            done = updater.refresh(card, force=force)

            parts = []
            if done["scryfall"]:
                parts.append("Scryfall " + ("erro: " + card.scryfall_error if card.scryfall_error else "OK"))
            if done["ligamagic"]:
                parts.append(
                    "LigaMagic "
                    + (f"R$ {card.price_brl:.2f}" if card.price_brl is not None else card.ligamagic_error)
                )

            self.stdout.write(f"[{index}/{len(cards)}] {card.name}: {' | '.join(parts) or 'em dia'}")

        self.stdout.write(self.style.SUCCESS("Concluído."))
