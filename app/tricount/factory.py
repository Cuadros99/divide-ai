from functools import lru_cache

from app.config import settings
from app.tricount.base import TricountBackend


@lru_cache
def get_tricount() -> TricountBackend:
    if settings.tricount_provider == "real":
        from app.tricount.real import TricountAPIBackend

        return TricountAPIBackend()

    from app.tricount.fake import FakeTricountBackend

    return FakeTricountBackend()
