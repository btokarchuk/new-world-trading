import pytest

from nwt_engine.domain import IllegalOrderTransition, OrderState, assert_transition


def test_legal_lifecycle():
    path = [
        OrderState.INTENT,
        OrderState.APPROVED,
        OrderState.SUBMITTED,
        OrderState.ACKED,
        OrderState.PARTIAL,
        OrderState.FILLED,
    ]
    for current, new in zip(path, path[1:], strict=False):
        assert_transition(current, new)


@pytest.mark.parametrize(
    ("current", "new"),
    [
        (OrderState.FILLED, OrderState.CANCELED),
        (OrderState.REJECTED, OrderState.ACKED),
        (OrderState.INTENT, OrderState.FILLED),
        (OrderState.CANCELED, OrderState.PARTIAL),
    ],
)
def test_illegal_transitions_raise(current, new):
    with pytest.raises(IllegalOrderTransition):
        assert_transition(current, new)
