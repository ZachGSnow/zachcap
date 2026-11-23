import numpy as np

from zachcap.voiceprint import from_blob, match_speakers, person_score, to_blob

rng = np.random.default_rng(0)
DANA, MARK, ZACH = (rng.normal(size=192) for _ in range(3))


def near(vector: np.ndarray, noise: float = 0.2) -> np.ndarray:
    return vector + noise * rng.normal(size=vector.shape) * np.linalg.norm(vector) / 14


def test_blob_round_trip():
    v = rng.normal(size=192).astype(np.float32)
    assert np.array_equal(from_blob(to_blob(v)), v)


def test_known_voices_are_labeled_and_strangers_are_not():
    library = {1: [near(DANA), near(DANA)], 2: [near(MARK)]}
    result = match_speakers({"s1": near(MARK), "s2": near(DANA), "s3": near(ZACH)}, library)
    assert (result["s1"].person_id, result["s1"].status) == (2, "auto")
    assert (result["s2"].person_id, result["s2"].status) == (1, "auto")
    assert (result["s3"].person_id, result["s3"].status) == (None, "unknown")


def test_a_voice_split_in_two_gets_the_same_name_twice():
    # Diarization sometimes splits one person into two speakers; both should be named.
    library = {1: [near(DANA)], 2: [near(MARK)]}
    result = match_speakers({"a": near(DANA), "b": near(DANA)}, library)
    assert [(m.person_id, m.status) for m in result.values()] == [(1, "auto"), (1, "auto")]


def test_empty_library_means_everyone_is_unknown():
    result = match_speakers({"a": near(DANA)}, {})
    assert result["a"].status == "unknown"


def test_score_uses_best_samples():
    assert person_score(DANA, [DANA, -DANA, MARK], top_k=1) > 0.99
