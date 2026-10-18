"""Deterministic stand-in for the FastEmbed model in tests.

The semantic index is live whenever the ``embed`` extra is installed, so the write path
embeds on every ordinary test — constructing a real ONNX session, and on a host without a
warm cache attempting a HuggingFace download. Only the *model class* is swapped:
``available()`` and ``VectorIndex`` are left completely alone, so the index is still
built, ``_embedder_or_fail`` still applies its load lock, and every hybrid code path is
still exercised.

The tests that need real semantics (a paraphrase match that a hash cannot produce) opt
out with the ``real_embedder`` marker and keep using the real model.

Note: this patch is in-process. A daemon started by ``MEM_TEST_SUBPROCESS=1`` runs in its
own interpreter and therefore still loads the real model — which is the intended
full-fidelity path, not an oversight.
"""

from __future__ import annotations

import hashlib
import math

from mem.server import embedding


def fake_unit_vector(text: str, dimensions: int = embedding.DIM) -> list[float]:
    """Hash ``text`` into an L2-normalised vector of the model's width.

    Same shape and normalisation contract as ``Embedder.embed``, so ``_serialize`` and
    sqlite-vec's ``FLOAT[384]`` column are unaffected.
    """
    values: list[float] = []
    counter = 0
    while len(values) < dimensions:
        digest = hashlib.sha256(f"{counter}:{text}".encode("utf-8")).digest()
        values.extend((byte - 127.5) / 127.5 for byte in digest)
        counter += 1
    vector = values[:dimensions]
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


class FakeEmbedder:
    """Drop-in for :class:`~mem.server.embedding.Embedder`, minus the model.

    Substituted for the class itself rather than for the factory that builds it, so
    ``VectorIndex._embedder_or_fail`` still runs for real — its load lock and its
    circuit breaker stay under test. ``test_s33.py::test_model_construction_is_serialised``
    swaps ``embedding.Embedder`` itself and keeps working, because the patch is just
    another value for the same module global.
    """

    model_name = embedding.MODEL_ALIAS

    def __init__(self, *args: object, **kwargs: object) -> None:
        # Matches `Embedder(model_name=..., cache_dir=...)`. Nothing to load.
        pass

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [fake_unit_vector(text) for text in texts]


def use_fake_embeddings(request, monkeypatch) -> None:
    """Swap the model class, unless the test asks for the real one.

    ``_embedder_or_fail`` builds the model through the module-global ``Embedder`` name
    (``embedding.py``, ``self._embedder = Embedder()``), so replacing that name reaches
    both paths — ``sync()`` and ``search()`` — without importing ``fastembed``, opening
    an ONNX session, or downloading anything.
    """
    if request.node.get_closest_marker("real_embedder"):
        return
    monkeypatch.setattr(embedding, "Embedder", FakeEmbedder)
