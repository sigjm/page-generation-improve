"""Fuse an mlx-vlm LoRA adapter into its 4bit base, as a model folder MLX Serve can load.

MLX Serve does not load adapters, so the fine-tuned agent model is the base with the adapter
merged in and re-quantized at the base's bits and group size.

    <mlx-vlm-venv>/bin/python scripts/dataset/fuse_lora.py \
        --model ~/.mlx-serve/models/empero-ai/Qwen3.8-9B-Distill-MLX-4bit \
        --adapter .local/finetune/lora/run-1 \
        --out ~/.mlx-serve/models/local/Qwen3.8-9B-kcdf-agent-4bit
"""

import argparse
import shutil
from pathlib import Path


def main() -> int:
    from mlx.utils import tree_unflatten
    from mlx_vlm.utils import load, save_weights

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    base, out = args.model.expanduser(), args.out.expanduser()

    model, _ = load(str(base), adapter_path=str(args.adapter.expanduser()))
    fused = [
        (name, module.fuse(dequantize=False))
        for name, module in model.named_modules()
        if hasattr(module, "fuse") and hasattr(module, "lora_a")
    ]
    if not fused:
        raise SystemExit("no LoRA layers found; is --adapter an mlx-vlm adapter folder?")
    model.update_modules(tree_unflatten(fused))

    out.mkdir(parents=True, exist_ok=True)
    save_weights(out, model, donate_weights=True)
    # Config, tokenizer, chat template and processor files come from the base unchanged.
    for item in base.iterdir():
        if item.suffix == ".safetensors" or item.name == "model.safetensors.index.json":
            continue
        if item.is_dir():
            shutil.copytree(item, out / item.name, dirs_exist_ok=True)
        else:
            shutil.copy2(item, out / item.name)
    print(f"fused {len(fused)} LoRA layers into {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
