"""Vertical runtimes keyed by vertical name."""

from .ecom import EcomRuntime
from .gaming import GamingRuntime
from .streaming import StreamingRuntime

RUNTIMES = {"streaming": StreamingRuntime, "ecom": EcomRuntime, "gaming": GamingRuntime}
