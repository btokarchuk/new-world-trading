from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from nwt_engine.domain import Fill, OrderState, OrderTicket


class OrderAck(BaseModel, frozen=True):
    client_order_id: str
    state: OrderState
    ts: datetime
    reason: str | None = None


class OrderStatus(BaseModel, frozen=True):
    client_order_id: str
    state: OrderState
    filled_qty: Decimal
    ts: datetime


class BrokerPosition(BaseModel, frozen=True):
    symbol: str
    qty: Decimal
    avg_cost: Decimal


class AccountState(BaseModel, frozen=True):
    ts: datetime
    cash: Decimal
    equity: Decimal


class Broker(ABC):
    """The parity boundary: SimBroker, AlpacaBroker(paper), AlpacaBroker(live).

    Strategy, sleeve, and risk code must behave identically against all three.
    """

    @abstractmethod
    def submit(self, ticket: OrderTicket) -> OrderAck: ...

    @abstractmethod
    def cancel(self, client_order_id: str) -> None: ...

    @abstractmethod
    def cancel_all(self) -> None: ...

    @abstractmethod
    def get_open_orders(self) -> list[OrderStatus]: ...

    @abstractmethod
    def get_positions(self) -> list[BrokerPosition]: ...

    @abstractmethod
    def get_account(self) -> AccountState: ...

    @abstractmethod
    def drain_events(self) -> list[Fill]:
        """Fills since last drain. Sim generates them; live drains a stream queue."""
