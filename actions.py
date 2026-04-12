# actions.py
import subprocess
import os
import requests
from services.ebay.handler import handle_scout_command
from services.ebay.brands import handle_brands as _handle_brands

# -------------------------------
# Helpers
# -------------------------------

def run(cmd, timeout=5):
    return subprocess.check_output(
        cmd,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        text=True
    ).strip()


# -------------------------------
# Hint / Guidance Handlers
# -------------------------------

def handle_ssh_hint():
    return "🔑 Use: `ssh <host>` to test SSH connectivity"

def handle_ping_hint():
    return "📡 Use: `ping <host>` to test reachability"

def handle_tailping_hint():
    return "🌀 Use: `tailping <node>` to test Tailscale connectivity"


# -------------------------------
# Prefix Command Handlers
# -------------------------------

def handle_ping_command(target):
    try:
        out = run(["ping", "-c", "2", "-W", "2", target], timeout=5)
        return f"📡 Ping OK\n{out.splitlines()[-1]}"
    except Exception as e:
        return f"❌ Ping failed: {target}"

def handle_ssh_command(host):
    try:
        run([
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=3",
            host,
            "echo ok"
        ], timeout=5)
        return f"🔑 SSH OK: {host}"
    except Exception:
        return f"❌ SSH failed: {host}"

def handle_tailping_command(node):
    try:
        out = run(["tailscale", "ping", "--c=3", node], timeout=6)
        return f"🌀 Tailscale ping OK\n{out.splitlines()[-1]}"
    except Exception:
        return f"❌ Tailscale ping failed: {node}"

def handle_trace_command(target):
    try:
        out = run(["traceroute", "-m", "8", target], timeout=10)
        lines = out.splitlines()[:6]
        return "🧭 Trace:\n" + "\n".join(lines)
    except Exception:
        return f"❌ Trace failed: {target}"


# -------------------------------
# Status / Info Handlers
# -------------------------------

def handle_tailscale_status():
    try:
        out = run(["tailscale", "status"], timeout=5)
        lines = out.splitlines()
        online = [l for l in lines if "idle" in l or "active" in l]
        return f"🌀 Tailnet OK\nNodes online: {len(online)}"
    except Exception:
        return "❌ Tailscale not running"

def handle_exitip_command():
    try:
        ip = requests.get("https://api.ipify.org", timeout=4).text.strip()
        return f"🌍 Exit IP: {ip}"
    except Exception:
        return "❌ Unable to fetch public IP"


# -------------------------------
# eBay Scout Service
# -------------------------------

def handle_scout(message):
    return handle_scout_command(message)

def handle_brands():
    return _handle_brands()

# -------------------------------
# Betfair Service
# -------------------------------

def handle_betfair(arg):
    # 1. Was: services.betfair_telegram.betfair_service
    from services.betfair_telegram.bt_services.betfair.betfair_service import handle
    
    # 2. Was: bt_services.telegram.telegram_client
    from services.betfair_telegram.bt_services.telegram.telegram_client import TelegramClient
    
    # 3. Was: import telegram_config (This one is likely config.py inside betfair_telegram)
    from services.betfair_telegram import config as cfg

    try:
        import credentials
        token   = credentials.TELEGRAM_BOT_TOKEN
        chat_id = credentials.TELEGRAM_CHAT_ID
    except ImportError:
        import os
        token   = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")

    telegram = TelegramClient(token)
    handle(cfg, telegram, chat_id, f"betfair {arg}")
    return None  # betfair sends its own messages directly
