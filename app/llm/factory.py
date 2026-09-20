from functools import lru_cache

from app.config import settings
from app.llm.base import ListaParser


@lru_cache
def get_parser() -> ListaParser:
    if settings.llm_provider == "openai":
        from app.llm.openai_parser import OpenAIParser

        return OpenAIParser()

    from app.llm.fake_parser import FakeParser

    return FakeParser()
