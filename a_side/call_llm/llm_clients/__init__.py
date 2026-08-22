from .BaseClient import LLMClient

__all__ = ["LLMClient"]

# Keep the base client usable in environments that have not installed the
# provider SDK (for example schema/unit tests).
try:
    from .GLMClient import GLMClient
except ModuleNotFoundError:  # pragma: no cover - depends on optional package
    GLMClient = None
else:
    __all__.append("GLMClient")

try:
    from .DSClient import DSClient
except ModuleNotFoundError:  # pragma: no cover - depends on optional package
    DSClient = None
else:
    __all__.append("DSClient")
