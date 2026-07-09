"""Batch orchestration for Claude analysis.

Pure (no DB/broker/LLM) so it's unit-testable: it chunks the candidate list, runs an
injected `analyze_fn` per batch, isolates batch failures (a malformed/failed batch is
counted and skipped, never crashes the run), merges results, dedupes by ticker keeping
the highest confidence, and caps the final count.
"""
import logging

logger = logging.getLogger("trading.batch")


def chunk(items: list, size: int) -> list[list]:
    """Split a list into consecutive chunks of at most `size`."""
    if size < 1:
        size = 1
    return [items[i : i + size] for i in range(0, len(items), size)]


def run_batched(symbols, batch_size, analyze_fn, dedupe_fn, max_final):
    """Run `analyze_fn(batch_symbols) -> list[intent]` over batches.

    analyze_fn may raise (or be made to raise on a failed/malformed batch); the exception
    is caught, counted in `failed_batches`, and the run continues. Returns
    (merged_intents, stats).
    """
    batches = chunk(list(symbols), batch_size)
    collected = []
    failed = 0
    for i, batch in enumerate(batches):
        try:
            result = analyze_fn(batch) or []
            collected.extend(result)
        except Exception as e:  # noqa: BLE001
            failed += 1
            logger.warning("Batch %d (%s) failed and was skipped: %s", i, batch, e)

    merged = dedupe_fn(collected)
    if max_final is not None:
        merged = merged[:max_final]

    stats = {
        "batches": len(batches),
        "failed_batches": failed,
        "merged_count": len(merged),
    }
    return merged, stats
