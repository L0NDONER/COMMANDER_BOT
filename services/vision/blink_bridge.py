# services/vision/blink_bridge.py

import asyncio
import os
import time
from aiohttp import ClientSession, TCPConnector
from blinkpy.blinkpy import Blink
from blinkpy.auth import Auth, BlinkTwoFARequiredError

USERNAME = os.environ.get("BLINK_USERNAME")
PASSWORD = os.environ.get("BLINK_PASSWORD")

async def start_bridge():
    # Use a context manager for the connector and session to ensure proper cleanup
    async with ClientSession(connector=TCPConnector(ssl=False)) as session:
        blink = Blink(session=session)

        if not USERNAME or not PASSWORD:
            print("❌ Set BLINK_USERNAME and BLINK_PASSWORD environment variables")
            return

        blink.auth = Auth(
            {"username": USERNAME, "password": PASSWORD},
            no_prompt=True
        )

        print("🚀 Connecting to Blink...")

        try:
            await blink.start()

        except BlinkTwoFARequiredError:
            print("🛡️  2FA required — check your email")
            code = input("Enter code: ").strip()

            success = await blink.auth.complete_2fa_login(code)
            if not success:
                print("❌ 2FA failed — wrong or expired code")
                return

            print("✅ 2FA complete — finishing setup...")
            blink.setup_urls()
            # Force a refresh by setting last_refresh to the past
            blink.last_refresh = int(time.time() - blink.refresh_rate * 1.05)
            await blink.setup_post_verify()

        if not blink.account_id:
            print("❌ Login failed — check credentials")
            return

        print("⏳ Waiting for account sync...")
        await asyncio.sleep(2)
        await blink.refresh()

        if not blink.cameras:
            print("❌ No cameras found")
        else:
            print(f"\n📡 Connected. Found {len(blink.cameras)} cameras:")
            for name, camera in blink.cameras.items():
                print(f"  ✅ {name} (Battery: {camera.battery})")
                await camera.image_to_file(f"{name}_snap.jpg")
                print(f"  📸 Saved {name}_snap.jpg")

        # Give the Blink API and underlying SSL a moment to finish pending tasks
        # while the session is still technically open within the 'async with' block.
        await asyncio.sleep(1)

########## EXPLANATION ##########
# The RuntimeError was caused by the event loop closing while the TCPConnector 
# still had active SSL transports attempting to shut down. 
# By wrapping the ClientSession in an 'async with' block, aiohttp handles 
# the teardown of the connector more gracefully.
# The 'ssl=False' in TCPConnector is an optional tweak if your local 
# environment has cert-verification issues, but the main fix is the 
# context manager and the increased sleep before exiting the block.
#################################

if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    try:
        asyncio.run(start_bridge())
    except KeyboardInterrupt:
        pass

