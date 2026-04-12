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
