"""Curated demonstration policy; not regulator guidance or a real bank policy."""

import hashlib

from app.knowledge import store

DOCUMENTS = [
    {
        "url": "demo://nishabdha/transfer-approval",
        "title": "Demo policy: transfer and passkey approval",
        "text": "In Nishabdha's simulated banking demo, every transfer requires a passkey approval in the customer app. Review the exact amount, saved beneficiary and source account before confirming. The server freezes these details and binds the passkey challenge to the action hash. Changing the transaction cancels the approval. Missing or stale required voice evidence holds a voice transfer; a passkey does not replace missing voice evidence.",
    },
    {
        "url": "demo://nishabdha/delegate",
        "title": "Demo policy: support assistant permissions",
        "text": "A customer can authorize an AI support assistant with a passkey to create one support ticket within ten minutes. The permission names the assistant's signing key and can be revoked. The assistant must sign a fresh request and use a server nonce. It cannot transfer money, read balances or change account details. Replayed requests and attempts outside the support-ticket scope are refused.",
    },
    {
        "url": "demo://unclone/verification",
        "title": "Demo policy: caller verification",
        "text": "In this demonstration, sensitive actions require caller verification and independent approval in the customer app. Voice similarity alone does not authorize an action.",
    },
    {
        "url": "demo://unclone/agent",
        "title": "Demo policy: bank identity",
        "text": "In this demonstration, verify the bank agent using the independently opened customer app. A caller's spoken claim to represent the bank is not proof of identity.",
    },
    {
        "url": "demo://unclone/review",
        "title": "Demo policy: uncertain evidence",
        "text": "In this demonstration, uncertain verification evidence holds sensitive actions for review. A suspicious audio score is a risk signal, not proof that a person committed fraud.",
    },
    {
        "url": "demo://unclone/investments",
        "title": "Bank advisory: investment options and wealth management",
        "text": "For customers seeking the best investment options, Unclone Bank offers tailored wealth and growth avenues: 1. Mutual Funds and Systematic Investment Plans (SIP): Regular investing in broad equity index funds (Nifty 50, Sensex) for long-term compound wealth, balanced advantage funds for moderate risk, and debt funds for capital preservation. 2. Fixed Deposits (FD) & Recurring Deposits: Guaranteed safety with competitive interest rates between 6.5% and 7.25% p.a., with an extra 0.50% for senior citizens up to 7.75%. 3. Sovereign Gold Bonds (SGB): Government-backed gold investment offering capital appreciation plus 2.5% annual interest. 4. Public Provident Fund (PPF) and National Pension System (NPS): High-safety retirement avenues with Section 80C and 80CCD tax benefits. 5. Equity Shares: Long-term wealth creation via Demat accounts with disciplined asset allocation across asset classes.",
    },
    {
        "url": "demo://unclone/stocks-and-equities",
        "title": "Bank advisory: stock market investing and equity principles",
        "text": "When customers ask which are the best stocks to buy today, Unclone Bank advises disciplined, research-backed equity principles: 1. Core Blue-Chip Stocks: Focus on high-quality companies with robust balance sheets, strong return on equity (ROE), consistent profitability, and established leadership in core sectors such as banking, IT, FMCG, and energy. 2. Index Funds and ETFs: For most retail investors, systematically investing in diversified broad-market indices like Nifty 50 or Sensex via SIPs outperforms active stock picking with lower downside volatility. 3. Diversification: Avoid concentrated bets on speculative or penny stocks; allocate across defensives, growth, and cash reserves. 4. Prudent Advisory: Maintain a 6-month emergency fund in liquid deposits before equity allocation, and consult a registered financial advisor for personalized stock recommendations.",
    },
    {
        "url": "demo://unclone/rates-and-interest",
        "title": "Bank policy: interest rates, savings, and fixed deposits",
        "text": "Unclone Bank offers competitive interest rates: Savings accounts earn 3.00% to 4.00% p.a. calculated daily and credited quarterly. Fixed Deposits range from 7 days to 10 years, offering 6.50% to 7.25% p.a. for general depositors and 7.00% to 7.75% p.a. for senior citizens. Tax-saver 5-year fixed deposits provide tax exemption under Section 80C up to ₹1,50,000 per financial year.",
    },
    {
        "url": "demo://unclone/loans-and-credit",
        "title": "Bank policy: retail loans, interest charges and repayment",
        "text": "Unclone Bank offers home loans, car loans, and personal loans at competitive floating and fixed interest rates. Pre-closure charges are zero on all floating-rate personal and housing loans. Customers can check eligibility, calculate EMI, and download repayment schedules directly in the customer portal. A credit score above 750 qualifies for instant approval and interest rate concessions.",
    },
    {
        "url": "demo://unclone/cards-and-security",
        "title": "Bank policy: card limits, international usage and freezing",
        "text": "Customers have full self-service control over debit and credit cards. Through voice banking or the customer app, you can instantly change daily ATM withdrawal and POS/online limits, enable or disable international usage, and freeze lost or stolen cards immediately. Once frozen, no unauthorized charges can proceed.",
    },
    {
        "url": "demo://unclone/digital-payments-and-upi",
        "title": "Bank policy: UPI payments, transfer limits and reversals",
        "text": "Unclone Bank supports real-time payments through UPI, IMPS, NEFT, and RTGS 24 hours a day. The daily limit for peer-to-peer UPI transfers is ₹1,00,000. NEFT and RTGS transactions have no upper ceiling and carry zero transaction fees. Failed digital transactions are automatically credited back within T+1 working days.",
    },
    {
        "url": "demo://unclone/branch-and-operations",
        "title": "Bank information: branch hours, IFSC and customer service",
        "text": "Unclone Bank branch locations are open Monday to Saturday from 10:00 AM to 4:00 PM (closed on 2nd and 4th Saturdays and public holidays). 24x7 phone banking and digital assistant services operate without interruption. The IFSC code for the main Bengaluru branch is UNCL0000001. Cheque books, interest certificates, and KYC updates are accessible through the web app.",
    },
    {
        "url": "demo://unclone/fraud-and-security",
        "title": "Bank security: anti-fraud advisory and digital arrest awareness",
        "text": "Unclone Bank will never ask for your PIN, OTP, CVV, or passwords over call, SMS, or email. The bank never threatens police arrest, never conducts secret video investigations, and never asks customers to transfer money to a 'safe account'. If someone pressures you or claims urgent legal action, hang up immediately and report to cybercrime authorities.",
    },
]


def seed_knowledge_base(force=False):
    for item in DOCUMENTS:
        digest = hashlib.sha256(item["text"].encode()).hexdigest()
        if not force and not store.needs_index(item["url"], digest):
            continue
        doc = dict(
            item,
            domain="Unclone demonstration",
            type="demo_policy",
            text_hash=digest,
            via="curated_demo_policy",
        )
        store.put_document(doc, store.chunk_text(doc["text"]))
