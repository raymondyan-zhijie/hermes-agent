"""Volcano (Volcengine Ark) Coding Plan provider profile.

Volcengine Ark's coding plan exposes Claude models (ark-code-latest)
through an Anthropic Messages-compatible endpoint. Authentication uses
a static bearer token via Authorization: Bearer (NOT x-api-key), so the
adapter's _requires_bearer_auth() must also match this base_url.
"""

from providers import register_provider
from providers.base import ProviderProfile

volcano_coding_plan = ProviderProfile(
    name="volcano-coding-plan",
    aliases=("volcano", "volcengine-ark", "ark-coding-plan"),
    display_name="Volcano (Volcengine Ark Coding Plan)",
    description="Volcengine Ark Coding Plan — Anthropic Messages API via Bearer token",
    signup_url="https://www.volcengine.com/product/ark",
    api_mode="anthropic_messages",
    env_vars=("VOLCANO_API_KEY", "ARK_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
    base_url="https://ark.cn-beijing.volces.com/api/coding",
    auth_type="api_key",
    fallback_models=("ark-code-latest",),
    default_aux_model="ark-code-latest",
)

register_provider(volcano_coding_plan)
