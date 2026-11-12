"""Explicitly authorized models and provider-reported snapshot aliases."""

from typing import Literal

ModelId = Literal["openai/gpt-6-luna", "openai/gpt-5.6-luna", "openai/gpt-6-astra"]
DEFAULT_MODEL = "openai/gpt-6-luna"
MODEL_ALIASES = {
    "openai/gpt-6-luna": {"openai/gpt-6-luna", "gpt-6-luna"},
    "openai/gpt-5.6-luna": {
        "openai/gpt-5.6-luna",
        "openai/gpt-5.6-luna-20260709",
        "gpt-5.6-luna",
    },
    "openai/gpt-6-astra": {
        "openai/gpt-6-astra",
        "openai/gpt-6-astra-20260903",
        "gpt-6-astra",
    },
}
