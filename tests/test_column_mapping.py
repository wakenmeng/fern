"""Column ACH objects -> cash movements (no network)."""

import pytest

from fern.column import ColumnError, to_snapshot


def obj(**kw):
    base = {
        "id": "acht_1",
        "amount": 30000,
        "type": "CREDIT",
        "status": "INITIATED",
        "created_at": "2026-11-12T17:00:00Z",
        "initiated_at": "2026-11-12T17:00:01Z",
    }
    return {**base, **kw}


def kinds(s):
    return [(t.kind, t.amount_minor) for t in s.transactions]


def test_credit_cash_leaves_at_initiation():
    s = to_snapshot(obj())
    assert s.status == "initiated" and kinds(s) == [("initiation", -30000)]
    assert s.transactions[0].provider_txn_id == "acht_1:initiation"  # stable across re-reads


def test_credit_returned_brings_cash_back():
    s = to_snapshot(obj(status="RETURNED", returned_at="2026-11-13T17:00:00Z", return_details=[{"return_code": "R02"}]))
    assert s.status == "returned" and s.return_code == "R02"
    assert kinds(s) == [("initiation", -30000), ("return", 30000)]


def test_debit_cash_arrives_only_at_settlement():
    assert kinds(to_snapshot(obj(type="DEBIT", status="SUBMITTED"))) == []
    s = to_snapshot(obj(type="DEBIT", status="SETTLED", settled_at="2026-11-14T13:00:00Z"))
    assert s.status == "settled" and kinds(s) == [("settlement", 30000)]


def test_debit_returned_after_settlement_takes_cash_back():
    s = to_snapshot(
        obj(
            type="DEBIT",
            status="RETURNED",
            settled_at="2026-11-14T13:00:00Z",
            returned_at="2026-11-15T13:00:00Z",
            return_details=[{"return_code": "R29"}],
        )
    )
    assert kinds(s) == [("settlement", 30000), ("return", -30000)]


def test_debit_returned_before_settlement_moves_no_cash():
    s = to_snapshot(obj(type="DEBIT", status="RETURNED", returned_at="2026-11-13T13:00:00Z"))
    assert s.status == "returned" and kinds(s) == []


def test_completed_counts_as_settled_and_unknown_status_is_loud():
    assert to_snapshot(obj(status="COMPLETED")).status == "settled"
    with pytest.raises(ColumnError):
        to_snapshot(obj(status="SOMETHING_NEW"))
