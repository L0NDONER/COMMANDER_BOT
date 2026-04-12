# services/garden/materials.py

DENSITY_MAP = {
    "light_green": 75,    # kg per m3 (branches, dry hedge)
    "heavy_green": 300,   # kg per m3 (wet grass, thick logs)
    "soil_turf": 1250,    # kg per m3 (heavy digging)
    "rubble": 1500        # kg per m3 (bricks/concrete)
}

RATE_MAP = {
    "light_green": 60,   # £ per tonne (branches, dry hedge)
    "heavy_green": 80,   # £ per tonne (wet grass, thick logs)
    "soil_turf":   90,   # £ per tonne (heavy digging)
    "rubble":      120,  # £ per tonne (bricks/concrete — licensed disposal)
}

MATERIAL_ALIASES = {
    "light": "light_green",
    "hedge": "light_green",
    "branches": "light_green",
    "grass": "heavy_green",
    "heavy": "heavy_green",
    "logs": "heavy_green",
    "soil": "soil_turf",
    "turf": "soil_turf",
    "rubble": "rubble",
    "bricks": "rubble",
    "concrete": "rubble",
}


def resolve_material(text: str) -> str:
    """Resolve a user-supplied material word to a DENSITY_MAP key."""
    text = text.lower().strip()
    if text in DENSITY_MAP:
        return text
    return MATERIAL_ALIASES.get(text, "heavy_green")


def estimate_weight(volume_m3: float, material_type: str) -> float:
    density = DENSITY_MAP.get(material_type, 200)
    return volume_m3 * density
