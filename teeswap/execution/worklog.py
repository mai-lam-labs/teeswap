"""The work log: what Mai did for a job, as Actions made of Steps (see docs/EXECUTION.md).

An Action is one operation carried out: "send 3 USDC to A". Its Steps are the
stages of doing it. A step may perform side effects; each is recorded on the
step before it is performed, so the log always shows what may have reached the
outside world. A step with a side effect that hasn't resolved is WAITING: it is
resolved from the chain (or whoever enforces the side effect's key), never by
performing a new side effect in its place.

The log is a record. Operations report into it through a StepReport; nothing
reads it back to decide what to do next.

In-flight positions in the ledger refer to the step holding them by its StepId.
"""

import enum
import re
from dataclasses import dataclass, field
from typing import Self, override

from ..blockchain.chains import Chain
from ..types import HttpExchange, Timestamp
from ..wire import Encodable, Validated, WireSchema, WireStruct
from .effects import IdempotencyKey, Outcome, SideEffect, SideEffectView


class StepId(str, Validated):
    """A step's id within its job: action number and step number, e.g. "a2.s1"."""

    PATTERN = r"^a[0-9]+\.s[0-9]+$"

    def __new__(cls, value: str) -> Self:
        if not isinstance(value, str):
            raise TypeError(f"StepId: expected str, got {type(value).__name__}")
        if re.fullmatch(cls.PATTERN, value) is None:
            raise ValueError(f"StepId: not a match for {cls.PATTERN}: {value!r}")
        return super().__new__(cls, value)

    @override
    def to_wire(self) -> Encodable:
        return str(self)

    @override
    @classmethod
    def json_schema(cls) -> WireSchema:
        return WireSchema(type="string", pattern=cls.PATTERN, description="work-log step id")


class StepStatus(enum.StrEnum):
    PENDING = "pending"
    EXECUTING = "executing"
    WAITING = "waiting"  # its side effects were performed; their outcome isn't known yet
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class StepView(WireStruct):
    id: StepId
    operation: str
    status: StepStatus
    chain: Chain | None
    started_at: Timestamp | None
    completed_at: Timestamp | None
    error: str | None
    side_effects: tuple[SideEffectView, ...]


@dataclass(slots=True)
class Step:
    id: StepId
    operation: str
    chain: Chain | None
    status: StepStatus = StepStatus.PENDING
    started_at: Timestamp | None = None
    completed_at: Timestamp | None = None
    side_effects: list[SideEffect] = field(default_factory=list)
    http_exchanges: list[HttpExchange] = field(default_factory=list)
    error: str | None = None

    def start(self) -> None:
        self.status = StepStatus.EXECUTING
        self.started_at = Timestamp.now()

    def wait(self) -> None:
        self.status = StepStatus.WAITING

    def complete(self) -> None:
        self.status = StepStatus.COMPLETED
        self.completed_at = Timestamp.now()

    def fail(self, error: str) -> None:
        self.status = StepStatus.FAILED
        self.completed_at = Timestamp.now()
        self.error = error

    def record(self, effect: SideEffect) -> None:
        """Record a side effect before performing it."""
        self.side_effects.append(effect)

    def resolve(self, key: IdempotencyKey, outcome: Outcome) -> None:
        """Every side effect with this key resolved the same way: at most one took effect."""
        for effect in self.side_effects:
            if effect.key == key:
                effect.resolve(outcome)

    @property
    def finished(self) -> bool:
        return self.status in (StepStatus.COMPLETED, StepStatus.FAILED)

    def view(self) -> StepView:
        return StepView(
            id=self.id,
            operation=self.operation,
            status=self.status,
            chain=self.chain,
            started_at=self.started_at,
            completed_at=self.completed_at,
            error=self.error,
            side_effects=tuple(e.view() for e in self.side_effects),
        )


class StepReport:
    """How an operation reports its step: what it sent, what came of it, how the step
    ended. The operation says what happened; nothing else decides it for the step."""

    __slots__ = ("_step",)

    def __init__(self, step: Step) -> None:
        self._step = step

    @property
    def id(self) -> StepId:
        return self._step.id

    def record(self, effect: SideEffect) -> None:
        """A side effect, before it is performed."""
        self._step.record(effect)

    def resolved(self, key: IdempotencyKey, outcome: Outcome) -> None:
        self._step.resolve(key, outcome)

    def waiting(self) -> None:
        """Its side effects are performed; the step is watching for their outcome."""
        self._step.wait()

    def completed(self) -> None:
        self._step.complete()

    def failed(self, reason: str) -> None:
        self._step.fail(reason)


@dataclass(frozen=True, slots=True)
class ActionView(WireStruct):
    id: str
    description: str
    source_chain: Chain
    steps: tuple[StepView, ...]


@dataclass(slots=True)
class Action:
    id: str  # "a1", "a2", ... in the order the job started them
    description: str
    source_chain: Chain
    started_at: Timestamp = field(default_factory=Timestamp.now)
    steps: list[Step] = field(default_factory=list)

    def add_step(self, operation: str) -> Step:
        step = Step(
            id=StepId(f"{self.id}.s{len(self.steps) + 1}"),
            operation=operation,
            chain=self.source_chain,
        )
        self.steps.append(step)
        return step

    @property
    def current_step(self) -> Step | None:
        for step in reversed(self.steps):
            if step.status in (StepStatus.EXECUTING, StepStatus.WAITING):
                return step
        return None

    @property
    def is_complete(self) -> bool:
        return bool(self.steps) and all(
            s.status in (StepStatus.COMPLETED, StepStatus.FAILED) for s in self.steps
        )

    def view(self) -> ActionView:
        return ActionView(
            id=self.id,
            description=self.description,
            source_chain=self.source_chain,
            steps=tuple(step.view() for step in self.steps),
        )


class WorkLog:
    """A job's actions, in the order they were started."""

    def __init__(self) -> None:
        self._actions: list[Action] = []

    def start(self, description: str, chain: Chain) -> Action:
        action = Action(
            id=f"a{len(self._actions) + 1}", description=description, source_chain=chain
        )
        self._actions.append(action)
        return action

    @property
    def actions(self) -> tuple[Action, ...]:
        return tuple(self._actions)
