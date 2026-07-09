"""Predefined ticker universes for "Universe Mode" — pick a theme instead of typing
every symbol. These are static reference lists; the run still caps at MAX_WATCHLIST_SIZE.
"""

UNIVERSES: dict[str, list[str]] = {
    "Mega-cap tech": [
        "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "AVGO", "TSLA", "ORCL", "NFLX",
    ],
    "AI infrastructure": [
        "NVDA", "AVGO", "AMD", "SMCI", "DELL", "ANET", "VRT", "MRVL", "MU", "CRWV",
    ],
    "Semiconductors": [
        "NVDA", "AMD", "AVGO", "MU", "MRVL", "TSM", "ASML", "AMAT", "LRCX", "KLAC",
        "INTC", "QCOM",
    ],
    "Nuclear/power": [
        "OKLO", "SMR", "CEG", "VST", "NRG", "TLN", "BWXT", "LEU", "CCJ", "NNE",
    ],
    "Quantum/speculative": [
        "IONQ", "RGTI", "QBTS", "QUBT", "ARQQ",
    ],
    "Space": [
        "RKLB", "LUNR", "ASTS", "PL", "RDW", "SPCE",
    ],
    "Crypto miners": [
        "MARA", "RIOT", "CLSK", "CIFR", "WULF", "HUT", "BITF", "IREN",
    ],
    "Small-cap momentum": [
        "SOFI", "PLTR", "HOOD", "AFRM", "UPST", "DKNG", "RKLB", "IONQ",
    ],
}


def universe_names() -> list[dict]:
    return [{"name": name, "count": len(syms)} for name, syms in UNIVERSES.items()]


def get_universe(name: str) -> list[str]:
    return list(UNIVERSES.get(name, []))
