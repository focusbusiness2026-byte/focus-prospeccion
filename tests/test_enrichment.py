import socket

import pytest
from bs4 import BeautifulSoup

from app.enrichment import PublicWebScraper, _public_url


def test_private_targets_are_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(ValueError, match="privadas"):
        _public_url("https://internal.example")


def test_social_links_are_discovered_without_scraping_profiles():
    soup = BeautifulSoup(
        '<a href="https://www.linkedin.com/company/focus">LinkedIn</a>'
        '<a href="https://instagram.com/focus">Instagram</a>'
        '<a href="/contact">Contacto</a>',
        "html.parser",
    )
    links = PublicWebScraper._social_links(soup, "https://focus.example")
    assert links == {
        "linkedin": "https://www.linkedin.com/company/focus",
        "instagram": "https://instagram.com/focus",
    }
