from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from cards.services.importer import decode_upload
from cards.services.updater import import_csv_content


class Command(BaseCommand):
    help = "Importa CSVs exportados da LigaMagic (arquivos ou pastas)."

    def add_arguments(self, parser):
        parser.add_argument("paths", nargs="+", help="Arquivos .csv ou pastas")

    def handle(self, *args, **options):
        files = []

        for raw in options["paths"]:
            path = Path(raw)

            if path.is_dir():
                files += sorted(p for p in path.rglob("*") if p.is_file())
            elif path.is_file():
                files.append(path)
            else:
                raise CommandError(f"Não encontrado: {path}")

        rows = created = 0

        for path in files:
            try:
                result = import_csv_content(decode_upload(path.read_bytes()))
            except ValueError as exc:
                self.stderr.write(f"{path.name}: {exc}")
                continue

            rows += result["rows"]
            created += result["created"]

            self.stdout.write(f"{path.name}: {result['rows']} linhas")

        self.stdout.write(self.style.SUCCESS(
            f"{len(files)} arquivo(s), {rows} linhas, {created} cartas novas."
        ))
        self.stdout.write("Rode 'python manage.py update_cards' para carregar os dados.")
