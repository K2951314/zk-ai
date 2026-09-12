"""End-to-end smoke test for ZK-AI against the local mock upstream.

Zero real API spend: every upstream call lands on ``scripts/mock_upstream.py``.

Run the stack first::

    python scripts/mock_upstream.py --port 8099
    MOCK_KEY_01=... MOCK_KEY_02=... MOCK_KEY_03=... \
        NO_PROXY=127.0.0.1,localhost \
        python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

then::

    python scripts/smoke_test.py
"""

from __future__ import annotations

import sys

import httpx

BASE = "http://127.0.0.1:8000"
#: Throwaway token for the local mock run (see config/config.yaml); not a secret.
ADMIN_TOKEN = "local-admin-token"  # noqa: S105
ADMIN = {"X-Admin-Token": ADMIN_TOKEN}

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    label = "PASS" if ok else "FAIL"
    print(f"[{label}] {name}" + (f"  -- {detail}" if detail else ""), flush=True)


def main() -> int:
    # trust_env=False: the machine running this test sits behind an HTTP proxy.
    with httpx.Client(base_url=BASE, timeout=30.0, trust_env=False) as client:
        # ------------------------------------------------------------------
        # 1. /health
        # ------------------------------------------------------------------
        r = client.get("/health")
        body = r.json()
        record(
            "GET /health",
            r.status_code == 200 and "status" in body,
            f"{r.status_code} status={body.get('status')} db={body.get('database', {}).get('ok')}",
        )

        # ------------------------------------------------------------------
        # 2. /v1/models  (models + aliases)
        # ------------------------------------------------------------------
        r = client.get("/v1/models")
        models = [m["id"] for m in r.json().get("data", [])]
        aliases = [m for m in models if m.startswith("zk-")]
        record(
            "GET /v1/models",
            r.status_code == 200 and "mock-general" in models and len(aliases) >= 9,
            f"{len(models)} entries, {len(aliases)} aliases",
        )

        # ------------------------------------------------------------------
        # 3. Non-streaming completion through an alias
        # ------------------------------------------------------------------
        r = client.post(
            "/v1/chat/completions",
            json={
                "model": "zk-coding",
                "messages": [{"role": "user", "content": "def add(a, b): return"}],
            },
        )
        payload = r.json()
        meta = payload.get("zk_ai", {})
        record(
            "POST /v1/chat/completions (model=zk-coding)",
            r.status_code == 200 and bool(payload.get("choices")),
            f"{r.status_code} provider={r.headers.get('x-zkai-provider')} "
            f"model={r.headers.get('x-zkai-model')} attempt={r.headers.get('x-zkai-attempt')}",
        )
        record(
            "routing metadata is attached",
            bool(meta.get("routing_reason")) and meta.get("alias") == "zk-coding",
            f"fallback_used={meta.get('fallback_used')}",
        )
        record(
            "capability router picked the coding model",
            meta.get("resolved_model") == "mock-smart",
            f"resolved_model={meta.get('resolved_model')}",
        )

        # ------------------------------------------------------------------
        # 4. Streaming (SSE)
        # ------------------------------------------------------------------
        chunks = 0
        comments = 0
        done = False
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "zk-auto",
                "messages": [{"role": "user", "content": "stream please"}],
                "stream": True,
            },
        ) as response:
            record("streaming returns SSE", response.status_code == 200
                   and response.headers.get("content-type", "").startswith("text/event-stream"),
                   f"{response.status_code} {response.headers.get('content-type')}")
            for line in response.iter_lines():
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        done = True
                    else:
                        chunks += 1
                elif line.startswith(":"):
                    comments += 1
        record(
            "SSE shape (chunks + [DONE] + routing comment)",
            chunks >= 3 and done and comments >= 1,
            f"{chunks} chunks, {comments} comment(s), done={done}",
        )

        # ------------------------------------------------------------------
        # 5. A 400 is the caller's fault: returned as-is, no key burned.
        # ------------------------------------------------------------------
        before = client.get("/admin/credentials", headers=ADMIN)
        failures_before = sum(c["failure_count"] for c in before.json()["data"])
        r = client.post(
            "/v1/chat/completions",
            json={"model": "zk-bad", "messages": [{"role": "user", "content": "hi"}]},
        )
        record(
            "upstream 400 is mirrored to the client",
            r.status_code == 400,
            f"{r.status_code} type={r.json().get('error', {}).get('type')}",
        )
        after = client.get("/admin/credentials", headers=ADMIN)
        failures_after = sum(c["failure_count"] for c in after.json()["data"])
        record(
            "400 does NOT rotate keys (no credential penalised)",
            failures_after == failures_before,
            f"failure_count {failures_before} -> {failures_after}",
        )

        # ------------------------------------------------------------------
        # 6. Fault injection: always-429 upstream -> cooldown, rotation, 429 out
        # ------------------------------------------------------------------
        r = client.post(
            "/v1/chat/completions",
            json={"model": "zk-flaky", "messages": [{"role": "user", "content": "hi"}]},
        )
        record(
            "429 upstream exhausts keys and returns 429 (not 401)",
            r.status_code == 429,
            f"{r.status_code} type={r.json().get('error', {}).get('type')}",
        )
        after = client.get("/admin/credentials", headers=ADMIN)
        states = {c["id"]: c["status"] for c in after.json()["data"]}
        cooled = sorted(k for k, v in states.items() if v == "cooldown")
        record(
            "throttled keys entered COOLDOWN (rotation happened)",
            len(cooled) >= 2,
            f"cooldown={cooled} all={states}",
        )

        # ------------------------------------------------------------------
        # 7. Admin surface
        # ------------------------------------------------------------------
        r = client.get("/admin/router/preview", params={"model": "zk-vision"}, headers=ADMIN)
        preview = r.json()
        record(
            "GET /admin/router/preview",
            r.status_code == 200 and preview.get("plan"),
            f"{r.status_code} order={' -> '.join(p['model'] for p in preview.get('plan', []))}"
            f" strategy={preview.get('strategy')}",
        )
        r = client.get("/admin/stats", headers=ADMIN)
        stats = r.json()
        record(
            "GET /admin/stats",
            r.status_code == 200 and stats.get("pool", {}).get("total") == 3,
            f"{r.status_code} keys={stats.get('pool', {}).get('total')} "
            f"requests={stats.get('pool', {}).get('requests')}",
        )
        r = client.get("/admin/credentials", headers={"X-Admin-Token": "wrong"})
        record("admin requires a valid token", r.status_code in {401, 403}, f"{r.status_code}")
        r = client.get("/admin/health", headers=ADMIN)
        record(
            "GET /admin/health",
            r.status_code == 200 and r.json().get("database", {}).get("ok") is True,
            f"{r.status_code} providers_available="
            f"{r.json().get('summary', {}).get('providers_available')}",
        )

        # ------------------------------------------------------------------
        # 8. Persistence: usage accounting landed in SQLite
        # ------------------------------------------------------------------
        r = client.get("/admin/stats", params={"recent": 10}, headers=ADMIN)
        rows = r.json().get("recent_requests", [])
        statuses = {row["status"] for row in rows}
        record(
            "requests are persisted with the right statuses",
            r.status_code == 200
            and statuses == {"success", "error"}
            and sum(1 for row in rows if row["status"] == "success") >= 2,
            f"{len(rows)} recent rows, statuses={sorted(statuses)}",
        )
        r = client.get("/admin/models", headers=ADMIN)
        record(
            "GET /admin/models",
            r.status_code == 200 and len(r.json().get("data", [])) >= 5,
            f"{r.status_code} models={len(r.json().get('data', []))}",
        )

    # ------------------------------------------------------------------
    print()
    failed = [name for name, ok, _ in results if not ok]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("failed: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
