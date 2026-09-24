"""Chain keys: what controls an account on a chain.

Each chain family has its own key type (its curve, address format and signing).
Code that needs to sign asks for the key type it works with; code that only
holds accounts sees a ChainKey.
"""

import abc


class ChainKey(abc.ABC):
    @property
    @abc.abstractmethod
    def address(self) -> str:
        """The key's address, in its chain family's format."""

    @property
    @abc.abstractmethod
    def private_key(self) -> bytes:
        """The secret itself: only ever handed to an invoice's owner, with the tools down."""
