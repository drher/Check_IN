import json
from pathlib import Path


def test_target_button_is_sign_out_selector():
    config = json.loads(Path('config.json').read_text(encoding='utf-8'))
    assert '下班簽退' in config['selectors']['target_button']
