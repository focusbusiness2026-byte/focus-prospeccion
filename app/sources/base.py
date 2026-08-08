from __future__ import annotations

from abc import ABC, abstractmethod


class CompanySource(ABC):
    name: str

    @abstractmethod
    def discover(self, filters: dict) -> list[dict]:
        """Return normalized candidates with source evidence."""

