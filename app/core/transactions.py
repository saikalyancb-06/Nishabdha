"""Immutable transfer authorization and atomic, idempotent simulated execution."""

import hashlib
import secrets
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import select, text, update

from app.config import get_settings
from app.core.audit import canonical
from app.core.banking import ActionError
from app.core.security_models import FrozenTransfer, PasskeyApproval
from app.db import SessionLocal
from app.models import (
    Account,
    ActionRequest,
    Call,
    Customer,
    Payee,
    StepUp,
    Transaction,
    utcnow,
)


def digest(value: dict) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def valid_evidence(e: dict, call_id: str, action_id: str) -> bool:
    s = get_settings()
    sources = {"detectifai"} | ({"mock"} if s.app_env == "test" else set())
    try:
        age = (utcnow() - datetime.fromisoformat(e["checked_at"])).total_seconds()
        sp = e.get("speaker_prob")
        speaker_ok = True if sp is None else (sp >= s.tier1_min_trust and e.get("speaker_source") in sources)
        return bool(
            e["call_id"] == call_id
            and e["action_id"] == action_id
            and e.get("audio_hash")
            and e.get("spoof_source") in sources
            and 0 <= age <= 120
            and speaker_ok
            and e["synthetic_prob"] < s.synthetic_soft
        )
    except (KeyError, ValueError, TypeError):
        return False


async def freeze_transfer(
    action_id: str, customer_id: str, call_id: str, params: dict, evidence: dict
) -> dict:
    async with SessionLocal() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        action = await db.get(ActionRequest, action_id)
        if (
            not action
            or action.customer_id != customer_id
            or action.call_id != call_id
            or action.intent != "transfer_funds"
        ):
            raise ActionError("Transfer request does not belong to this session.")
        if not valid_evidence(evidence, call_id, action_id):
            raise ActionError(
                "Current DetectifAI verification is required before a voice transfer."
            )
        try:
            amount = Decimal(str(params.get("amount_rupees"))) * 100
            if (
                not amount.is_finite()
                or amount != amount.to_integral_value()
                or amount <= 0
            ):
                raise ValueError()
            paise = int(amount)
        except (InvalidOperation, TypeError, ValueError):
            raise ActionError(
                "Please specify a positive amount with at most two decimal places.",
                need="amount_rupees",
            )
        payees = list(
            (
                await db.execute(select(Payee).where(Payee.customer_id == customer_id))
            ).scalars()
        )
        if params.get("payee_id"):
            matches = [p for p in payees if p.id == params["payee_id"]]
        else:
            name = str(params.get("payee_name", "")).strip().casefold()
            matches = [p for p in payees if p.name.casefold() == name]
        if len(matches) != 1:
            raise ActionError(
                "Please select one exact saved beneficiary; the name is missing or ambiguous.",
                need="payee_name",
            )
        payee = matches[0]
        if payee.active_from > utcnow():
            raise ActionError("This beneficiary is still in the cooling period.")
        account = (
            await db.execute(
                select(Account)
                .where(Account.customer_id == customer_id)
                .order_by(Account.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if not account:
            raise ActionError("No source account found.")
        old = await db.get(FrozenTransfer, action_id)
        if old:
            if (
                old.snapshot["payee_id"] == payee.id
                and old.snapshot["amount_paise"] == paise
                and old.expires_at > utcnow()
            ):
                return old.snapshot
            await db.execute(
                update(StepUp)
                .where(StepUp.action_id == action_id, StepUp.status == "pending")
                .values(status="expired")
            )
            await db.execute(
                update(PasskeyApproval)
                .where(PasskeyApproval.action_id == action_id)
                .values(consumed=True)
            )
            action.status = "cancelled"
            await db.commit()
            raise ActionError(
                "Transaction changed. Start a new request and approve it again."
            )
        expires = utcnow() + timedelta(seconds=120)
        snapshot = {
            "version": 1,
            "action_id": action_id,
            "customer_id": customer_id,
            "call_id": call_id,
            "intent": "transfer_funds",
            "source_account_id": account.id,
            "source_last4": account.number[-4:],
            "payee_id": payee.id,
            "payee_name": payee.name,
            "destination": payee.account_number,
            "destination_last4": payee.account_number[-4:],
            "ifsc": payee.ifsc,
            "amount_paise": paise,
            "currency": "INR",
            "expires_at": expires.isoformat(),
            "nonce": secrets.token_urlsafe(24),
        }
        db.add(
            FrozenTransfer(
                action_id=action_id,
                customer_id=customer_id,
                call_id=call_id,
                snapshot=snapshot,
                action_hash=digest(snapshot),
                evidence=evidence,
                expires_at=expires,
            )
        )
        await db.commit()
        return snapshot


async def execute_transfer(action_id: str, customer_id: str, call_id: str) -> dict:
    from app.core.agent_identity import sign_record

    async with SessionLocal() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        act = await db.get(ActionRequest, action_id)
        frozen = await db.get(FrozenTransfer, action_id)
        if (
            not act
            or not frozen
            or frozen.customer_id != customer_id
            or frozen.call_id != call_id
        ):
            raise ActionError("Unknown transaction.")
        if act.status == "executed":
            return act.result
        if act.status not in {"pending", "awaiting_stepup", "awaiting_challenge"}:
            raise ActionError("Transaction is no longer executable.")
        if (
            frozen.expires_at <= utcnow()
            or digest(frozen.snapshot) != frozen.action_hash
        ):
            raise ActionError("Transaction approval expired or changed.")
        call = await db.get(Call, call_id)
        customer = await db.get(Customer, customer_id)
        if (
            not call
            or call.status != "active"
            or call.customer_id != customer_id
            or customer.voice_locked
            or call.risk_state == "fraud"
        ):
            raise ActionError("This banking session is locked or ended.")
        if not valid_evidence(frozen.evidence, call_id, action_id):
            raise ActionError("Voice verification is missing or expired.")
        approval = (
            (
                await db.execute(
                    select(PasskeyApproval).where(
                        PasskeyApproval.action_id == action_id,
                        PasskeyApproval.customer_id == customer_id,
                        PasskeyApproval.purpose == "transfer",
                        PasskeyApproval.binding_hash == frozen.action_hash,
                        PasskeyApproval.verified.is_(True),
                        PasskeyApproval.consumed.is_(False),
                        PasskeyApproval.expires_at > utcnow(),
                    )
                )
            )
            .scalars()
            .first()
        )
        step = (
            (
                await db.execute(
                    select(StepUp).where(
                        StepUp.action_id == action_id,
                        StepUp.status == "approved",
                        StepUp.customer_id == customer_id,
                        StepUp.call_id == call_id,
                        StepUp.expires_at > utcnow(),
                    )
                )
            )
            .scalars()
            .first()
        )
        if not approval or not step:
            raise ActionError(
                "A fresh passkey approval for this exact transaction is required."
            )
        snap = frozen.snapshot
        payee = await db.get(Payee, snap["payee_id"])
        if (
            not payee
            or payee.customer_id != customer_id
            or payee.account_number != snap["destination"]
            or payee.ifsc != snap["ifsc"]
            or payee.active_from > utcnow()
        ):
            raise ActionError(
                "Beneficiary changed or is not eligible. Start a new request."
            )
        previous = list(
            (
                await db.execute(
                    select(FrozenTransfer)
                    .join(ActionRequest, ActionRequest.id == FrozenTransfer.action_id)
                    .where(
                        FrozenTransfer.call_id == call_id,
                        ActionRequest.status == "executed",
                    )
                )
            ).scalars()
        )
        amount = snap["amount_paise"]
        if (
            sum(p.snapshot["amount_paise"] for p in previous) + amount
            > get_settings().max_transfer_paise
        ):
            raise ActionError("Per-call transfer limit exceeded.")
        changed = await db.execute(
            update(Account)
            .where(
                Account.id == snap["source_account_id"],
                Account.customer_id == customer_id,
                Account.balance_paise >= amount,
            )
            .values(balance_paise=Account.balance_paise - amount)
        )
        if changed.rowcount != 1:
            raise ActionError("Insufficient balance or invalid source account.")
        approval.consumed = True
        db.add(
            Transaction(
                account_id=snap["source_account_id"],
                amount_paise=-amount,
                description=f"Transfer to {payee.name}",
            )
        )
        balance = (await db.get(Account, snap["source_account_id"])).balance_paise
        result = {
            "summary": f"Sent Rs {amount / 100:,.2f} to {payee.name}, account ending {snap['destination_last4']}.",
            "data": {
                "amount_paise": amount,
                "payee": payee.name,
                "balance_paise": balance,
                "action_id": action_id,
            },
        }
        payload = {
            "type": "transaction_receipt",
            "action_id": action_id,
            "action_hash": frozen.action_hash,
            "customer_id": customer_id,
            "call_id": call_id,
            "policy_version": 2,
            "outcome": "executed",
            "evidence": frozen.evidence,
            "at": utcnow().isoformat(),
        }
        frozen.receipt = {"payload": payload, "signature": sign_record(payload)}
        act.status, act.result = "executed", result
        await db.commit()
        return result
