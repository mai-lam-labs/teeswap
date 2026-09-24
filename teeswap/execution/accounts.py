"""The accounts under a job's control (see docs/EXECUTION.md).

A job has no single address. It has accounts: each is a key on one chain, of
that chain's key type, derived from the job's seed for a purpose. An action asks
for the account it needs by chain and purpose, and gets the existing one or a
new one. Derivation is deterministic in (seed, chain, purpose), so the same
request always names the same account.
"""

import hmac
from dataclasses import dataclass

from ..blockchain.chains import Chain
from ..blockchain.derivation import derive_key
from ..blockchain.keys import ChainKey
from ..common import TeeSwapError
from ..types import Address


class AccountError(TeeSwapError):
    pass


@dataclass(frozen=True, slots=True)
class Account:
    address: Address
    purpose: str  # what the job uses it for; part of its derivation


class Accounts:
    def __init__(self, seed: bytes) -> None:
        self._seed = seed
        self._accounts: dict[tuple[Chain, str], Account] = {}
        self._keys: dict[Address, ChainKey] = {}

    @classmethod
    def for_job(cls, root_key: bytes, job_id: str) -> Accounts:
        return cls(hmac.digest(root_key, job_id.encode(), "sha256"))

    def get(self, chain: Chain, purpose: str) -> Account:
        """The job's account on `chain` for `purpose`, derived on first request."""
        existing = self._accounts.get((chain, purpose))
        if existing is not None:
            return existing
        key = derive_key(chain, self._seed, f"{chain.caip2}/{purpose}".encode())
        account = Account(address=Address(chain, key.address), purpose=purpose)
        self._accounts[(chain, purpose)] = account
        self._keys[account.address] = key
        return account

    def input(self, index: int, chain: Chain) -> Account:
        """Where the job's `index`th input is deposited."""
        return self.get(chain, f"input/{index}")

    def key[K: ChainKey](self, address: Address, kind: type[K]) -> K:
        """The key controlling one of the job's accounts, as the type the caller signs with."""
        key = self._keys.get(address)
        if not isinstance(key, kind):
            raise AccountError(
                f"{address.value} on {address.chain.name} is not a job account with a {kind.__name__}"
            )
        return key

    @property
    def all(self) -> tuple[Account, ...]:
        return tuple(self._accounts.values())
