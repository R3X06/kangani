"""Lexical search over notes: sentence-aware chunking, an inverted index, and
Okapi BM25 scoring.

Deliberately dependency-free -- stdlib and SQLite only. The bot already carries
Chromium for timetable images; adding a vector/embedding stack for a corpus of
a few hundred short Telegram notes would cost more than it returns.

This is the LEXICAL half of the retrieval plan. A semantic (cosine) arm and
Reciprocal Rank Fusion sit on top of the same `note_chunks` rows if and when a
vector source is chosen -- nothing here forecloses that. Until then `search`
returns one ranking, not three, and the eval write-up should say so rather than
implying a fusion that isn't running.

Scoping: every query is scoped to one chat_id, and so are the corpus statistics
(N and avgdl). A global avgdl would let one chat's note lengths distort another
chat's scores. Single-user today, but the schema has always been multi-chat and
this is the kind of thing that is invisible until it isn't.
"""

import logging
import math
import re
import sqlite3
from collections import Counter

logger = logging.getLogger(__name__)

# Okapi BM25 constants. k1 controls how fast term-frequency saturates, b how
# hard length normalization bites. 1.5/0.75 are the standard defaults and are
# fine for short documents; they are named here so the eval can vary them.
K1 = 1.5
B = 0.75

# Chunk sizing. Notes are usually one Telegram message and stay a single chunk;
# this only bites on pasted lecture material.
MAX_CHUNK_CHARS = 400
# One sentence of overlap so a fact spanning a chunk boundary is still findable
# from either side.
OVERLAP_SENTENCES = 1

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Sentence terminator followed by whitespace. Abbreviations are repaired after
# the split rather than avoided during it -- a lookbehind that tries to encode
# every abbreviation is unreadable and still wrong.
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

_ABBREVIATIONS = frozenset({
    "e.g", "i.e", "etc", "vs", "cf", "al", "approx", "fig", "eq", "ch",
    "dr", "mr", "mrs", "ms", "prof", "st", "no", "vol", "pp", "sec",
})

# Deliberately short. An aggressive stoplist hurts a small corpus more than it
# helps: BM25's IDF term already discounts words that appear everywhere, so
# removing them by hand mostly just breaks phrase-ish queries.
_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "of", "to", "in", "on", "at", "for", "with", "by", "from", "as",
    "and", "or", "but", "if", "then", "than", "that", "this", "these",
    "those", "it", "its", "i", "you", "we", "they", "he", "she",
})


def _normalize(token: str) -> str:
    """Very conservative suffix stripping.

    Only plurals and -ing/-ed, only on tokens long enough that the stripped
    form is still a word. No Porter stemmer: full stemming needs a stop-list of
    exceptions to avoid collapsing unrelated terms, and on a corpus this small
    a bad collision costs more recall than the extra matching gains.
    """
    if len(token) > 4:
        if token.endswith("ies"):
            return token[:-3] + "y"
        if token.endswith("sses"):
            return token[:-2]
        if token.endswith("ing") and len(token) > 6:
            return token[:-3]
        if token.endswith("ed") and len(token) > 5:
            return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens, stopped and normalized."""
    return [
        _normalize(t)
        for t in _TOKEN_RE.findall(text.lower())
        if t not in _STOPWORDS
    ]


def split_sentences(text: str) -> list[str]:
    """Split on sentence terminators, then glue back any split that landed
    immediately after a known abbreviation."""
    raw = _SENT_SPLIT_RE.split(text.strip())
    out: list[str] = []
    for piece in raw:
        if not piece:
            continue
        if out:
            tail = out[-1].rstrip()
            last_word = tail.split()[-1].lower().rstrip(".") if tail.split() else ""
            if last_word in _ABBREVIATIONS:
                out[-1] = f"{out[-1]} {piece}"
                continue
        out.append(piece)
    return out


def _hard_split(sentence: str) -> list[str]:
    """A single sentence longer than a whole chunk still has to fit somewhere.
    Break on whitespace rather than mid-word."""
    words = sentence.split()
    parts: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > MAX_CHUNK_CHARS:
            parts.append(current)
            current = word
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def chunk_text(text: str) -> list[str]:
    """Pack sentences into <=MAX_CHUNK_CHARS chunks with sentence overlap."""
    sentences: list[str] = []
    for sentence in split_sentences(text):
        if len(sentence) > MAX_CHUNK_CHARS:
            sentences.extend(_hard_split(sentence))
        else:
            sentences.append(sentence)

    if not sentences:
        return []

    chunks: list[str] = []
    current: list[str] = []
    for sentence in sentences:
        candidate = " ".join(current + [sentence])
        if current and len(candidate) > MAX_CHUNK_CHARS:
            chunks.append(" ".join(current))
            current = current[-OVERLAP_SENTENCES:] if OVERLAP_SENTENCES else []
            # The overlap tail plus the new sentence can itself overflow; drop
            # the tail rather than emit an oversized chunk.
            if len(" ".join(current + [sentence])) > MAX_CHUNK_CHARS:
                current = []
            current.append(sentence)
        else:
            current.append(sentence)
    if current:
        chunks.append(" ".join(current))
    return chunks


# --- indexing --------------------------------------------------------------

def unindex_note(conn: sqlite3.Connection, note_id: int) -> None:
    """Drop a note's chunks and postings.

    Called explicitly on note delete/edit. Note that note_chunks also has ON
    DELETE CASCADE from notes(id), so a cascading topic delete cleans up on its
    own -- but only when foreign_keys is ON, which get_connection sets. The
    explicit path exists for re-indexing an edited note, where nothing is
    deleted from `notes` at all.
    """
    conn.execute(
        "DELETE FROM postings WHERE chunk_id IN "
        "(SELECT id FROM note_chunks WHERE note_id = ?)",
        (note_id,),
    )
    conn.execute("DELETE FROM note_chunks WHERE note_id = ?", (note_id,))


def index_note(
    conn: sqlite3.Connection, note_id: int, chat_id: int, content: str
) -> int:
    """(Re)index one note. Returns the number of chunks written.

    Idempotent: unindexes first, so calling it twice on the same note leaves
    the same rows rather than doubling the term frequencies.
    """
    unindex_note(conn, note_id)
    written = 0
    offset = 0
    for chunk_index, chunk in enumerate(chunk_text(content)):
        # Locate the chunk in the source so a hit can be shown in context
        # later. Overlap means find() must start from the previous match, or
        # the second chunk resolves back to the first one's position.
        char_start = content.find(chunk, offset)
        if char_start == -1:
            char_start = offset
        char_end = char_start + len(chunk)
        offset = char_start + 1

        tokens = tokenize(chunk)
        cur = conn.execute(
            "INSERT INTO note_chunks (note_id, chat_id, chunk_index, text, "
            "length, char_start, char_end) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (note_id, chat_id, chunk_index, chunk, len(tokens),
             char_start, char_end),
        )
        chunk_id = cur.lastrowid
        counts = Counter(tokens)
        if counts:
            conn.executemany(
                "INSERT INTO postings (term, chunk_id, tf) VALUES (?, ?, ?)",
                [(term, chunk_id, tf) for term, tf in counts.items()],
            )
        written += 1
    return written


def backfill(conn: sqlite3.Connection) -> int:
    """Index every note that has no chunks yet.

    Additive and idempotent, in the same spirit as _backfill_tags: every run
    after the first is a no-op. A note whose content chunks to nothing (empty
    or punctuation-only) would be re-attempted on every startup; that is a
    handful of rows at worst and is preferred to a marker column.
    """
    rows = conn.execute(
        "SELECT n.id, n.chat_id, n.content FROM notes n "
        "WHERE NOT EXISTS (SELECT 1 FROM note_chunks c WHERE c.note_id = n.id)"
    ).fetchall()
    indexed = 0
    for row in rows:
        indexed += index_note(conn, row["id"], row["chat_id"], row["content"])
    if rows:
        conn.commit()
        logger.info(
            "Retrieval backfill: indexed %d note(s) into %d chunk(s)",
            len(rows), indexed,
        )
    return indexed


# --- search ----------------------------------------------------------------

def _corpus_stats(conn: sqlite3.Connection, chat_id: int) -> tuple[int, float]:
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(AVG(length), 0) AS avgdl "
        "FROM note_chunks WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    return row["n"], row["avgdl"]


def score_bm25(
    conn: sqlite3.Connection, chat_id: int, query: str
) -> list[tuple[int, float]]:
    """Score every chunk in this chat that shares a term with the query.

    Returns (chunk_id, score) sorted high to low. Chunks sharing no query term
    are absent rather than scored zero -- an inverted index exists precisely so
    the whole corpus is never touched.
    """
    terms = tokenize(query)
    if not terms:
        return []
    unique = sorted(set(terms))
    placeholders = ",".join("?" * len(unique))

    n_docs, avgdl = _corpus_stats(conn, chat_id)
    if n_docs == 0 or avgdl == 0:
        return []

    df_rows = conn.execute(
        f"SELECT p.term, COUNT(DISTINCT p.chunk_id) AS df FROM postings p "
        f"JOIN note_chunks c ON c.id = p.chunk_id "
        f"WHERE c.chat_id = ? AND p.term IN ({placeholders}) GROUP BY p.term",
        (chat_id, *unique),
    ).fetchall()
    df = {r["term"]: r["df"] for r in df_rows}
    if not df:
        return []

    posting_rows = conn.execute(
        f"SELECT p.term, p.chunk_id, p.tf, c.length FROM postings p "
        f"JOIN note_chunks c ON c.id = p.chunk_id "
        f"WHERE c.chat_id = ? AND p.term IN ({placeholders})",
        (chat_id, *unique),
    ).fetchall()

    # A query term repeated ("gradient gradient descent") should weigh more
    # than one mentioned once, so score per occurrence rather than per unique
    # term.
    query_tf = Counter(terms)
    scores: dict[int, float] = {}
    for row in posting_rows:
        term = row["term"]
        # Okapi IDF. The +1 inside the log keeps this non-negative for a term
        # appearing in more than half the corpus, which the raw Robertson form
        # does not -- without it a common term can subtract from a score and
        # push a genuinely matching chunk below a non-matching one.
        idf = math.log(1 + (n_docs - df[term] + 0.5) / (df[term] + 0.5))
        tf = row["tf"]
        norm = tf + K1 * (1 - B + B * row["length"] / avgdl)
        contribution = idf * (tf * (K1 + 1)) / norm
        scores[row["chunk_id"]] = (
            scores.get(row["chunk_id"], 0.0) + contribution * query_tf[term]
        )

    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


def search(
    conn: sqlite3.Connection, chat_id: int, query: str, limit: int = 10
) -> list[dict]:
    """Ranked chunk hits, joined back to their note and topic for display."""
    ranked = score_bm25(conn, chat_id, query)[:limit]
    if not ranked:
        return []
    by_id = {chunk_id: score for chunk_id, score in ranked}
    placeholders = ",".join("?" * len(by_id))
    rows = conn.execute(
        f"SELECT c.id AS chunk_id, c.text, c.chunk_index, n.id AS note_id, "
        f"n.tag, n.source, n.is_reference, t.name AS topic_name "
        f"FROM note_chunks c JOIN notes n ON n.id = c.note_id "
        f"LEFT JOIN topics t ON t.id = n.topic_id "
        f"WHERE c.id IN ({placeholders})",
        tuple(by_id),
    ).fetchall()
    out = [dict(r) | {"score": by_id[r["chunk_id"]]} for r in rows]
    out.sort(key=lambda r: (-r["score"], r["chunk_id"]))
    return out
