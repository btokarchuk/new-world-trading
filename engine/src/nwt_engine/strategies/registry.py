from .base import BaseStrategy

_REGISTRY: dict[str, type[BaseStrategy]] = {}


def register(name: str):
    def deco(cls: type[BaseStrategy]) -> type[BaseStrategy]:
        cls.name = name
        if name in _REGISTRY:
            raise ValueError(f"duplicate strategy name: {name}")
        _REGISTRY[name] = cls
        return cls

    return deco


def get_strategy(name: str) -> type[BaseStrategy]:
    if name not in _REGISTRY:
        raise KeyError(f"unknown strategy {name!r}; registered: {sorted(_REGISTRY)}")
    return _REGISTRY[name]
