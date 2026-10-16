"""Demo bank data. `reset_demo()` wipes everything and recreates it (POST /api/demo/reset)."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete

from app.db import Base, engine, session_scope
from app.models import Account, Card, Customer, Payee, Transaction, utcnow

DEMO = [
    {
        "id": "cus_ananya", "name": "Ananya Rao", "phone": "9876543210", "lang": "en", "app_token": "app-ananya-demo",
        "account": ("50100012344521", 1_24_500_00),
        "cards": [("4521", "debit", 50_000_00), ("8890", "credit", 1_00_000_00)],
        "payees": [("Amma", "30214455667788", -48)],
        "txns": [(-1_250_00, "Swiggy"), (-899_00, "Amazon"), (85_000_00, "Salary - Acme Tech"), (-15_000_00, "Rent to Amma"), (-450_00, "Namma Metro")],
    },
    {
        "id": "cus_rahul", "name": "Rahul Sharma", "phone": "9123456780", "lang": "hi", "app_token": "app-rahul-demo",
        "account": ("50100099887766", 42_300_00),
        "cards": [("3311", "debit", 25_000_00)],
        "payees": [("Papa", "11223344556677", -72)],
        "txns": [(-2_100_00, "BigBasket"), (-600_00, "Jio Recharge"), (40_000_00, "Salary")],
    },
    {
        "id": "cus_lakshmi", "name": "Lakshmi Gowda", "phone": "9900112233", "lang": "kn", "app_token": "app-lakshmi-demo",
        "account": ("50100055443322", 3_05_000_00),
        "cards": [("7702", "debit", 40_000_00)],
        "payees": [],
        "txns": [(-5_000_00, "LIC Premium"), (1_20_000_00, "Pension"), (-780_00, "BESCOM")],
    },
]


async def reset_demo_state() -> dict:
    """Reset customer balances, cards, and session states without wiping audit history."""
    from sqlalchemy import select
    from app.models import StepUp, Challenge
    async with session_scope() as db:
        for d in DEMO:
            c = await db.get(Customer, d["id"])
            if c:
                c.voice_locked = False
            acc = await db.get(Account, f"acc_{d['id'][4:]}")
            if acc:
                acc.balance_paise = d["account"][1]
            for last4, kind, limit in d["cards"]:
                card = (
                    await db.execute(
                        select(Card).where(Card.customer_id == d["id"], Card.last4 == last4)
                    )
                ).scalar_one_or_none()
                if card:
                    card.daily_limit_paise = limit
                    card.status = "active"
                    card.international_enabled = False
        for st in (await db.execute(select(StepUp).where(StepUp.status == "pending"))).scalars():
            st.status = "expired"
        for ch in (await db.execute(select(Challenge).where(Challenge.status == "pending"))).scalars():
            ch.status = "expired"
    return {"status": "ok", "message": "Demo state reset to initial seed values."}


async def reset_demo() -> dict:
    async with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(delete(table))
    return await seed()


async def seed() -> dict:
    now = utcnow()
    async with session_scope() as db:
        for d in DEMO:
            if await db.get(Customer, d["id"]):
                continue
            c = Customer(id=d["id"], name=d["name"], phone=d["phone"], preferred_lang=d["lang"], app_token=d["app_token"])
            db.add(c)
            acc = Account(id=f"acc_{d['id'][4:]}", customer_id=c.id, number=d["account"][0], balance_paise=d["account"][1])
            db.add(acc)
            await db.flush()  # parents first: SQLite enforces foreign keys
            for last4, kind, limit in d["cards"]:
                db.add(Card(customer_id=c.id, last4=last4, kind=kind, daily_limit_paise=limit))
            for name, accno, hours in d["payees"]:
                db.add(Payee(customer_id=c.id, name=name, account_number=accno, active_from=now + timedelta(hours=hours)))
            for i, (amt, desc) in enumerate(d["txns"]):
                db.add(Transaction(account_id=acc.id, amount_paise=amt, description=desc, created_at=now - timedelta(days=i + 1)))
    return {"customers": [{"id": d["id"], "name": d["name"], "phone": d["phone"], "app_token": d["app_token"]} for d in DEMO]}
