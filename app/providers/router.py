from dataclasses import dataclass
from enum import Enum

class ProviderName(str, Enum):
    OPENAI = "openai"

@dataclass(frozen=True)
class ProviderRoute:
    provider: ProviderName
    model: str

ROUTES: dict[str, ProviderRoute] = {
    "copy": ProviderRoute(ProviderName.OPENAI, "gpt-5.6"),
    "classify": ProviderRoute(ProviderName.OPENAI, "gpt-5.6"),
    "summarize": ProviderRoute(ProviderName.OPENAI, "gpt-5.6"),
    "score": ProviderRoute(ProviderName.OPENAI, "gpt-5.6"),
    "creative_brief": ProviderRoute(ProviderName.OPENAI, "gpt-5.6"),
}

def resolve_route(task: str) -> ProviderRoute:
    try:
        return ROUTES[task]
    except KeyError as exc:
        raise ValueError(f"unsupported_task:{task}") from exc
