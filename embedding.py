"""Text embedding for the semantic half of retrieval.

Bytes in, bytes out. This module knows nothing about SQLite, `note_chunks`,
chats or notes -- it turns strings into fixed-width float32 blobs and back.
Everything that decides *what* to embed and *where* to put it lives in
`database.py` and `retrieval.py`. That separation is not decoration: the same
interface has to lift into a Postgres/pgvector store later without carrying a
`sqlite3.Connection` along in its signatures.

Three properties this module commits to, because things downstream rely on
them:

1. **Importing costs nothing.** The model is loaded on first use, not on
   import. `import embedding` opens no file and touches no network, so `bot.py`
   startup is unchanged whether or not embeddings are ever used.
2. **Absence is a supported state, not an error.** `get_embedder()` returns
   None when embeddings are switched off, when `fastembed` isn't installed, or
   when the model fails to load. Callers write a NULL embedding and carry on.
   Indexing a note must never cost the user their note.
3. **The byte layout is fixed and explicit.** Little-endian float32, L2
   normalized, no header. Whoever computes cosine similarity later decodes with
   `decode_vector` and does not have to guess.

The model file itself is ~67 MB and is hosted only on HuggingFace -- fastembed
lists no mirror for this model, unlike some of its siblings. That is a
build-time dependency (the weights should be materialized into the image), not
a runtime one, but it is also why a load failure has to degrade rather than
raise: a HuggingFace outage during a note write should cost that chunk its
vector, nothing more.
"""

import logging
import math
import os
import struct
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# 384 dimensions, MIT licensed, ~67 MB quantized ONNX. fastembed resolves this
# name to the `qdrant/bge-small-en-v1.5-onnx-q` repo and the
# `model_optimized.onnx` file inside it.
DEFAULT_MODEL_ID = "BAAI/bge-small-en-v1.5"

# Env vars follow the KANGANI_* convention already used by
# KANGANI_INSTRUMENTATION and KANGANI_MAX_TOOL_ITERATIONS.
ENABLED_ENV_VAR = "KANGANI_EMBEDDINGS"
MODEL_ENV_VAR = "KANGANI_EMBEDDING_MODEL"

# Anything in this set disables embeddings. Default is ENABLED: an unset
# variable in production should give the fuller behaviour, and the eval harness
# sets it explicitly to pin the lexical-only baseline.
_DISABLED_VALUES = frozenset({"0", "false", "off", "no", "none", ""})


# --- byte layout -----------------------------------------------------------

def l2_normalize(values: list[float]) -> list[float]:
    """Scale a vector to unit length so cosine similarity is a plain dot
    product -- the per-comparison square roots disappear from the scan.

    A zero vector is returned unchanged rather than raising. It cannot arise
    from a real embedding, but a NULL-ish row reaching here should not take
    down a note write, and zeros score 0.0 against everything, which is the
    correct answer for a vector carrying no information.
    """
    norm = math.sqrt(sum(v * v for v in values))
    if norm == 0.0:
        logger.warning("l2_normalize received a zero vector; returning it unchanged")
        return list(values)
    return [v / norm for v in values]


def encode_vector(values: list[float]) -> bytes:
    """Pack floats into the stored blob format: little-endian float32, no
    header, no length prefix.

    Explicitly little-endian ('<') rather than native ('=') so a database file
    stays readable if it is ever moved between architectures. At 384 floats the
    cost of not using the native path is irrelevant, and a silently
    byte-swapped vector is the kind of bug that reads as "the model got worse".

    Note this does NOT normalize -- `embed` normalizes before encoding, so
    doing it here as well would be a second, invisible pass.
    """
    return struct.pack(f"<{len(values)}f", *values)


def decode_vector(blob: bytes) -> list[float]:
    """Unpack a stored blob back into floats. Inverse of `encode_vector`."""
    if len(blob) % 4 != 0:
        raise ValueError(
            f"embedding blob length {len(blob)} is not a multiple of 4 bytes "
            "-- it was not written by encode_vector"
        )
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


# --- interface -------------------------------------------------------------

@runtime_checkable
class Embedder(Protocol):
    """What the rest of the system is allowed to depend on.

    Deliberately narrow. No connection, no chat_id, no note_id -- an
    implementation backed by a hosted API or by pgvector satisfies this
    unchanged.
    """

    @property
    def model_id(self) -> str:
        """Identifier of the model producing these vectors. Stored alongside
        the vectors would let a model change be detected; for now it is here so
        logs and the eval write-up can name what actually ran."""
        ...

    @property
    def dim(self) -> int:
        """Number of dimensions per vector. Available without loading the
        model, so a caller can size a column or validate a blob cheaply."""
        ...

    def embed(self, texts: list[str]) -> list[bytes]:
        """Embed a batch of strings, returning one L2-normalized,
        little-endian float32 blob per input, in the same order."""
        ...


class FastEmbedEmbedder:
    """`Embedder` backed by fastembed's ONNX runtime.

    The `TextEmbedding` object is constructed on first `embed` call. That
    construction is what downloads the weights if they are not already cached,
    so it is deliberately kept off the import path and off `__init__`.
    """

    def __init__(self, model_id: str = DEFAULT_MODEL_ID) -> None:
        self._model_id = model_id
        self._model = None
        self._dim: int | None = None

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dim(self) -> int:
        if self._dim is None:
            self._dim = self._lookup_dim()
        return self._dim

    def _lookup_dim(self) -> int:
        """Read the dimension from fastembed's model registry.

        This is a lookup in a Python list of dataclasses -- it does not load or
        download the model, which is why `dim` is usable before any weights
        exist. If the model is loaded already, prefer asking it directly.
        """
        from fastembed import TextEmbedding

        for description in TextEmbedding.list_supported_models():
            if description["model"] == self._model_id:
                return int(description["dim"])
        raise ValueError(
            f"{self._model_id!r} is not in fastembed's supported model list"
        )

    def _load(self):
        if self._model is None:
            from fastembed import TextEmbedding

            logger.info("Loading embedding model %s", self._model_id)
            self._model = TextEmbedding(model_name=self._model_id)
        return self._model

    def embed(self, texts: list[str]) -> list[bytes]:
        if not texts:
            return []
        model = self._load()
        blobs = []
        for vector in model.embed(texts):
            # fastembed yields numpy arrays; tolist() gets us to plain floats
            # so nothing below this line depends on numpy being importable.
            blobs.append(encode_vector(l2_normalize(vector.tolist())))
        if len(blobs) != len(texts):
            raise RuntimeError(
                f"embedder returned {len(blobs)} vectors for {len(texts)} inputs"
            )
        return blobs


# --- resolution ------------------------------------------------------------

_embedder: Embedder | None = None
_resolved = False


def embeddings_enabled() -> bool:
    """Whether the switch is on. Separate from `get_embedder()` so a caller can
    distinguish "switched off" from "switched on but unavailable" -- those look
    the same downstream but mean very different things in a log line."""
    raw = os.environ.get(ENABLED_ENV_VAR)
    if raw is None:
        return True
    return raw.strip().casefold() not in _DISABLED_VALUES


def get_embedder() -> Embedder | None:
    """The process-wide embedder, or None if embeddings are unavailable.

    Resolved once and cached, including the None result: a missing dependency
    is not going to appear halfway through a process, and retrying the import
    on every note write would turn one log line into thousands.

    Returning None rather than raising is the whole contract. Callers write a
    NULL embedding, keep their BM25 behaviour, and stay correct.
    """
    global _embedder, _resolved
    if _resolved:
        return _embedder

    _resolved = True
    if not embeddings_enabled():
        logger.info("Embeddings disabled via %s", ENABLED_ENV_VAR)
        _embedder = None
        return None

    model_id = os.environ.get(MODEL_ENV_VAR, DEFAULT_MODEL_ID)
    try:
        import fastembed  # noqa: F401  -- probing availability, not using it here
    except ImportError:
        logger.warning(
            "fastembed is not installed; embeddings unavailable, retrieval "
            "will use BM25 only"
        )
        _embedder = None
        return None

    _embedder = FastEmbedEmbedder(model_id)
    logger.info("Embedder ready: %s (weights load on first use)", model_id)
    return _embedder


def reset_embedder_cache() -> None:
    """Clear the cached resolution. For tests that flip the env var; not used
    at runtime."""
    global _embedder, _resolved
    _embedder = None
    _resolved = False
