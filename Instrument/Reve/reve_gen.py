#!/usr/bin/env python3
"""
Reve Image Generation Script (Playwright-based)
Uses browser automation to generate an image via app.reve.com

Usage:
  export REVE_BEARER_TOKEN="your-jwt-token-here"
  python3 reve_gen.py

How to get your token:
  1. Open app.reve.com in browser, login
  2. Open DevTools → Application → Local Storage → app.reve.com
  3. Copy the value of "reve:bearer_token"
  4. Set it as REVE_BEARER_TOKEN env variable
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

BEARER_TOKEN = os.environ.get("REVE_BEARER_TOKEN", "")
PROMPT = "foggy forest at dawn, cinematic, desaturated, no people"
OUTPUT_FILE = Path(__file__).parent / "reve_test_output.jpg"
TIMEOUT_S = 120

captured_chat_payload = None
captured_generation_data = None


async def main():
    if not BEARER_TOKEN:
        print("ERROR: REVE_BEARER_TOKEN environment variable is not set.")
        print()
        print("To get your token:")
        print("  1. Open app.reve.com in your browser and login")
        print("  2. Open DevTools → Application → Local Storage → app.reve.com")
        print("  3. Copy the value of 'reve:bearer_token'")
        print("  4. Export it: export REVE_BEARER_TOKEN='your-token'")
        sys.exit(1)

    start_time = time.time()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

        page = await context.new_page()

        captured_requests = []
        captured_responses = {}

        async def handle_response(response):
            url = response.url
            if "/api/misc/chat" in url or "/api/proto/model_infer" in url or "/api/project/" in url:
                try:
                    body = await response.text()
                    captured_responses[url] = {
                        "status": response.status,
                        "body": body[:5000]
                    }
                    print(f"  [RESPONSE] {response.status} {url[:80]}")
                except Exception:
                    pass

        async def handle_request(request):
            url = request.url
            if "/api/misc/chat" in url or "/api/proto/model_infer" in url:
                print(f"  [REQUEST] {request.method} {url}")
                if request.method == "POST":
                    post_data = request.post_data
                    if post_data:
                        captured_requests.append({
                            "url": url,
                            "method": request.method,
                            "headers": dict(request.headers),
                            "body": post_data[:5000]
                        })
                        print(f"  [PAYLOAD] {post_data[:500]}")

        page.on("response", handle_response)
        page.on("request", handle_request)

        print(f"Step 1: Setting auth token and navigating to app.reve.com...")
        await page.goto("https://app.reve.com")

        await page.evaluate(f"""() => {{
            localStorage.setItem('reve:bearer_token', '{BEARER_TOKEN}');
        }}""")

        print("Step 2: Reloading page with token...")
        await page.goto("https://app.reve.com")
        await page.wait_for_timeout(3000)

        print("Step 3: Checking auth status...")
        try:
            user_info = await page.evaluate("""() => {
                return localStorage.getItem('reve:user_info');
            }""")
            if user_info:
                info = json.loads(user_info)
                user = info.get("user", {})
                print(f"  Logged in as: {user.get('name', 'unknown')} ({user.get('email', 'unknown')})")
                project_id = user.get("default_project", "")
                print(f"  Default project: {project_id}")
            else:
                print("  WARNING: No user info found. Token may be invalid.")
                print("  Trying to proceed anyway...")
        except Exception as e:
            print(f"  Warning reading user info: {e}")

        print(f"Step 4: Waiting for editor to load...")
        await page.wait_for_timeout(5000)

        print(f"Step 5: Looking for prompt input field...")
        prompt_input = None
        selectors = [
            'textarea',
            'input[type="text"]',
            '[placeholder*="prompt" i]',
            '[placeholder*="Describe" i]',
            '[placeholder*="Type" i]',
            '[contenteditable="true"]',
            'rv-text-field textarea',
        ]
        for sel in selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    prompt_input = el
                    print(f"  Found input with selector: {sel}")
                    break
            except Exception:
                continue

        if not prompt_input:
            print("  Could not find prompt input. Taking screenshot for debugging...")
            screenshot_path = Path(__file__).parent / "reve_debug_screenshot.png"
            await page.screenshot(path=str(screenshot_path))
            print(f"  Screenshot saved: {screenshot_path}")

            print("\n  Attempting to navigate to editor directly...")
            await page.goto("https://app.reve.com/editor")
            await page.wait_for_timeout(5000)
            screenshot_path2 = Path(__file__).parent / "reve_debug_screenshot2.png"
            await page.screenshot(path=str(screenshot_path2))
            print(f"  Screenshot saved: {screenshot_path2}")

            for sel in selectors:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        prompt_input = el
                        print(f"  Found input with selector: {sel}")
                        break
                except Exception:
                    continue

        if prompt_input:
            print(f"Step 6: Typing prompt: '{PROMPT}'")
            await prompt_input.click()
            await prompt_input.fill(PROMPT)
            await page.wait_for_timeout(500)

            print("Step 7: Submitting prompt (Enter key)...")
            await prompt_input.press("Enter")

            print(f"Step 8: Waiting for generation (timeout {TIMEOUT_S}s)...")
            gen_start = time.time()
            image_found = False

            while time.time() - gen_start < TIMEOUT_S:
                await page.wait_for_timeout(2000)
                elapsed = int(time.time() - gen_start)

                if captured_responses:
                    print(f"  [{elapsed}s] Captured {len(captured_responses)} API responses")

                if captured_requests:
                    print(f"  [{elapsed}s] Captured {len(captured_requests)} API requests")

                if elapsed % 10 == 0 and elapsed > 0:
                    print(f"  [{elapsed}s] Still waiting...")

                for url, resp in captured_responses.items():
                    if "/generation/" in url or "/image/" in url:
                        try:
                            data = json.loads(resp["body"])
                            if "item" in data or "data" in data:
                                print(f"  [{elapsed}s] Got generation/image data!")
                                captured_generation_data = data
                                image_found = True
                                break
                        except json.JSONDecodeError:
                            pass

                if image_found:
                    break

                try:
                    imgs = await page.query_selector_all('img[src*="image"], img[src*="generation"], canvas')
                    if imgs:
                        print(f"  [{elapsed}s] Found {len(imgs)} image elements on page")
                except Exception:
                    pass

            if captured_requests:
                print("\n=== Captured Chat Payload ===")
                for req in captured_requests:
                    print(f"URL: {req['url']}")
                    print(f"Headers: {json.dumps({k:v for k,v in req['headers'].items() if k.lower() in ['authorization', 'content-type', 'cookie']}, indent=2)}")
                    try:
                        body_parsed = json.loads(req['body'])
                        print(f"Body:\n{json.dumps(body_parsed, indent=2)[:2000]}")
                    except json.JSONDecodeError:
                        print(f"Body (raw): {req['body'][:1000]}")
                    print()

                payload_path = Path(__file__).parent / "reve_captured_payload.json"
                with open(payload_path, "w") as f:
                    json.dump(captured_requests, f, indent=2, default=str)
                print(f"Payload saved: {payload_path}")

            if captured_responses:
                resp_path = Path(__file__).parent / "reve_captured_responses.json"
                with open(resp_path, "w") as f:
                    json.dump(captured_responses, f, indent=2, default=str)
                print(f"Responses saved: {resp_path}")

            print("\nStep 9: Trying to download generated image...")
            downloaded = False

            try:
                images = await page.query_selector_all('img')
                for img in images:
                    src = await img.get_attribute('src')
                    if src and ('/image/' in src or '/generation/' in src or 'blob:' in src):
                        print(f"  Found image: {src[:120]}")
                        if src.startswith('http'):
                            resp = await page.request.get(src)
                            if resp.ok:
                                body = await resp.body()
                                with open(OUTPUT_FILE, 'wb') as f:
                                    f.write(body)
                                downloaded = True
                                print(f"  Downloaded: {OUTPUT_FILE} ({len(body)} bytes)")
                                break
                        elif src.startswith('blob:'):
                            print("  Blob URL detected - extracting via canvas...")
                            try:
                                data_url = await page.evaluate("""async (src) => {
                                    const resp = await fetch(src);
                                    const blob = await resp.blob();
                                    return new Promise((resolve) => {
                                        const reader = new FileReader();
                                        reader.onloadend = () => resolve(reader.result);
                                        reader.readAsDataURL(blob);
                                    });
                                }""", src)
                                import base64
                                header, encoded = data_url.split(',', 1)
                                img_data = base64.b64decode(encoded)
                                with open(OUTPUT_FILE, 'wb') as f:
                                    f.write(img_data)
                                downloaded = True
                                print(f"  Downloaded from blob: {OUTPUT_FILE} ({len(img_data)} bytes)")
                                break
                            except Exception as e:
                                print(f"  Failed to extract blob: {e}")
            except Exception as e:
                print(f"  Error finding images: {e}")

            if not downloaded:
                print("\n  Could not automatically download image.")
                print("  Taking final screenshot...")
                screenshot_path = Path(__file__).parent / "reve_final_screenshot.png"
                await page.screenshot(path=str(screenshot_path), full_page=True)
                print(f"  Screenshot saved: {screenshot_path}")

                print("\n  Page content snippet:")
                content = await page.content()
                print(f"  HTML length: {len(content)} chars")

        else:
            print("\nERROR: Could not find prompt input field on the page.")
            print("The site may have changed its UI structure.")

        elapsed_total = time.time() - start_time
        print(f"\n=== Summary ===")
        print(f"Total time: {elapsed_total:.1f}s")
        print(f"Output file: {OUTPUT_FILE}")
        if OUTPUT_FILE.exists():
            size = OUTPUT_FILE.stat().st_size
            print(f"File size: {size:,} bytes ({size/1024:.1f} KB)")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
