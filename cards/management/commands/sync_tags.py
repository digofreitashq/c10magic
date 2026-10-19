import time

from django.conf import settings
from django.core.management.base import BaseCommand

from cards.services.tags import sync_all


class Command(BaseCommand):
    help = (
        "Baixa as tags do Scryfall Tagger (função e arte). Por padrão só "
        f"quando a última sincronização tiver mais de {settings.CARD_REFRESH_DAYS} dias."
    )

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="Baixa mesmo se estiver em dia.")

    def handle(self, *args, **options):
        start = time.monotonic()

        done = sync_all(force=options["force"])

        if not done:
            self.stdout.write("Tags em dia.")
            return

        for kind, count in done.items():
            self.stdout.write(f"{kind}: {count} itens com tags")

        self.stdout.write(self.style.SUCCESS(f"Concluído em {time.monotonic() - start:.0f}s."))
