import json
import re
import threading
import time
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup
from django.conf import settings


BASIC_LANDS = ["Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes"]

WORTHLESS_TOKENS = [
    "Beau Token", "Bird Illusion Token", "Bird Token", "Elemental Token",
    "Elf Warrior Token", "Goblin Token", "Human Warrior Token", "Servo Token",
    "Soldier Token", "Spirit Token", "Squirrel Token", "Zombie Berserker Token",
    "Replicated Ring Token",
]

WORTHLESS_CARDS = {c.lower() for c in BASIC_LANDS + WORTHLESS_TOKENS}

_request_lock = threading.Lock()


class PriceNotFound(Exception):
    """A página da carta não tem os dados de preço esperados."""


class LigaMagicScraper:

    def __init__(self, delay: float | None = None):
        self.delay = settings.LIGAMAGIC_DELAY if delay is None else delay

        self.session = requests.Session()

        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,*/*;q=0.8"
            ),
            "Accept-Language": "pt-BR,pt;q=0.9",
        })

    def _get(self, url):
        # Todas as threads compartilham o mesmo ritmo de requisições
        with _request_lock:
            response = self.session.get(url, timeout=30)

            response.raise_for_status()

            time.sleep(self.delay)

        return response.text

    def find_card_url(self, card_name: str):
        return (
            f"{settings.LIGAMAGIC_BASE_URL}/?view=cards/"
            f"card&card={quote_plus(card_name)}"
        )

    @staticmethod
    def get_min_price(prices_list):
        p_list = []

        for item in prices_list:
            if type(item["price"]) is list:
                p_list.append(min(float(price["p"]) for price in item["price"]))
            elif type(item["price"]["0"]) is dict:
                p_list.append(float(item["price"]["0"]["p"]))
            else:
                prices = [float(price["p"]) for price in item["price"]["0"]]
                if prices:
                    p_list.append(min(prices))

        positive = [p for p in p_list if p > 0]

        return min(positive) if positive else None

    @staticmethod
    def _javascript_var(soup, var_name):
        script = soup.find("script", string=re.compile(var_name))

        if script is None:
            raise PriceNotFound(f"variável {var_name} não encontrada")

        for line in script.text.split("\n"):
            if "var " + var_name in line:
                return json.loads(line.split("=", 1)[1].strip().rstrip(";"))

        raise PriceNotFound(f"variável {var_name} não encontrada")

    def scrape_card_page(self, url: str):
        soup = BeautifulSoup(self._get(url), "html.parser")

        try:
            if soup.find_all("div", {"class": "container-lot-of-stock"}):
                prices_list = self._javascript_var(soup, "cardsjson")
                prices = [
                    float(p["precoMenor"])
                    for p in prices_list
                    if float(p["precoMenor"]) > 0
                ]
                min_price = min(prices) if prices else None
            else:
                prices_list = self._javascript_var(soup, "cards_editions")
                min_price = self.get_min_price(prices_list)

        except PriceNotFound:
            raise
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise PriceNotFound(f"formato inesperado: {exc}") from exc

        return {
            "price_brl": min_price,
            "foil_price_brl": None,
            "url": url,
        }

    @staticmethod
    def name_candidates(card_name: str) -> list[str]:
        """
        Nomes a tentar na LigaMagic, nesta ordem.

        Cartas divididas e aventuras ("Spite // Malice", "Brazen Borrower //
        Petty Theft") só são encontradas pelo nome completo; cartas de dupla
        face ("Delver of Secrets // Insectile Aberration"), só pelo da frente.
        """

        full = card_name.strip()
        front = full.split(" // ")[0].strip()

        return [full] if front == full else [full, front]

    def get_price(self, card_name: str):
        candidates = self.name_candidates(card_name)

        if card_name.lower() in WORTHLESS_CARDS:
            return {"price_brl": 0, "foil_price_brl": None, "url": self.find_card_url(candidates[-1])}

        error = None

        for name in candidates:
            try:
                return self.scrape_card_page(self.find_card_url(name))
            except PriceNotFound as exc:
                error = exc

        raise error
