"""受控数据 Provider 层。"""

from .router import (
    ProviderResult,
    ProviderRouter,
    get_provider_router,
    reset_provider_router,
)

__all__ = [
    'ProviderResult',
    'ProviderRouter',
    'get_provider_router',
    'reset_provider_router',
]
