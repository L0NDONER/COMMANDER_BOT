# services/ebay/brands.py

STRONG_BRANDS = {
    # Heritage / British
    "stone island",
    "cp company",
    "barbour",
    "burberry",
    "aquascutum",
    "schoffel",
    "belstaff",
    "jaeger",
    "mulberry",
    # Smart casual / lifestyle
    "reiss",
    "gant",
    "ralph lauren",
    "hackett",
    "hackett london",
    "ted baker",
    "allsaints",
    "whistles",
    "boden",
    "mint velvet",
    "seasalt cornwall",
    "joules",
    "fat face",
    "hobbs",
    "lk bennett",
    "phase eight",
    "white stuff",
    # Sportswear / performance
    "nike",
    "adidas",
    "lacoste",
    "fred perry",
    "lululemon",
    "new balance",
    "on running",
    "salomon",
    "helly hansen",
    "columbia",
    "timberland",
    "berghaus",
    "napapijri",
    "the north face",
    "patagonia",
    "arc'teryx",
    # Streetwear
    "stüssy",
    "carhartt",
    "dickies",
    "palace",
    "represent",
    "champion",
    "fila",
    "ellesse",
    "kappa",
    # Footwear
    "dr martens",
    # Premium / designer
    "levis",
    "tommy hilfiger",
    "hugo boss",
    "calvin klein",
    "canada goose",
    "moncler",
}

HIGH_VALUE_KEYWORDS = {
    "retro", "vintage", "1980s", "1990s", "90s", "80s",
    "jvc", "umbro", "admiral", "bukta", "o2", "sega", "dreamcast",
    "match worn", "player issue", "lextra",
}

HIGH_VALUE_THRESHOLD = 35.0


def get_high_value_alert(query: str, median_price: float) -> str:
    query_lower = query.lower()
    match = next((k for k in HIGH_VALUE_KEYWORDS if k in query_lower), None)
    if match and median_price > HIGH_VALUE_THRESHOLD:
        return (
            f"🚨 <b>HIGH-VALUE ASSET DETECTED</b> 🚨\n"
            f"<i>Trigger: '{match.upper()}'</i>\n"
            f"Do not list on Vinted — use eBay with a reserve price."
        )
    return ""


SLOW_KEYWORDS = {
    "homeware",
    "plate",
    "mug",
    "bowl",
    "denby",
    "portmeirion",
    "lego",
}

BRANDS_MSG = """
👕 *Charity Shop Cheat Sheet — Norfolk*

🔥 *Always grab*
  Stone Island, CP Company
  Barbour (jackets), Schöffel
  Burberry, Aquascutum
  Musto, Henri Lloyd, Gill (sailing)

✅ *Solid earners*
  Levi's (501s, trucker jackets)
  Ralph Lauren, Lacoste, Fred Perry
  Wrangler (jackets especially)
  Adidas vintage (trefoil logo)
  Nike ACG / vintage Nike
  Dr Martens, Scarpa, Meindl boots

🏠 *Homeware — Holt/Burnham*
  Denby, Hornsea, Portmeirion
  Emma Bridgewater, Cath Kidston
  Pyrex (pattern matters!)
  Zeiss/Swarovski binoculars

📦 *Other*
  Lego (any — bulk, Technic, Star Wars)
  Vintage cameras (Pentax, Olympus)
  Fountain pens (Parker, Waterman)
  Complete vintage board games
  Vintage band tees (tour dates on back)

💡 *Tip: scout [item] [£price] for instant lookup*
"""


def handle_brands() -> str:
    return BRANDS_MSG
