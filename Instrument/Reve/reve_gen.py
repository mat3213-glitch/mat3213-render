#!/usr/bin/env python3
"""
Reve Image Generation — Playwright-based API probe.
Авторизуется через bearer token, затем вызывает API через page.evaluate()
(из браузерного контекста — обходим CORS, используем точный формат браузера).

Usage:
  export REVE_BEARER_TOKEN="your-jwt-token-here"
  python3 reve_gen.py
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

BEARER_TOKEN = os.environ.get("REVE_BEARER_TOKEN", "")
PROMPT = os.environ.get("REVE_PROMPT", "foggy forest at dawn, cinematic, desaturated, no people")
OUT_DIR = Path(__file__).parent
TIMEOUT_S = 90


async def main():
    if not BEARER_TOKEN:
        sys.exit("ERROR: REVE_BEARER_TOKEN not set")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        )
        page = await context.new_page()

        # Перехват всех API-ответов для диагностики
        captured_responses = {}
        async def on_response(resp):
            if "/api/" in resp.url:
                try:
                    body = await resp.text()
                    captured_responses[resp.url] = {"status": resp.status, "body": body[:3000]}
                    print(f"  [RESP] {resp.status} {resp.url[:100]}")
                except Exception:
                    pass
        page.on("response", on_response)

        # Открываем сайт и ставим токен
        print("Step 1: Navigate + set token")
        await page.goto("https://app.reve.com")
        await page.evaluate(f"() => localStorage.setItem('reve:bearer_token', '{BEARER_TOKEN}')")
        await page.goto("https://app.reve.com")
        await page.wait_for_timeout(4000)

        # Читаем user info + project_id из localStorage
        print("Step 2: Get user info")
        user_info_raw = await page.evaluate("() => localStorage.getItem('reve:user_info')")
        project_id = None
        if user_info_raw:
            try:
                ui = json.loads(user_info_raw)
                user = ui.get("user", {})
                project_id = user.get("default_project", "")
                print(f"  User: {user.get('name')} | project: {project_id}")
            except Exception as e:
                print(f"  Parse error: {e}")

        if not project_id:
            # fallback — попробуем вытащить из захваченных ответов
            for url, resp in captured_responses.items():
                if "/api/misc/userinfo" in url and resp["status"] == 200:
                    try:
                        d = json.loads(resp["body"])
                        project_id = d.get("user", {}).get("default_project", "")
                        if project_id:
                            print(f"  Got project_id from userinfo response: {project_id}")
                            break
                    except Exception:
                        pass

        # Делаем API-вызовы из браузерного контекста через fetch()
        print(f"\nStep 3: Probe node structure (project_id={project_id})")

        async def fetch_js(method, path, payload=None):
            body_js = f"JSON.stringify({json.dumps(payload)})" if payload else "undefined"
            js = f"""
async () => {{
    const opts = {{
        method: '{method}',
        headers: {{
            'Authorization': 'Bearer {BEARER_TOKEN}',
            'Content-Type': 'application/json',
        }},
    }};
    if ({body_js} !== undefined) opts.body = {body_js};
    const resp = await fetch('https://app.reve.com{path}', opts);
    const text = await resp.text();
    return {{ status: resp.status, body: text.slice(0, 4000) }};
}}
"""
            r = await page.evaluate(js)
            print(f"  {method} {path} → {r['status']}: {r['body'][:200]}")
            return r

        results = []

        # Получаем список существующих нод
        print("\n  GET /node?props=all")
        r = await fetch_js("GET", f"/api/project/{project_id}/node?props=all")
        results.append({"step": "list_nodes", "status": r["status"], "body": r["body"]})

        node_id = None
        if r["status"] == 200:
            try:
                nodes = json.loads(r["body"])
                if isinstance(nodes, list) and nodes:
                    node_id = nodes[0].get("id") or nodes[0].get("node_id")
                    print(f"  Found existing node: {node_id}")
                    print(f"  Node keys: {list(nodes[0].keys())}")
            except Exception as e:
                print(f"  Parse error: {e}")

        # Создаём новую ноду (пробуем разные форматы)
        if not node_id:
            print("\n  POST /node — create node")
            for node_payload in [
                {"type": "image_gen", "position": {"x": 0, "y": 0}},
                {"kind": "image_gen"},
                {"node_type": "image_gen"},
                {},
            ]:
                r = await fetch_js("POST", f"/api/project/{project_id}/node", node_payload)
                results.append({"step": "create_node", "payload": node_payload, "status": r["status"], "body": r["body"]})
                if r["status"] in (200, 201):
                    try:
                        d = json.loads(r["body"])
                        node_id = d.get("id") or d.get("node_id") or d.get("item", {}).get("id")
                        print(f"  ✅ Node created: {node_id}")
                    except Exception:
                        pass
                    break

        # Генерация
        print(f"\n  POST /generation (node={node_id})")
        gen_payloads = []
        if node_id:
            gen_payloads = [
                {"node": node_id, "data": {"prompt": PROMPT}},
                {"node": node_id, "data": {"prompt": PROMPT, "model": "reve-2.0", "width": 1024, "height": 1024}},
                {"node": node_id, "data": {"prompt": PROMPT}, "model_id": "reve-2.0"},
            ]
        gen_payloads.append({"data": {"prompt": PROMPT}})

        for gp in gen_payloads:
            r = await fetch_js("POST", f"/api/project/{project_id}/generation", gp)
            results.append({"step": "generate", "payload": gp, "status": r["status"], "body": r["body"]})
            if r["status"] == 200:
                print(f"  ✅ Generation started!")
                break

        # Polling если нашли generation_id
        for res in results:
            if res.get("status") == 200 and res.get("step") == "generate":
                try:
                    d = json.loads(res["body"])
                    gen_id = d.get("id") or d.get("generation_id") or d.get("item", {}).get("id")
                    if gen_id:
                        print(f"\n  Polling generation {gen_id}...")
                        for _ in range(15):
                            await page.wait_for_timeout(4000)
                            rp = await fetch_js("GET", f"/api/project/{project_id}/generation/{gen_id}")
                            results.append({"step": "poll", "status": rp["status"], "body": rp["body"]})
                            if rp["status"] == 200:
                                pd = json.loads(rp["body"])
                                status = pd.get("item", {}).get("status", pd.get("status", "?"))
                                print(f"  status: {status}")
                                if status in ("completed", "done", "success"):
                                    break
                except Exception as e:
                    print(f"  Polling error: {e}")

        # Сохраняем всё
        probe_path = OUT_DIR / "reve_captured_payload.json"
        probe_path.write_text(json.dumps(results, indent=2, ensure_ascii=False))
        print(f"\nSaved probe results → {probe_path}")

        resp_path = OUT_DIR / "reve_captured_responses.json"
        resp_path.write_text(json.dumps(captured_responses, indent=2, ensure_ascii=False))
        print(f"Saved captured responses → {resp_path}")

        # Если нашли 200 — пробуем скачать изображение
        for r in results:
            if r["status"] == 200:
                try:
                    data = json.loads(r["body"])
                    print(f"\nSuccessful response data: {json.dumps(data, indent=2)[:1000]}")
                except Exception:
                    pass

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
