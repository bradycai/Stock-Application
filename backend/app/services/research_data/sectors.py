"""Pure SIC-code -> (sector, industry) mapping (GICS-like sector names). Unknown -> (None, None)."""

# (low, high inclusive, sector). First match wins, so narrower ranges come first.
_RANGES: list[tuple[int, int, str]] = [
    (100, 999, "Consumer Staples"),             # agriculture
    (1000, 1099, "Materials"),                  # metal mining
    (1200, 1399, "Energy"),                     # coal, oil & gas extraction
    (1400, 1499, "Materials"),
    (1500, 1799, "Industrials"),                # construction
    (2000, 2199, "Consumer Staples"),           # food, beverages, tobacco
    (2200, 2399, "Consumer Discretionary"),     # textiles, apparel
    (2400, 2499, "Materials"),
    (2500, 2599, "Consumer Discretionary"),     # furniture
    (2600, 2699, "Materials"),                  # paper
    (2700, 2799, "Communication Services"),     # publishing
    (2830, 2836, "Health Care"),                # pharma / biotech
    (2800, 2899, "Materials"),                  # chemicals
    (2900, 2999, "Energy"),                     # petroleum refining
    (3000, 3099, "Materials"),
    (3100, 3199, "Consumer Discretionary"),
    (3200, 3399, "Materials"),                  # stone/glass, primary metals
    (3400, 3499, "Industrials"),
    (3570, 3579, "Technology"),                 # computers
    (3500, 3599, "Industrials"),                # machinery
    (3600, 3699, "Technology"),                 # electronics, semiconductors
    (3710, 3719, "Consumer Discretionary"),     # motor vehicles
    (3700, 3799, "Industrials"),                # transport equipment
    (3800, 3851, "Health Care"),                # instruments, medical devices
    (3852, 3899, "Technology"),
    (3900, 3999, "Consumer Discretionary"),
    (4000, 4799, "Industrials"),                # rail, trucking, air, shipping
    (4800, 4899, "Communication Services"),
    (4900, 4999, "Utilities"),
    (5000, 5199, "Industrials"),                # wholesale
    (5400, 5499, "Consumer Staples"),           # food stores
    (5200, 5999, "Consumer Discretionary"),     # retail
    (6000, 6499, "Financials"),
    (6500, 6599, "Real Estate"),
    (6700, 6799, "Financials"),
    (7000, 7299, "Consumer Discretionary"),     # hotels, personal services
    (7370, 7379, "Technology"),                 # software / IT services
    (7300, 7399, "Industrials"),                # business services
    (7800, 7999, "Communication Services"),     # movies, entertainment
    (8000, 8099, "Health Care"),
    (8700, 8799, "Industrials"),
]

# Specific industries where a more useful label exists than the SIC description.
_INDUSTRY: dict[int, str] = {
    3674: "Semiconductors", 7372: "Software", 7370: "IT Services", 7371: "IT Services",
    7374: "IT Services", 3571: "Computer Hardware", 3577: "Computer Hardware",
    2834: "Pharmaceuticals", 2833: "Pharmaceuticals", 2836: "Biotechnology", 6021: "Banks",
    6022: "Banks", 6211: "Capital Markets", 1311: "Oil & Gas", 4911: "Electric Utilities",
}


def sector_from_sic(sic) -> tuple[str | None, str | None]:
    """Returns (sector, industry) for a SIC code, or (None, None) when unknown/invalid."""
    try:
        code = int(str(sic).strip())
    except (TypeError, ValueError):
        return None, None
    for low, high, sector in _RANGES:
        if low <= code <= high:
            return sector, _INDUSTRY.get(code)
    return None, None
