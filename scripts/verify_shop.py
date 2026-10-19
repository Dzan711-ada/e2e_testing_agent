"""Independent real-browser oracle for stage 1; never invokes an Agent."""
import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from e2e_agent.config import Settings
from e2e_agent.sample_shop import shop_server


class ScriptedResources:
    """Fixed fictional values and local fake-SMS delivery; no console or remote service."""
    def __init__(self, store):
        self.store = store
        self.requests = []

    def get(self, kind, challenge=None):
        self.requests.append(kind)
        return {"username": "stage_user", "password": "TestPass123!", "phone": "15500000001"}.get(kind) if kind != "sms_code" else self.store.test_code(challenge)


CASES = [
    ("normal-round-1", (), "chain", "passed"),
    ("normal-round-2", (), "chain", "passed"),
    ("wrong-password-rejected", (), "negative", "passed"),
    ("correct-password-rejected", ("valid_login_denied",), "chain", "failed"),
    ("wrong-password-accepted", ("invalid_password_accepted",), "negative", "failed"),
    ("wrong-cart-quantity", ("cart_quantity_wrong",), "chain", "failed"),
    ("cart-refresh-loss", ("cart_lost_on_refresh",), "chain", "failed"),
    ("simulated-sms-chain", (), "sms", "passed"),
]


async def verify(output):
    settings = Settings.load()
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(settings.browsers_path)
    from playwright.async_api import async_playwright, expect
    report = {"stage": 1, "model_calls": 0, "scenarios": [], "status": "running"}
    with shop_server() as (base, store):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chromium", headless=settings.headless)
            try:
                for identity, faults, kind, expected in CASES:
                    ns = uuid.uuid4().hex[:16]
                    store.prepare(ns, faults)
                    prefix = f"{base}/shop/{ns}"
                    context = await browser.new_context()
                    page = await context.new_page()
                    page.set_default_timeout(1500)
                    resources = ScriptedResources(store)
                    checkpoints = []
                    actual, detail = "passed", ""
                    try:
                        if kind == "sms":
                            await page.goto(prefix + "/sms")
                            await page.get_by_label("Phone", exact=True).fill(resources.get("phone"))
                            await page.get_by_role("button", name="Send code", exact=True).click()
                            challenge = await page.get_by_test_id("challenge").input_value()
                            await page.get_by_label("Code", exact=True).fill(resources.get("sms_code", challenge))
                            await page.get_by_role("button", name="Verify code", exact=True).click()
                        else:
                            await page.goto(prefix + "/register")
                            await page.get_by_label("Username", exact=True).fill(resources.get("username"))
                            await page.get_by_label("Password", exact=True).fill(resources.get("password"))
                            await page.get_by_role("button", name="Register account", exact=True).click()
                            await expect(page.get_by_test_id("message")).to_have_text("Registration successful")
                            checkpoints.append({"id": "registration", "status": "passed"})
                            assert store.stats(ns)["login_count"] == 0
                            await page.goto(prefix + "/login")
                            await page.get_by_label("Username", exact=True).fill(resources.get("username"))
                            await page.get_by_label("Password", exact=True).fill("Wrong123!" if kind == "negative" else resources.get("password"))
                            await page.get_by_role("button", name="Log in", exact=True).click()
                        if kind == "negative":
                            await expect(page.get_by_test_id("message")).to_have_text("Invalid credentials")
                            await expect(page.get_by_test_id("identity")).to_have_count(0)
                            assert store.stats(ns)["login_count"] == 0
                            checkpoints.append({"id": "password-rejection", "status": "passed"})
                        else:
                            await expect(page.get_by_test_id("identity")).to_be_visible()
                            assert store.stats(ns)["login_count"] == 1
                            checkpoints.append({"id": "login", "status": "passed"})
                            await page.get_by_role("button", name="Add to cart", exact=True).click()
                            await expect(page.get_by_test_id("cart-book")).to_contain_text("Agent Handbook")
                            await expect(page.get_by_test_id("quantity")).to_have_text("1")
                            checkpoints.append({"id": "cart", "status": "passed"})
                            await page.reload()
                            await expect(page.get_by_test_id("quantity")).to_have_text("1")
                            assert store.stats(ns)["login_count"] == 1
                            checkpoints.append({"id": "refresh", "status": "passed"})
                            if kind == "sms":
                                assert store.stats(ns)["send_count"] == 1
                                assert resources.requests.count("sms_code") == 1
                    except AssertionError as error:
                        actual, detail = "failed", str(error)
                        checkpoints.append({"id": "business-assertion", "status": "failed"})
                    except Exception as error:
                        actual, detail = "error", f"{type(error).__name__}: {error}"
                    finally:
                        await page.screenshot(path=str(output / (identity + ".png")), full_page=True)
                        url = page.url
                        await context.close()
                    report["scenarios"].append({"id": identity, "namespace": ns, "expected": expected, "actual": actual,
                                                "checkpoints": checkpoints, "stats": store.stats(ns), "url": url,
                                                "resource_requests": resources.requests, "detail": detail,
                                                "screenshot": str(output / (identity + ".png"))})
                    print(identity + ": expected=" + expected + ", actual=" + actual, flush=True)
                    if actual != expected:
                        report["status"] = "failed"
                        break
                else:
                    assert len({s["namespace"] for s in report["scenarios"]}) == len(CASES)
                    report["status"] = "passed"
            finally:
                await browser.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    report = asyncio.run(verify(args.output))
    (args.output / "shop-validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
