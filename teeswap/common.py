from importlib.metadata import metadata

_PKG = metadata(__package__ or __name__)
PKG_NAME: str = _PKG["Name"]
PKG_VERSION: str = _PKG["Version"]


DEFAULT_HOST = "0.0.0.0"  # noqa: S104
DEFAULT_PORT = 8402


class TeeSwapError(Exception):
    pass
