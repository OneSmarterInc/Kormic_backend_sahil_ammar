"""Staging HTTP load runner; each token must belong to a DIFFERENT test student.

Uses real configured model calls. Explicit --allow-model-cost is required.
Never print tokens or responses. Tokens JSON: ["bearer token", ...].
"""
import argparse
import asyncio
import json
import statistics
import time
import uuid
from pathlib import Path
import httpx


async def main(args):
    tokens = json.loads(Path(args.tokens_file).read_text(encoding="utf-8"))
    if len(set(tokens)) < args.students:
        raise ValueError("Provide one distinct staging student token per conversation")
    gate = asyncio.Semaphore(args.concurrency)
    results = []
    async with httpx.AsyncClient(base_url=args.base_url.rstrip("/") + "/", timeout=30,
                                 limits=httpx.Limits(max_connections=args.concurrency + 10)) as client:
        async def conversation(token):
            async with gate:
                begin = time.perf_counter()
                headers = {"Authorization": "Bearer " + token, "Idempotency-Key": str(uuid.uuid4())}
                try:
                    response = await client.post("chat/agent/", headers=headers, json={"message": args.message})
                    if response.status_code not in (200, 202):
                        return {"status": response.status_code, "seconds": time.perf_counter() - begin}
                    job = response.json()
                    for _ in range(310):
                        if not job.get("job_id") or job.get("status") in ("completed", "failed"):
                            return {"status": job.get("status", "completed"), "seconds": time.perf_counter() - begin}
                        await asyncio.sleep(5)
                        response = await client.get(f"chat/jobs/{job['job_id']}/", headers=headers)
                        response.raise_for_status()
                        job = response.json()
                    return {"status": "timeout", "seconds": time.perf_counter() - begin}
                except Exception as exc:
                    return {"status": type(exc).__name__, "seconds": time.perf_counter() - begin}
        results = await asyncio.gather(*(conversation(token) for token in tokens[:args.students]))
    durations = sorted(result["seconds"] for result in results)
    counts = {}
    for result in results:
        key = str(result["status"])
        counts[key] = counts.get(key, 0) + 1
    print(json.dumps({"students": args.students, "concurrency": args.concurrency, "outcomes": counts,
                      "median_seconds": statistics.median(durations), "p95_seconds": durations[min(len(durations)-1, int(len(durations)*.95))]}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Staging API root ending /api/")
    parser.add_argument("--tokens-file", required=True)
    parser.add_argument("--students", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=100)
    parser.add_argument("--message", default="Briefly summarize my academic goals from my profile.")
    parser.add_argument("--allow-model-cost", action="store_true")
    options = parser.parse_args()
    if not options.allow_model_cost:
        parser.error("This runner consumes real model tokens; pass --allow-model-cost on staging only.")
    if options.students < 1 or options.concurrency < 1:
        parser.error("students and concurrency must be positive")
    asyncio.run(main(options))
