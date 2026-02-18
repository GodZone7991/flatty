#!/usr/bin/env python3
"""
Buyer Profile - Search criteria, preferences, and location config
for the property alert service.
"""

# ---------------------------------------------------------------------------
# Budget & Hard Filters (applied in scraper before any LLM call)
# ---------------------------------------------------------------------------

PRICE_MIN = 100_000   # EUR
PRICE_MAX = 300_000   # EUR
SIZE_MIN_M2 = 50
ROOMS_MIN = 2

# ---------------------------------------------------------------------------
# Blacklisted Zones (filtered out BEFORE agent evaluation — saves tokens)
# ---------------------------------------------------------------------------

BLACKLISTED_ZONES = [
    "Raval", "El Raval",
    "Poble Sec", "Poble-sec", "El Poble Sec", "El Poble-sec",
    "Gótico", "Gòtic", "Barri Gòtic", "Barrio Gótico",
]

# ---------------------------------------------------------------------------
# Search Configurations per City
# ---------------------------------------------------------------------------

SEARCH_CONFIGS = [
    # Barcelona
    {
        "city": "barcelona",
        "label": "Barcelona",
        "preferred_zones": [
            "Gràcia",
            "Gracia",
            "Les Corts",
            "Sarrià-Sant Gervasi",
            "Sarrià",
            "Sarria",
            "Eixample",
            "Poblenou",
            "Sant Martí",
            "Sants-Montjuïc",
            "Sants",
        ],
        "pisos_com": {
            "base_url": "https://www.pisos.com/venta/pisos-barcelona_capital/",
            "params": {
                "precio_desde": PRICE_MIN,
                "precio_hasta": PRICE_MAX,
                "metros_desde": SIZE_MIN_M2,
                "habitaciones_desde": ROOMS_MIN,
            },
        },
        "fotocasa": {
            "base_url": "https://www.fotocasa.es/es/comprar/viviendas/barcelona-capital/todas-las-zonas/l",
            "params": {
                "minPrice": PRICE_MIN,
                "maxPrice": PRICE_MAX,
                "minRooms": ROOMS_MIN,
                "minSurface": SIZE_MIN_M2,
            },
        },
        "idealista": {
            "center": "41.3874,2.1686",
            "distance": 5000,  # meters — city center only
            "country": "es",
            "operation": "sale",
            "propertyType": "homes",
            "locale": "es",
        },
    },
    # Madrid
    {
        "city": "madrid",
        "label": "Madrid",
        "preferred_zones": [
            "Tetuán",
            "Tetuan",
            "Chamberí",
            "Chamberi",
            "Salamanca",
            "Retiro",
            "Moncloa-Aravaca",
            "Moncloa",
            "Arganzuela",
        ],
        "pisos_com": {
            "base_url": "https://www.pisos.com/venta/pisos-madrid_capital/",
            "params": {
                "precio_desde": PRICE_MIN,
                "precio_hasta": PRICE_MAX,
                "metros_desde": SIZE_MIN_M2,
                "habitaciones_desde": ROOMS_MIN,
            },
        },
        "fotocasa": {
            "base_url": "https://www.fotocasa.es/es/comprar/viviendas/madrid-capital/todas-las-zonas/l",
            "params": {
                "minPrice": PRICE_MIN,
                "maxPrice": PRICE_MAX,
                "minRooms": ROOMS_MIN,
                "minSurface": SIZE_MIN_M2,
            },
        },
        "idealista": {
            "center": "40.4168,-3.7038",
            "distance": 5000,  # meters — city center only
            "country": "es",
            "operation": "sale",
            "propertyType": "homes",
            "locale": "es",
        },
    },
]

# ---------------------------------------------------------------------------
# Buyer Profile (fed to LLM agents)
# ---------------------------------------------------------------------------

BUYER_PROFILE = """
## Buyer Profile

### Financial Situation
- Pre-approved 100% mortgage at 2-3% fixed interest, 30-year term
- Savings: ~40,000 EUR (covers transaction costs + minor renovations)
- Budget: 100,000 - 300,000 EUR purchase price
- Transaction costs (ITP, notary, registro, gestoría) must fit within savings

### Strategy
- Will NOT move in immediately. Plan to rent out 1 room (at referencia de precio / rental cap) for 1-2 years
- Already has a tenant lined up
- Will stay registered at the address (empadronamiento)
- Must comply with Spanish rental price cap (índice de referencia de precios)
- Monthly mortgage payment must be coverable by rental income + small personal contribution
- Long-term: primary residence after 1-2 years of partial rental

### Requirements
- Minimum 50 m², minimum 2 bedrooms (need separate room for tenant)
- Property type: Apartment (piso)
- Must be legally rentable (cédula de habitabilidad, proper registration)

### Preferred Locations
- **Barcelona** (priority order): Gràcia, Les Corts, Sarrià-Sant Gervasi, Eixample, Poblenou, Sant Martí, Sants-Montjuïc
- **Madrid** (priority order): Tetuán, Chamberí, Salamanca, Retiro, Moncloa-Aravaca, Arganzuela

### Red Flags (automatic rejection)
- Bank repossessions with unclear legal status
- Ongoing litigation or occupancy issues (okupas)
- Failed ITE (Inspección Técnica de Edificios)
- No elevator above 3rd floor (planta 3+)
- Energy certificate F or G with no renovation budget left
- Communities with unpaid special assessments (derramas)
- Properties listed as "se vende con inquilino" (sold with tenant)
"""

# ---------------------------------------------------------------------------
# Agent-specific context
# ---------------------------------------------------------------------------

FINANCIAL_CONTEXT = """
## Spanish Property Market Context (2025-2026)
- Average price/m² Barcelona: ~3,800 EUR/m² (varies widely by district)
- Average price/m² Madrid: ~4,200 EUR/m² (varies widely by district)
- Typical gross rental yield Barcelona: 4.5-6%
- Typical gross rental yield Madrid: 4-5.5%
- Mortgage rates (variable): Euribor + 0.8-1.5%
- Mortgage rates (fixed): 2.5-3.5%
- Buyer transaction costs: ~10-12% of purchase price (ITP 10%, notary, registration, gestoría)
- Non-resident buyers: may face stricter mortgage conditions
"""

LEGAL_CONTEXT = """
## Spanish Property Legal Context
- ITP (Impuesto de Transmisiones Patrimoniales): 10% in Catalonia, 6% in Madrid for resale
- IVA + AJD: 10% + 1.5% for new builds
- Nota Simple: essential document from Registro de la Propiedad showing ownership, charges, liens
- ITE (Inspección Técnica de Edificios): mandatory for buildings >45 years in Barcelona, >30 in Madrid
- Cédula de habitabilidad: required in Catalonia for sale/rental
- Certificado energético: mandatory for all sales
- Community of owners (comunidad de propietarios): check for derramas (special assessments)
- Okupas risk: higher in certain neighborhoods, check building security
"""

LOCATION_CONTEXT = """
## Barcelona Neighborhoods (within budget, priority order)
- Gràcia: TOP PRIORITY. Bohemian, walkable, young professionals, great cafés/restaurants. 3,200-4,500 EUR/m².
- Les Corts: Quiet residential, good schools, FC Barcelona area, family-friendly. 3,500-4,800 EUR/m².
- Sarrià-Sant Gervasi: Upscale, green, best schools in Barcelona, safe. 4,000-6,000 EUR/m². May be tight on budget.
- Eixample: Well-connected, high demand, older buildings, classic Barcelona. 3,500-5,000 EUR/m².
- Poblenou: Tech hub (22@), renovated industrial, growing fast. 3,000-4,200 EUR/m².
- Sants-Montjuïc: More affordable, good transport, mixed quality. 2,500-3,500 EUR/m².

## Madrid Neighborhoods (within budget, priority order)
- Tetuán: TOP PRIORITY. Gentrifying fast, close to Chamberí/Salamanca, still affordable. Great upside. 2,800-4,000 EUR/m².
- Chamberí: Central, traditional, well-connected. 3,800-5,500 EUR/m².
- Salamanca: Premium, may only find small/studio at budget. 4,500-7,000 EUR/m².
- Retiro: Residential, park access, solid demand. 3,500-5,000 EUR/m².
- Moncloa-Aravaca: University area, good transport. 3,000-4,500 EUR/m².
- Arganzuela: Up-and-coming, Madrid Río, affordable. 2,800-3,800 EUR/m².
"""
