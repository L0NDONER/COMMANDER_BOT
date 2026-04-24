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

BRAND_TIPS = {
    "joe browns":       "Joe Browns 'Campervan' and 'Floral' prints sell 40% faster than plains. Check the label for the print name.",
    "barbour":          "Barbour Bedale and Beaufort wax jackets outsell all other styles. Rewaxed condition adds £10–£15.",
    "ralph lauren":     "Polo shirts in bold colours (yellow, purple, red) outsell white and navy. Include 'Classic Fit' or 'Slim Fit' in the title.",
    "stone island":     "Always photograph the badge clearly. Buyers pay a premium for intact badges — a missing one halves the price.",
    "cp company":       "The Goggle Jacket is the hero piece. Even worn condition sells fast. Lead with 'Goggle' in your title.",
    "fred perry":       "Twin-tipped collars in original colourways (laurel/red) outsell modern cuts. Mention the tipping colour.",
    "levi":             "501s in W30–W34 sell fastest. Always include waist and leg measurements — buyers are size-specific.",
    "hackett":          "Hackett rugby shirts outsell everything else in the range. Include the colourway and any sponsor branding.",
    "gant":             "Gant Rugger line outsells mainline. 'Rugger' in the title lifts visibility significantly.",
    "adidas":           "Vintage Trefoil logo outsells the Three Stripes modern range by 3x. Check inside the collar for era.",
    "nike":             "ACG and vintage 'Just Do It' era pieces command a premium. Check the swoosh style — pre-2000 labels sell faster.",
    "umbro":            "Diamond logo era (pre-2008) outsells modern Umbro heavily. Replica kits need the season and team in the title.",
    "burberry":         "Nova Check lining is the key detail buyers search for. Always mention it — even if it's just the cuffs.",
    "north face":       "700-fill and Gore-Tex jackets outsell fleece. Include the fill weight and any waterproof rating in the title.",
    "patagonia":        "Buyers are brand-loyal and eco-conscious. Mention 'Patagonia Repair Programme eligible' — it adds trust.",
    "carhartt":         "Workwear is peak 2026 trend. 'Double Knee' trousers and 'Detroit' jackets sell for 2x the price of standard hoodies.",
    "cos":              "The 'Quiet Luxury' king. Minimalist linen pieces and leather bags sell fastest. Include 'Minimalist' and 'Stealth Wealth' in tags.",
    "lucy & yak":       "High brand loyalty. Always include the specific print name (e.g., 'Sunflower') and the fit type (e.g., 'Original' vs. 'Alexa').",
    "stussy":           "8 ball and crown graphics are high-risk for fakes. If authentic, they sell in minutes. Lead with the graphic name.",
    "damson madder":    "The 'UK Ganni'. Leopard prints and oversized collars are viral on TikTok. Use 'Scandi-style' in your description.",
}


def get_brand_tip(query: str) -> str:
    query_lower = query.lower()
    match = next((k for k in BRAND_TIPS if k in query_lower), None)
    return f"🏷 <i>Merchant Tip: {BRAND_TIPS[match]}</i>" if match else ""


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
