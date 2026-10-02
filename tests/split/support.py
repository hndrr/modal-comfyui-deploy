"""Small Modal doubles; assertions stay in the tests that use them."""
from types import SimpleNamespace
from unittest.mock import AsyncMock


def remote_mock(result=None, **kwargs):
    return SimpleNamespace(aio=AsyncMock(return_value=result, **kwargs))


def volume_mocks(*names):
    return {
        name: SimpleNamespace(commit=remote_mock(), reload=remote_mock())
        for name in names or ("input", "output", "data", "models", "environment")
    }
