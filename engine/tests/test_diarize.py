import numpy as np
import pytest

torch = pytest.importorskip("torch")  # the GPU stack isn't installed in CI

from zachcap.diarize import FixedBatchEmbedding  # noqa: E402


def test_short_last_batch_is_padded_to_the_full_size_then_trimmed():
    seen = []

    class Inner:
        dimension = 256

        def __call__(self, waveforms, masks=None):
            seen.append((tuple(waveforms.shape), tuple(masks.shape)))
            return np.arange(waveforms.shape[0] * 2, dtype=np.float32).reshape(-1, 2)

    embedding = FixedBatchEmbedding(Inner(), batch_size=32)
    out = embedding(torch.zeros(27, 1, 16000), masks=torch.zeros(27, 589))

    # One fixed shape reaches the model, so cuDNN never plans for a new one.
    assert seen == [((32, 1, 16000), (32, 589))]
    assert out.shape == (27, 2)
    assert embedding.dimension == 256  # other attributes pass through


def test_full_batches_are_untouched():
    class Inner:
        def __call__(self, waveforms, masks=None):
            return np.zeros((waveforms.shape[0], 2))

    out = FixedBatchEmbedding(Inner(), 32)(torch.zeros(32, 1, 10), masks=torch.zeros(32, 5))
    assert out.shape == (32, 2)
