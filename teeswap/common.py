from typing import Any

import dacite


class TeeSwapError(Exception):
    pass


DACITE_CONFIG = dacite.Config(strict=True)


def from_dict[T](cls: type[T], data: dict[str, Any]) -> T:
    """Typed wrapper around dacite.from_dict.

    dacite's Data protocol has a malformed __getitem__ signature
    (*args, **kwargs instead of key: str), so pyrefly rejects
    dict[str, Any] as incompatible. This wrapper narrows the
    suppression to one location.
    """
    return dacite.from_dict(
        data_class=cls,
        data=data,  # pyrefly: ignore[bad-argument-type]
        config=DACITE_CONFIG,
    )
