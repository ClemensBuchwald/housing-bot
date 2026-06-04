from __future__ import annotations

from abc import ABC, abstractmethod

from typing import List

from src.config import Criteria
from src.models import Listing


class BaseScraper(ABC):
    name: str = "base"

    @abstractmethod
    def fetch_listings(self, criteria: Criteria) -> List[Listing]:
        """Fetch raw listings from the portal. Must be implemented per portal."""
        ...

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}>"
