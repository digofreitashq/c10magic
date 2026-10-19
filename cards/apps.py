from django.apps import AppConfig


class CardsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'cards'
    verbose_name = 'Cartas'

    def ready(self):
        # Usa os certificados do sistema operacional (necessário em redes
        # com proxy/inspeção SSL, cujo certificado raiz não está no certifi).
        try:
            import truststore
        except ImportError:
            return

        truststore.inject_into_ssl()
