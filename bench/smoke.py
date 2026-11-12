"""S1-S5 against a deployed Fern (live Column sandbox), plus reconciliation and the AI report.

Usage: python bench/smoke.py https://waken.dev/apps/fern
Uses one space from the pool. Exits non-zero on the first failed check.
"""

import sys
import uuid

import httpx

base = sys.argv[1].rstrip("/")
c = httpx.Client(base_url=base, timeout=90)


def check(cond: bool, what: str) -> None:
    print(("ok   " if cond else "FAIL ") + what, flush=True)
    if not cond:
        sys.exit(1)


r = c.post("/spaces")
check(r.status_code == 201, f"claim a space ({r.status_code})")
s = r.json()
h = {"Authorization": f"Bearer {s['token']}"}
run = uuid.uuid4().hex[:8]


def idem(k: str) -> dict:
    return {**h, "Idempotency-Key": f"{run}-{k}"}


def available() -> int:
    me = c.get("/me", headers=h).json()
    return next(x for x in me["customers"] if x["id"] == s["customer_id"])["available_minor"]


r = c.post("/demo/payroll", headers=h)
check(r.status_code == 201 and available() == 2000_00, "S1 payroll lands: $2,000.00")

r = c.post("/pulls", json={"amount_minor": 500_00}, headers=idem("pull-1"))
check(r.status_code == 201 and available() == 2000_00, "S2 pull requested, not spendable yet")
c.post("/demo/settle", headers=h)
c.post("/demo/release-funds", headers=h)
check(available() == 2500_00, "S2 pull settled and released: $2,500.00")

r = c.post("/p2p", json={"amount_minor": 50_00, "to_customer_id": s["npc_customer_id"]}, headers=idem("p2p-1"))
check(r.status_code == 201 and available() == 2450_00, "S3 P2P $50 sent")
again = c.post("/p2p", json={"amount_minor": 50_00, "to_customer_id": s["npc_customer_id"]}, headers=idem("p2p-1"))
same = again.json().get("id") == r.json()["id"]
check(same and available() == 2450_00, "S3 replay with the same key: no double send")

r = c.post("/withdrawals", json={"amount_minor": 300_00, "drill": "drop_response"}, headers=idem("wd-1"))
wd = r.json()["id"]
status = c.get(f"/transfers/{wd}", headers=h).json()["transfer"]["status"]
check(status == "unknown" and available() == 2150_00, "S4 bank response lost: status unknown, $300 held")
retried = c.post("/transfers/recover-unknown", headers=h).json()["retried"]
view = c.get(f"/transfers/{wd}", headers=h).json()
check(wd in retried and view["transfer"]["status"] == "processing", "S4 recovery retried with the same key")
check(available() == 2150_00, "S4 exactly one cash-out: $2,150.00")

r = c.post("/pulls", json={"amount_minor": 100_00, "counterparty_name": "RETURN_UNAUTH"}, headers=idem("pull-2"))
pull2 = r.json()["id"]
c.post("/demo/settle", headers=h)
view = c.get(f"/transfers/{pull2}", headers=h).json()["transfer"]
check(view["status"] == "returned" and available() == 2150_00, f"S5 pull returned ({view.get('return_code')})")

tb = c.get("/ledger/trial-balance", headers=h).json()
check(tb["debits_equal_credits"] and tb["balances_match_postings"], "books balance")

rec = c.post("/ops/reconciliation/run", headers=h).json()
open_ = [e for e in rec["exceptions"] if e["status"] == "open"]
check(open_ == [], f"reconciliation with Column: {rec['last_run']['matched']}/{rec['last_run']['scanned']} matched")

rep = c.post("/ops/reports", headers=h, timeout=180).json()
check(rep.get("status") == "done", f"AI report: source={rep.get('source')} model={rep.get('model')}")
print("headline:", rep["content"]["headline"])
print("space:", s["space_id"])
