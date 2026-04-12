# config.py (NETOPS edition)
import os

import actions

# Base Paths
BASE_DIR = os.path.expanduser('~/ansible')
INVENTORY_PATH = os.path.join(BASE_DIR, 'inventory')
VAULT_PASS_FILE = os.path.join(BASE_DIR, '.vault_pass')

# --- COMMAND REGISTRIES ---
# Keyword Commands: maps a command "concept" to (handler, trigger words)
KEYWORD_COMMANDS = {
    'tailscale': (actions.handle_tailscale_status, ['tailscale', 'tailnet', 'tunnel', 'vpn', 'ts']),
    'tailping':  (actions.handle_tailping_hint,    ['tailping', 'ts ping', 'tailscale ping']),
    'ping':      (actions.handle_ping_hint,        ['ping', 'reachable', 'can you reach', 'connectivity']),
    'ssh':       (actions.handle_ssh_hint,         ['ssh', 'login', 'connect to', 'port 22']),
    'exitip':    (actions.handle_exitip_command,   ['exit', 'exit node', 'public ip', 'egress', 'what ip']),
    'brands':    (actions.handle_brands,           ['brands', 'guide', 'cheatsheet']),
}

# Prefix Commands: require arguments after the command word
PREFIX_COMMANDS = {
    'ssh':      actions.handle_ssh_command,
    'ping':     actions.handle_ping_command,
    'tailping': actions.handle_tailping_command,
    'trace':    actions.handle_trace_command,
    'scout':    actions.handle_scout,
    'betfair':  actions.handle_betfair,
}

# Secrets
try:
    import credentials as secrets
    ALLOWED_CHAT_IDS = getattr(secrets, "ALLOWED_CHAT_IDS", [])
    GROQ_API_KEY       = getattr(secrets, "GROQ_API_KEY", None)
    TELEGRAM_BOT_TOKEN = getattr(secrets, "TELEGRAM_BOT_TOKEN", None)
    TELEGRAM_CHAT_ID   = getattr(secrets, "TELEGRAM_CHAT_ID", None)
    GEMINI_API_KEY     = getattr(secrets, "GEMINI_API_KEY", None)
except (ImportError, AttributeError):
    ALLOWED_CHAT_IDS = os.getenv('ALLOWED_TELEGRAM_CHAT_IDS', '').split(',')
    GROQ_API_KEY     = os.getenv('GROQ_API_KEY')
    GEMINI_API_KEY   = os.getenv('GEMINI_API_KEY')

# eBay API
EBAY_APP_ID = os.getenv('EBAY_APP_ID')
EBAY_SECRET = os.getenv('EBAY_SECRET')

# Optional debug toggle
DEBUG_MODE = os.getenv('DEBUG_MODE', '0') in ('1', 'true', 'True', 'yes', 'YES')
