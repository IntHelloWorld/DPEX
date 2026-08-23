from typing import Any, Dict


TEXT_INDEX_MODE = "text_index"
IMAGE_ONLY_MODE = "image_only"
INTERACTION_MODES = {TEXT_INDEX_MODE, IMAGE_ONLY_MODE}


def localization_interaction_mode(config: Dict[str, Any]) -> str:
    cfg = config.get("mllm", config)
    mode = str(cfg.get("interaction_mode") or TEXT_INDEX_MODE).strip()
    if mode not in INTERACTION_MODES:
        raise ValueError(
            "mllm.interaction_mode must be 'text_index' or 'image_only'"
        )
    return mode
