"""CorpClaw Lite — corporate AI agent for closed-loop environments with local LLMs."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

__all__ = [
    "__version__",
]

try:
    __version__ = _dist_version("corpclaw-lite")
except PackageNotFoundError:  # source/editable checkout without installed metadata
    __version__ = "0.0.0+unknown"
