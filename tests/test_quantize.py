import json

import pytest

from src.quantize import calibration_prompts


def test_calibration_prompts_seeded_sample(tmp_path):
    states = tmp_path / "states.jsonl"
    states.write_text("".join(json.dumps({"prompt": f"s{i}"}) + "\n" for i in range(10)))
    a = calibration_prompts(states, 4, seed=7)
    assert a == calibration_prompts(states, 4, seed=7) and len(set(a)) == 4
    with pytest.raises(ValueError):
        calibration_prompts(states, 11, seed=7)
