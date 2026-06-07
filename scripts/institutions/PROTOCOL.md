This protocol is offered freely. Its structure, vocabulary, and geometry originate from Martin O'Flaherty.

---

# Institution Mapping Protocol

Public institutions — hospitals, GP surgeries, council depots, public markets — are infrastructure.
Their geometry belongs in the commons. Any courier, any system, any country's equivalent may consume it freely.

## Licences

| Layer | Licence | Scope |
|---|---|---|
| `institutions/public/*.json` | CC0 1.0 Universal | Data — no rights reserved |
| `../../*.py` | AGPL-3.0 | Code |

## File naming

`<short_name_snake_case>.json` — e.g. `norfolk_norwich_university_hospital.json`

## Schema

### Top-level fields

| Field | Type | Description |
|---|---|---|
| `_licence` | string | Human-readable licence statement |
| `_source` | string | Data provenance — OSM way/relation IDs, NHS pages, NaPTAN |
| `_surveyed` | string (ISO date) | Date record was created or last ground-truthed |
| `_osm_way` | integer | Primary OSM way ID for the campus polygon |
| `_osm_relation` | integer | OSM relation ID if campus is a multipolygon (optional) |
| `_cqc_location` | string | CQC location reference (optional) |
| `name` | string | Full legal name |
| `short_name` | string | Common abbreviation used in manifests |
| `nhs_trust` | string | Full NHS trust name |
| `type` | string | `hospital` / `gp_surgery` / `council_depot` / `public_market` |
| `subtype` | string | e.g. `major_teaching`, `district_general` |
| `country` | string | ISO 3166-1 alpha-2 |
| `region` | string | |
| `icb` | string | Integrated Care Board (NHS England) |

### `address`

| Field | Description |
|---|---|
| `street` | Primary street |
| `locality` | Village / suburb |
| `town` | Town or city |
| `county` | County |
| `postcode_delivery` | Postcode for delivery / satnav |
| `postcode_campus` | Campus postcode (may differ) |

### `centroid`

`{ "lat": float, "lon": float }` — WGS84 decimal degrees.

### `bounding_box`

`{ "south": float, "north": float, "west": float, "east": float }` — WGS84.

### `road_access`

Free-text fields describing approach routes by direction. `notes` captures anything relevant to vehicle routing.

### `entrances[]`

Each entrance is an object:

| Field | Description |
|---|---|
| `id` | Snake-case identifier |
| `label` | Human-readable name |
| `type` | `vehicle_and_pedestrian` / `pedestrian` / `ae_and_ambulance` / `ae_reference` / `delivery_and_pedestrian` / `dropoff` |
| `lat`, `lon` | WGS84 |
| `coord_source` | How the coord was derived — OSM node ref, inferred, ground survey |
| `notes` | Anything useful for routing or approach |

### `car_parks[]`

| Field | Description |
|---|---|
| `id` | Snake-case identifier |
| `label` | Display name |
| `lat`, `lon` | WGS84 centroid |
| `osm_way` | OSM way ID |
| `ref` | Letter/number reference (e.g. `A`, `G`) if signed on site |
| `access` | `public` / `permit` / `private` / `customers` / `unknown` |
| `capacity` | Total spaces (integer) |
| `capacity_disabled` | Disabled spaces (integer or `true` if present but uncounted) |
| `fee` | boolean |
| `charge` | Human-readable tariff string |
| `maxstay` | e.g. `30 minutes` |
| `opening_hours` | OSM opening_hours format or free text |
| `surface` | `asphalt` / `gravel` / etc. |
| `notes` | Routing or access notes |

### `bus_stops[]`

| Field | Description |
|---|---|
| `naptan` | NaPTAN AtcoCode |
| `label` | Stop name and indicator |
| `lat`, `lon` | WGS84 |
| `bearing` | Compass bearing of travel |
| `street` | Street name |
| `landmark` | NaPTAN landmark (optional) |
| `shelter` | boolean |
| `operators` | Array of operator names |
| `plusbus_zone` | PlusBus zone code |
| `notes` | |

### `defibrillators[]`

| Field | Description |
|---|---|
| `lat`, `lon` | WGS84 |
| `location` | Plain-English location description |
| `indoor` | boolean |
| `opening_hours` | |
| `osm_ref` | The Circuit GUID if known |

### `internal_junctions[]`

Turning circles, mini roundabouts, and other features relevant to van routing.

| Field | Description |
|---|---|
| `type` | `mini_roundabout` / `turning_circle` / `give_way` |
| `lat`, `lon` | WGS84 |
| `notes` | |

### `delivery`

| Field | Description |
|---|---|
| `preferred_vehicle_entry` | `id` of preferred entrance for delivery vehicles |
| `loading_bay` | `id` of entrance nearest loading bay (optional) |
| `loading_bay_notes` | |
| `ae_dropoff` | `id` of A&E entrance (optional) |
| `throat_on_approach` | Description of any throat constraint, or `null` |
| `internal_road` | Name of primary internal road |
| `internal_road_notes` | Routing notes |
| `turning_available` | boolean |
| `turning_notes` | |
| `notes` | Anything else relevant |

### `contacts`

Free key/value pairs — `switchboard`, `email`, `website`, `ae_web`, `fax`, etc.

---

## coord_source vocabulary

Use one of these values in `coord_source` so consumers know what to trust:

| Value | Meaning |
|---|---|
| `OSM <type> node <id or coords>` | Derived directly from an OSM element |
| `OSM way centroid` | Centre of an OSM way polygon — less precise |
| `NaPTAN` | From the National Public Transport Access Node dataset |
| `postcodes.io` | Postcode centroid from postcodes.io API |
| `Ground survey` | Physically verified on site |
| `Inferred — <reason>` | Calculated or estimated; reason given |

---

## Contributing

Ground-truth beats inference. If you survey a site, update `_surveyed` and change `coord_source` from `Inferred` to `Ground survey`.

Contributed geometry for public institutions should be submitted under CC0.

---

*Inception: 2026-04-12 10:17:26 BST*
