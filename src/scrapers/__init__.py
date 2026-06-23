from src.db.models import Platform
from src.scrapers.base import ListingDetail, ListingPreview, Scraper
from src.scrapers.cetiri_zida import CetiriZidaScraper
from src.scrapers.cityexpert import CityExpertScraper
from src.scrapers.halooglasi import HalooglasiScraper
from src.scrapers.nekretnine import NekretnineScraper

SCRAPERS: dict[Platform, type[Scraper]] = {
    Platform.NEKRETNINE: NekretnineScraper,
    Platform.CETIRI_ZIDA: CetiriZidaScraper,
    Platform.HALOOGLASI: HalooglasiScraper,
    Platform.CITYEXPERT: CityExpertScraper,
}

__all__ = [
    "SCRAPERS",
    "CetiriZidaScraper",
    "CityExpertScraper",
    "HalooglasiScraper",
    "ListingDetail",
    "ListingPreview",
    "NekretnineScraper",
    "Scraper",
]
