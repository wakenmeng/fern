import os
from uuid import UUID

import psycopg
import pytest
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from fern import ledger, payments
from fern.bank import SimulatedBank
from fern.platform.db import create_pool
from fern.platform.migrate import migrate
from fern.spaces import NewSpace, create_space

TEST_DB = os.environ.get("FERN_TEST_DATABASE_URL", "postgresql://fern:fern@localhost:54329/fern_test")


@pytest.fixture(scope="session", autouse=True)
def fresh_schema() -> None:
    with psycopg.connect(TEST_DB, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    migrate(TEST_DB)


@pytest.fixture
async def db():
    conn = await AsyncConnection.connect(TEST_DB, autocommit=True, row_factory=dict_row)
    yield conn
    await conn.close()


@pytest.fixture
async def pool():
    p = create_pool(TEST_DB, max_size=20)
    await p.open()
    yield p
    await p.close()


@pytest.fixture
def bank() -> SimulatedBank:
    return SimulatedBank()


@pytest.fixture
async def space(db) -> NewSpace:
    return await create_space(db)


async def pay_salary(db, bank: SimulatedBank, space: NewSpace, amount: int) -> UUID:
    txn = await bank.receive_payroll(amount)
    return await payments.record_incoming_credit(db, bank.provider, space.space_id, space.customer_id, txn)


async def assert_books_balance(db, space_id: UUID) -> dict:
    tb = await ledger.trial_balance(db, space_id)
    assert tb["debits_equal_credits"], tb
    assert tb["balances_match_postings"], tb
    return {(a["role"], a["customer_id"]): a["balance_minor"] for a in tb["accounts"]}


def principal(space: NewSpace) -> str:
    return f"customer:{space.customer_id}"
