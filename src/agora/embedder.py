"""The embedding pipeline's pure core (agora-0137): chunking and the
standing reconcile. The thread, client and lifecycle wrap these; keeping
the diff computation pure is what makes crash-recovery testable — the
work set is DERIVED state, never a stored queue (ops cycle 2: any stored
queue desyncs from truth exactly when a SIGKILL interrupts it; hashes
stored WITH the vectors are crash-safe by construction).

Chunking (retrieval cycle-2 sweep, measured): docs > CHUNK_THRESHOLD
chars split into CHUNK_SIZE windows with CHUNK_OVERLAP; the winning
config put a 59.5k-char log message's tail query at rank 1 vs 19 for
1800/300. Windows are code-point based (retrieval P3: byte windows can
split a codepoint; Python str slicing is code-point safe by nature).
Every chunk carries the WHOLE-input hash — edits invalidate atomically.
"""

from __future__ import annotations

from typing import Any

CHUNK_THRESHOLD = 2000
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 200


def chunk_text(title: str, text: str) -> list[str]:
    """The embed inputs for one doc: title rides EVERY chunk (362
    title-only docs measured; and a chunk without its title loses the
    topical anchor). Returns [single] for small docs."""
    title = title or ""
    text = text or ""
    whole = f"{title}\n{text}" if title else text
    if len(whole) <= CHUNK_THRESHOLD:
        return [whole]
    step = CHUNK_SIZE - CHUNK_OVERLAP
    chunks = []
    for start in range(0, len(text), step):
        window = text[start:start + CHUNK_SIZE]
        if not window:
            break
        chunks.append(f"{title}\n{window}" if title else window)
        if start + CHUNK_SIZE >= len(text):
            break
    return chunks or [whole[:CHUNK_SIZE]]


def reconcile(docs: dict[tuple[str, str, str], dict[str, Any]],
              vec_hashes: dict[tuple[str, str, str], str],
              vec_chunk_counts: dict[tuple[str, str, str], int],
              *, active_model: str,
              models_present: set[str],
              pending_model: str | None = None) -> dict[str, Any]:
    """The standing 4-prong sweep (ops cycle 3: attached to rebuild() it
    never fires after `agora restore`; standing, it heals ANY divergence
    between the corpus and the vector store, whatever caused it).

    Inputs are snapshots: `docs` from search_docs (key -> {text_hash,
    title, text}), `vec_hashes`/`vec_chunk_counts` from the vector store
    for ONE model. Returns the work: to_embed (docs whose vectors are
    missing, stale, or chunk-incomplete), to_delete_docs (orphans), and
    models_to_drop (leftovers outside the active/pending pair).
    """
    to_embed: list[tuple[str, str, str]] = []
    for key, doc in docs.items():
        have_hash = vec_hashes.get(key)
        if have_hash != doc["text_hash"]:
            to_embed.append(key)          # prong 1 (missing) + 3 (stale)
            continue
        expected = len(chunk_text(doc["title"], doc["text"]))
        if vec_chunk_counts.get(key, 0) != expected:
            to_embed.append(key)          # coverage = ALL chunks present
    orphans = [key for key in vec_hashes if key not in docs]   # prong 2
    keep = {active_model} | ({pending_model} if pending_model else set())
    drops = sorted(models_present - keep)                      # prong 4
    return {"to_embed": to_embed, "to_delete_docs": orphans,
            "models_to_drop": drops}
