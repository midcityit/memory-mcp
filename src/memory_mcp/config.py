import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    qdrant_url: str
    api_token: str
    stale_days: int


def load_config() -> Config:
    return Config(
        qdrant_url=os.environ["QDRANT_URL"],
        api_token=os.environ["API_TOKEN"],
        stale_days=int(os.environ.get("STALE_DAYS", "30")),
    )
