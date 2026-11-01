"""Browser-based Tsukikage service (no Qt)."""

try:  # the library is the single source of the product version
    from moonlib._version import __version__
except Exception:  # pragma: no cover - only when the library is not installed
    __version__ = "1.6.6"
