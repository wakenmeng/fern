"""Load test for Fern. Runs inside the cluster against a copy of the API that uses the simulated bank.

Each virtual user (VU) gets its own space, takes a $10,000 payroll, and then loops over a mix of
reads and money movements: views, P2P, withdrawals, pulls, and a share of idempotent replays.
After the load stops, every VU settles its space and checks that the money is exactly right:

  * the books balance (trial balance: debits = credits, balances = postings);
  * the customer's balance equals what the VU expects from the requests that succeeded;
  * nothing is still held, and no balance went negative;
  * replaying an idempotency key returned the original transfer, never a second one.

Usage: python load.py --base http://fern-bench.fern.svc --out /tmp/bench.json
Stages are "seconds:vus" pairs, e.g. --stages 60:50,180:200,60:400.
"""

import argparse
import asyncio
import json
import random
import statistics
import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime

import httpx

PAYROLL = 10_000_00
# (weight, name). Money movements are $1-$5 so a VU never runs out during the test.
MIX = [(40, "GET /me"), (15, "GET /transfers"), (15, "POST /p2p"), (12, "POST /withdrawals"), (12, "POST /pulls"), (6, "replay")]


class Stats:
    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.per_sec: dict[int, list[float]] = defaultdict(list)
        self.errors_per_sec: dict[int, int] = defaultdict(int)
        self.vus_per_sec: dict[int, int] = {}
        self.by_endpoint: dict[str, list[float]] = defaultdict(list)
        self.status: dict[str, int] = defaultdict(int)

    def record(self, name: str, ms: float, status: int) -> None:
        sec = int(time.monotonic() - self.t0)
        self.per_sec[sec].append(ms)
        self.by_endpoint[name].append(ms)
        self.status[str(status)] += 1
        if status >= 500 or status == 0:
            self.errors_per_sec[sec] += 1


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(p / 100 * len(xs)))], 1)


class VU:
    def __init__(self, client: httpx.AsyncClient, stats: Stats) -> None:
        self.c, self.stats = client, stats
        self.expected = PAYROLL
        self.keys: list[tuple[str, str, dict, str]] = []  # (path, key, body, transfer id)
        self.replay_mismatches = 0
        self.rejected = 0

    async def call(self, name: str, method: str, path: str, **kw) -> httpx.Response | None:
        t = time.perf_counter()
        try:
            r = await self.c.request(method, path, headers={**self.h, **kw.pop("headers", {})}, **kw)
        except httpx.HTTPError:
            self.stats.record(name, (time.perf_counter() - t) * 1000, 0)
            return None
        self.stats.record(name, (time.perf_counter() - t) * 1000, r.status_code)
        return r

    async def setup(self) -> None:
        self.h: dict[str, str] = {}
        r = await self.call("POST /spaces", "POST", "/spaces")
        s = r.json()
        self.h = {"Authorization": f"Bearer {s['token']}"}
        me = (await self.call("GET /me", "GET", "/me")).json()
        self.me = me["customer_id"]
        self.npc = next(c["id"] for c in me["customers"] if c["kind"] == "npc")
        await self.call("POST /demo/payroll", "POST", "/demo/payroll", json={"amount_minor": PAYROLL})

    async def move(self, name: str, path: str, body: dict, sign: int) -> None:
        key = str(uuid.uuid4())
        r = await self.call(name, "POST", path, json=body, headers={"Idempotency-Key": key})
        if r is not None and r.status_code == 201:
            self.expected += sign * body["amount_minor"]
            self.keys.append((path, key, body, r.json()["id"]))
        elif r is not None and r.status_code < 500:
            self.rejected += 1

    async def step(self) -> None:
        name = random.choices([n for _, n in MIX], weights=[w for w, _ in MIX])[0]
        amount = random.randint(1, 5) * 100
        if name == "GET /me":
            await self.call(name, "GET", "/me")
        elif name == "GET /transfers":
            await self.call(name, "GET", "/transfers")
        elif name == "POST /p2p":
            await self.move(name, "/p2p", {"amount_minor": amount, "to_customer_id": self.npc}, -1)
        elif name == "POST /withdrawals":
            await self.move(name, "/withdrawals", {"amount_minor": amount}, -1)
        elif name == "POST /pulls":
            await self.move(name, "/pulls", {"amount_minor": amount}, +1)
        elif self.keys:  # replay an earlier request with the same key: must return the same transfer
            path, key, body, tid = random.choice(self.keys)
            r = await self.call("replay (same key)", "POST", path, json=body, headers={"Idempotency-Key": key})
            if r is None or r.status_code >= 300 or r.json().get("id") != tid:
                self.replay_mismatches += 1

    async def verify(self) -> dict:
        await self.call("POST /demo/settle", "POST", "/demo/settle")
        tb = (await self.call("GET /ledger/trial-balance", "GET", "/ledger/trial-balance")).json()
        me = (await self.call("GET /me", "GET", "/me")).json()
        mine = next(c for c in me["customers"] if c["id"] == self.me)
        return {
            "books_balance": tb["debits_equal_credits"] and tb["balances_match_postings"],
            "balance_ok": mine["balance_minor"] == self.expected,
            "nothing_held": mine["held_minor"] == 0,
            "non_negative": all(c["balance_minor"] >= 0 for c in me["customers"]),
            "replay_mismatches": self.replay_mismatches,
            "transfers": len(self.keys),
            "diff": mine["balance_minor"] - self.expected,
        }


async def run(args: argparse.Namespace) -> dict:
    stages = [tuple(int(x) for x in s.split(":")) for s in args.stages.split(",")]
    peak = max(v for _, v in stages)
    limits = httpx.Limits(max_connections=peak + 50, max_keepalive_connections=peak + 50)
    async with httpx.AsyncClient(base_url=args.base, timeout=30, limits=limits) as client:
        setup_stats = Stats()
        vus = [VU(client, setup_stats) for _ in range(peak)]
        sem = asyncio.Semaphore(50)

        async def setup(v: VU) -> None:
            async with sem:
                await v.setup()

        print(f"setting up {peak} spaces", flush=True)
        await asyncio.gather(*(setup(v) for v in vus))

        stats = Stats()
        for v in vus:
            v.stats = stats
        active = 0
        stop_at: list[float] = []

        async def loop(v: VU, index: int) -> None:
            while time.monotonic() < stop_at[0]:
                if index >= active:
                    await asyncio.sleep(0.05)
                    continue
                await v.step()
                await asyncio.sleep(random.uniform(0, args.think))

        total = sum(d for d, _ in stages)
        stop_at.append(time.monotonic() + total)
        tasks = [asyncio.create_task(loop(v, i)) for i, v in enumerate(vus)]
        for duration, n in stages:
            active = n
            print(f"stage: {n} VUs for {duration}s", flush=True)
            end = time.monotonic() + duration
            while time.monotonic() < end:
                stats.vus_per_sec[int(time.monotonic() - stats.t0)] = active
                await asyncio.sleep(1)
        await asyncio.gather(*tasks)

        print("verifying money", flush=True)
        checks = await asyncio.gather(*(v.verify() for v in vus))

    secs = sorted(stats.per_sec)
    series = [
        {
            "t": s,
            "rps": len(stats.per_sec[s]),
            "p50": pct(stats.per_sec[s], 50),
            "p95": pct(stats.per_sec[s], 95),
            "errors": stats.errors_per_sec.get(s, 0),
            "vus": stats.vus_per_sec.get(s, 0),
        }
        for s in secs
        if s < total
    ]
    all_ms = [ms for s in secs for ms in stats.per_sec[s]]
    rps = [p["rps"] for p in series]
    window = 10
    rolling = [statistics.mean(rps[i : i + window]) for i in range(max(1, len(rps) - window + 1))]
    sustained_stage = max(stages, key=lambda s: s[0])
    start = sum(d for d, _ in stages[: stages.index(sustained_stage)])
    sustained = series[start : start + sustained_stage[0]]
    return {
        "run_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "method": {
            "where": "k8s Job inside the EKS cluster, against the fern-bench Deployment",
            "bank": "simulated (in-process, Column-like contract); the Column sandbox is not load tested",
            "database": "the same RDS instance, separate database fern_bench",
            "stages": [{"seconds": d, "vus": n} for d, n in stages],
            "think_time_s": [0, args.think],
            "mix": {n: w for w, n in MIX},
            "api_replicas": args.replicas,
        },
        "headline": {
            "peak_rps_10s": round(max(rolling), 1),
            "sustained_rps": round(statistics.mean(p["rps"] for p in sustained), 1) if sustained else 0,
            "sustained_p95_ms": pct([ms for p in sustained for ms in stats.per_sec[p["t"]]], 95),
            "peak_vus": peak,
            "requests": len(all_ms),
            "error_rate": round(sum(stats.errors_per_sec.values()) / max(1, len(all_ms)), 5),
            "p50_ms": pct(all_ms, 50),
            "p95_ms": pct(all_ms, 95),
            "p99_ms": pct(all_ms, 99),
        },
        "series": series,
        "endpoints": [
            {"name": n, "count": len(xs), "p50": pct(xs, 50), "p95": pct(xs, 95), "p99": pct(xs, 99)}
            for n, xs in sorted(stats.by_endpoint.items(), key=lambda kv: -len(kv[1]))
        ],
        "status_codes": dict(stats.status),
        "money": {
            "spaces": len(checks),
            "transfers": sum(c["transfers"] for c in checks),
            "rejected_by_rules": sum(v.rejected for v in vus),
            "books_balance": all(c["books_balance"] for c in checks),
            "balances_exact": sum(c["balance_ok"] for c in checks),
            "nothing_held": sum(c["nothing_held"] for c in checks),
            "non_negative": all(c["non_negative"] for c in checks),
            "duplicate_transfers": sum(c["replay_mismatches"] for c in checks),
            "money_errors": sum(
                (not c["books_balance"]) + (not c["balance_ok"]) + (not c["nothing_held"]) + (not c["non_negative"])
                + c["replay_mismatches"]
                for c in checks
            ),
            "sample_diffs": [c["diff"] for c in checks if c["diff"]][:10],
        },
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://fern-bench.fern.svc")
    p.add_argument("--stages", default="30:50,240:200,60:400,30:50")
    p.add_argument("--think", type=float, default=1.0, help="max think time between steps, seconds")
    p.add_argument("--replicas", type=int, default=2)
    p.add_argument("--out", default="/tmp/bench.json")
    args = p.parse_args()
    result = asyncio.run(run(args))
    with open(args.out, "w") as f:
        json.dump(result, f, indent=1)
    print(json.dumps({"headline": result["headline"], "money": result["money"]}, indent=1), flush=True)
    print("BENCH_JSON_BEGIN")
    print(json.dumps(result))
    print("BENCH_JSON_END", flush=True)


if __name__ == "__main__":
    main()
