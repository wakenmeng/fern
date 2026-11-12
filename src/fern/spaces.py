"""Demo spaces: each visitor gets an isolated set of customers and ledger accounts (DESIGN §2.1)."""

from dataclasses import dataclass
from uuid import UUID, uuid4

from psycopg import AsyncConnection

from fern.bank import Bank
from fern.ledger import SYSTEM_ROLES
from fern.platform.db import transaction


@dataclass(frozen=True)
class NewSpace:
    space_id: UUID
    customer_id: UUID
    npc_customer_id: UUID


async def create_space(
    conn: AsyncConnection,
    bank: Bank | None = None,
    display_name: str = "Jordan Rivera",
    pool_status: str = "claimed",
) -> NewSpace:
    space = NewSpace(uuid4(), uuid4(), uuid4())
    # Opening the bank account is idempotent per space id, so a retry reuses the same account.
    account_id, account_number_id = await bank.open_account(str(space.space_id)) if bank else (None, None)

    async def work(c: AsyncConnection) -> NewSpace:
        await c.execute(
            "INSERT INTO spaces (id, bank_account_id, bank_account_number_id, pool_status) VALUES (%s, %s, %s, %s)",
            (space.space_id, account_id, account_number_id, pool_status),
        )
        await c.execute(
            "INSERT INTO customers (id, space_id, kind, display_name) "
            "VALUES (%s, %s, 'primary', %s), (%s, %s, 'npc', 'Alex')",
            (space.customer_id, space.space_id, display_name, space.npc_customer_id, space.space_id),
        )
        for role in SYSTEM_ROLES:
            await c.execute(
                "INSERT INTO accounts (id, space_id, role) VALUES (%s, %s, %s)",
                (uuid4(), space.space_id, role),
            )
        for customer_id in (space.customer_id, space.npc_customer_id):
            await c.execute(
                "INSERT INTO accounts (id, space_id, role, customer_id) VALUES (%s, %s, 'customer_deposit', %s)",
                (uuid4(), space.space_id, customer_id),
            )
        return space

    return await transaction(conn, work)


async def load_space(conn: AsyncConnection, space_id: UUID) -> NewSpace:
    cur = await conn.execute("SELECT id, kind FROM customers WHERE space_id = %s", (space_id,))
    ids = {r["kind"]: r["id"] for r in await cur.fetchall()}
    return NewSpace(space_id, ids["primary"], ids["npc"])
