# Commander Bot

A personal Telegram bot ("Minty") that combines network operations, eBay reselling tools, Betfair trading, and garden clearance quoting into a single chat interface.

---

## Features

### Network Ops
| Command | Description |
|---|---|
| `ping <host>` | ICMP ping test |
| `ssh <host>` | SSH connectivity check |
| `tailping <node>` | Tailscale mesh ping |
| `trace <host>` | Traceroute (8 hops) |
| `tailscale` | Tailnet node status |
| `exitip` | Current public IP |

### eBay Scout
Send `scout <item> [£price]` to get a Vinted resale verdict using live eBay pricing.

```
scout barbour jacket XXL £8
scout levi 501 jeans W32
```

Returns: eBay median, Vinted list price, fast-sale price, profit, ROI, and a ready-to-paste Vinted title/description.

Type `brands` for the Norfolk charity shop cheat sheet.

### Local Scout
Daemon that monitors eBay local listings against your watchlist and fires Telegram alerts when a deal meets your margin/profit thresholds.

Watches: Netgear, Synology, Ubiquiti, Makita/DeWalt power tools, Sony cameras, Marantz hi-fi, Sky boxes.

```bash
python3 services/local_scout/telegram_local_scout.py           # run once
python3 services/local_scout/telegram_local_scout.py --daemon  # poll every N mins
```

### Garden Clearance Vision
Send a **photo** to the bot with a spade or fork visible as scale reference.

Caption format: `garden <material>`

| Material | Examples |
|---|---|
| `hedge` / `light_green` | Branches, dry hedge |
| `grass` / `heavy_green` | Wet grass, thick logs |
| `soil` / `turf` | Heavy digging |
| `rubble` / `bricks` | Concrete, masonry |

Returns: estimated volume, weight, number of van runs, and a full job quote broken down by disposal, labour, and margin.

### Betfair
`betfair <command>` — integrates with the Betfair exchange via Telegram.

Standalone traders also available:
- `services/betfair_telegram/trader.py` — Over 2.5 Goals lay trader
- `services/betfair_telegram/trader_updated.py` — Over 3.5 Goals lay trader

### AI Fallback
Any unrecognised message is handled by Minty, powered by Groq (Llama 3.3 70B). Responds to infrastructure questions with command suggestions, or just chats naturally.

---

## Setup

### Requirements
```bash
pip3 install -r requirements.txt
```

### credentials.py
Create this file locally — **never commit it**.

```python
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID   = ""
ALLOWED_CHAT_IDS   = [""]
GROQ_API_KEY       = ""
GROQ_MODEL         = "llama-3.3-70b-versatile"
EBAY_APP_ID        = ""
EBAY_SECRET        = ""
GEMINI_API_KEY     = ""
```

### Run
```bash
python3 telegram_app.py
```

### Run as a systemd service (EC2 / server)
```ini
[Unit]
Description=Commander Telegram Bot
After=network.target

[Service]
ExecStart=/usr/bin/python3 /home/ubuntu/commander/telegram_app.py
WorkingDirectory=/home/ubuntu/commander
Restart=always
User=ubuntu

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable commander
sudo systemctl start commander
```

---

## Project Structure

```
commander/
├── telegram_app.py        # bot entry point
├── telegram_config.py     # command registry
├── actions.py             # command handlers
├── safety_belt.py         # error handling wrapper
├── credentials.py         # secrets (not in git)
├── requirements.txt
└── services/
    ├── ebay/              # eBay scout + Vinted pricing
    ├── betfair_telegram/  # Betfair exchange integration
    ├── local_scout/       # automated eBay deal alerts
    └── garden/            # vision-based clearance quoting
```
