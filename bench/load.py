"""Load test for Fern. Runs inside the cluster against a copy of the API that uses the simulated bank.

Each virtual user (VU) gets its own space, takes a $10,000 payroll, and then loops over a mix of
reads and money movements: views, P2P, withdrawals, pulls, and a share of idempotent replays.
After the load stops, every VU checks that the money is exactly right:

  * the books balance (trial balance: debits = credits, balances = postings);
  * the available balance equals what the VU expects from the requests that succeeded;
  * no available balance went negative;
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
MIX = [
    (40, "GET /me"),
    (15, "GET /transfers"),
    (15, "POST /p2p"),
    (12, "POST /withdrawals"),
    (12, "POST /pulls"),
    (6, "replay"),
]


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
            # Available balance: P2P and withdrawals leave at once (held, then cashed out); a pull
            # only becomes spendable after it settles, which the test never does, so it adds 0.
            self.expected += min(sign, 0) * body["amount_minor"]
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
            if r is None or r.status_code >= 500:
                pass  # a failed request, already counted as an error; not evidence of a duplicate
            elif r.status_code >= 300 or r.json().get("id") != tid:
                self.replay_mismatches += 1

    async def get_json(self, name: str, path: str) -> dict:
        # Verification reads are retried: a transient error here must not hide the result.
        for attempt in range(4):
            r = await self.call(name, "GET", path)
            if r is not None and r.status_code == 200:
                return r.json()
            await asyncio.sleep(1 + attempt)
        raise RuntimeError(f"{path} kept failing")

    async def verify(self) -> dict:
        # No settlement here: the simulated bank keeps state per pod, and settling is a demo
        # action, not part of the load. The books and the available balance must be exact anyway.
        tb = await self.get_json("verify trial balance", "/ledger/trial-balance")
        me = await self.get_json("verify /me", "/me")
        mine = next(c for c in me["customers"] if c["id"] == self.me)
        return {
            "books_balance": tb["debits_equal_credits"] and tb["balances_match_postings"],
            "balance_ok": mine["available_minor"] == self.expected,
            "non_negative": all(c["available_minor"] >= 0 for c in me["customers"]),
            "replay_mismatches": self.replay_mismatches,
            "transfers": len(self.keys),
            "diff": mine["available_minor"] - self.expected,
        }


def share(n: int, i: int, parts: int) -> int:
    """Worker i's part of n, so that the parts add up to n exactly."""
    return n * (i + 1) // parts - n * i // parts


async def drive(args: argparse.Namespace, stages: list[tuple[int, int]], i: int, parts: int, barrier) -> dict:
    """One load-generator process: its share of the VUs, on the same wall-clock schedule as the others."""
    peak = share(max(v for _, v in stages), i, parts)
    limits = httpx.Limits(max_connections=peak + 20, max_keepalive_connections=peak + 20)
    async with httpx.AsyncClient(base_url=args.base, timeout=30, limits=limits) as client:
        vus = [VU(client, Stats()) for _ in range(peak)]
        sem = asyncio.Semaphore(20)

        async def setup(v: VU) -> None:
            async with sem:
                await v.setup()

        await asyncio.gather(*(setup(v) for v in vus))
        await asyncio.to_thread(barrier.wait)  # start together

        stats = Stats()
        for v in vus:
            v.stats = stats
        active = 0
        total = sum(d for d, _ in stages)
        stop_at = time.monotonic() + total

        async def loop(v: VU, index: int) -> None:
            while time.monotonic() < stop_at:
                if index >= active:
                    await asyncio.sleep(0.2)
                    continue
                await v.step()
                await asyncio.sleep(random.uniform(0, args.think))

        tasks = [asyncio.create_task(loop(v, k)) for k, v in enumerate(vus)]
        for duration, n in stages:
            active = share(n, i, parts)
            if i == 0:
                print(f"stage: {n} VUs for {duration}s", flush=True)
            end = time.monotonic() + duration
            while time.monotonic() < end:
                stats.vus_per_sec[int(time.monotonic() - stats.t0)] = active
                await asyncio.sleep(0.5)
        await asyncio.gather(*tasks)

        verify_sem = asyncio.Semaphore(10)

        async def verify(v: VU) -> dict:
            v.stats = Stats()  # keep verification out of the load numbers
            async with verify_sem:
                return await v.verify()

        checks = await asyncio.gather(*(verify(v) for v in vus))
    return {
        "per_sec": dict(stats.per_sec),
        "errors_per_sec": dict(stats.errors_per_sec),
        "vus_per_sec": stats.vus_per_sec,
        "by_endpoint": dict(stats.by_endpoint),
        "status": dict(stats.status),
        "checks": checks,
        "rejected": sum(v.rejected for v in vus),
    }


def worker(args: argparse.Namespace, stages: list[tuple[int, int]], i: int, parts: int, barrier, out) -> None:
    out.put((i, asyncio.run(drive(args, stages, i, parts, barrier))))


def run(args: argparse.Namespace) -> dict:
    import multiprocessing as mp

    stages = [tuple(int(x) for x in s.split(":")) for s in args.stages.split(",")]
    peak = max(v for _, v in stages)
    total = sum(d for d, _ in stages)
    parts = args.procs
    ctx = mp.get_context("fork")
    barrier, out = ctx.Barrier(parts), ctx.Queue()
    print(f"setting up {peak} spaces in {parts} load processes", flush=True)
    procs = [ctx.Process(target=worker, args=(args, stages, i, parts, barrier, out)) for i in range(parts)]
    for p in procs:
        p.start()
    parts_out = [out.get()[1] for _ in procs]
    for p in procs:
        p.join()
    print("verified money", flush=True)

    stats = Stats()
    checks: list[dict] = []
    rejected = 0
    for w in parts_out:
        for s, xs in w["per_sec"].items():
            stats.per_sec[s].extend(xs)
        for s, n in w["errors_per_sec"].items():
            stats.errors_per_sec[s] += n
        for s, n in w["vus_per_sec"].items():
            stats.vus_per_sec[s] = stats.vus_per_sec.get(s, 0) + n
        for name, xs in w["by_endpoint"].items():
            stats.by_endpoint[name].extend(xs)
        for code, n in w["status"].items():
            stats.status[code] += n
        checks += w["checks"]
        rejected += w["rejected"]

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

    # Per stage, skipping its first 10 s while the new VUs ramp in.
    stage_results, start = [], 0
    for duration, n in stages:
        secs_in = [s for s in range(start + min(10, duration // 3), start + duration) if s in stats.per_sec]
        ms = [x for s in secs_in for x in stats.per_sec[s]]
        stage_results.append(
            {
                "vus": n,
                "seconds": duration,
                "rps": round(len(ms) / max(1, len(secs_in)), 1),
                "p50_ms": pct(ms, 50),
                "p95_ms": pct(ms, 95),
                "p99_ms": pct(ms, 99),
                "errors": sum(stats.errors_per_sec.get(s, 0) for s in secs_in),
            }
        )
        start += duration
    # Capacity: the busiest stage that still met the latency objective with no errors.
    ok = [s for s in stage_results if s["p95_ms"] <= args.slo_ms and s["errors"] == 0]
    best = max(ok, key=lambda s: s["rps"]) if ok else min(stage_results, key=lambda s: s["p95_ms"])
    return {
        "run_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "method": {
            "where": "k8s Job inside the EKS cluster, against the fern-bench Deployment",
            "bank": "simulated (in-process, Column-like contract); the Column sandbox is not load tested",
            "database": args.database,
            "stages": [{"seconds": d, "vus": n} for d, n in stages],
            "think_time_s": [0, args.think],
            "mix": {n: w for w, n in MIX},
            "api_replicas": args.replicas,
            "load_processes": args.procs,
            "slo_p95_ms": args.slo_ms,
            "note": args.note,
        },
        "headline": {
            "peak_rps_10s": round(max(rolling), 1),
            # "Sustained" = capacity: the busiest stage that kept p95 within the SLO, no errors.
            "sustained_rps": best["rps"],
            "sustained_p95_ms": best["p95_ms"],
            "sustained_vus": best["vus"],
            "peak_vus": peak,
            "requests": len(all_ms),
            "error_rate": round(sum(stats.errors_per_sec.values()) / max(1, len(all_ms)), 5),
            "p50_ms": pct(all_ms, 50),
            "p95_ms": pct(all_ms, 95),
            "p99_ms": pct(all_ms, 99),
        },
        "stages": stage_results,
        "series": series,
        "endpoints": [
            {"name": n, "count": len(xs), "p50": pct(xs, 50), "p95": pct(xs, 95), "p99": pct(xs, 99)}
            for n, xs in sorted(stats.by_endpoint.items(), key=lambda kv: -len(kv[1]))
        ],
        "status_codes": dict(stats.status),
        "money": {
            "spaces": len(checks),
            "transfers": sum(c["transfers"] for c in checks),
            "rejected_by_rules": rejected,
            "books_balance": all(c["books_balance"] for c in checks),
            "balances_exact": sum(c["balance_ok"] for c in checks),
            "non_negative": all(c["non_negative"] for c in checks),
            "duplicate_transfers": sum(c["replay_mismatches"] for c in checks),
            "money_errors": sum(
                (not c["books_balance"]) + (not c["balance_ok"]) + (not c["non_negative"]) + c["replay_mismatches"]
                for c in checks
            ),
            "sample_diffs": [c["diff"] for c in checks if c["diff"]][:10],
        },
    }


def wait_ready(base: str, seconds: int = 600) -> None:
    """The Job starts before the API copies are up (make bench), so wait for /healthz first."""
    end = time.monotonic() + seconds
    while True:
        try:
            if httpx.get(f"{base}/healthz", timeout=5).status_code == 200:
                time.sleep(20)  # let the rest of the replicas come up behind the first one
                return
        except httpx.HTTPError:
            pass
        if time.monotonic() > end:
            raise SystemExit(f"{base} not ready after {seconds}s")
        time.sleep(3)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://fern-bench.fern.svc")
    p.add_argument("--stages", default="60:25,60:50,60:100,60:150,60:200")
    p.add_argument("--think", type=float, default=1.0, help="max think time between steps, seconds")
    p.add_argument("--replicas", type=int, default=2)
    p.add_argument("--procs", type=int, default=4, help="load-generator processes")
    p.add_argument("--slo-ms", type=float, default=500, help="p95 latency objective for capacity")
    p.add_argument("--note", default="", help="free text for the method section, e.g. the bottleneck")
    p.add_argument("--database", default="the same RDS instance, separate database fern_bench")
    p.add_argument("--out", default="/tmp/bench.json")
    args = p.parse_args()
    wait_ready(args.base)
    result = run(args)
    with open(args.out, "w") as f:
        json.dump(result, f, indent=1)
    print(json.dumps({"headline": result["headline"], "money": result["money"]}, indent=1), flush=True)
    print("BENCH_JSON_BEGIN")
    print(json.dumps(result))
    print("BENCH_JSON_END", flush=True)


if __name__ == "__main__":
    main()
