from __future__ import annotations

import argparse
from pathlib import Path

import torch
from diffusers import ControlNetModel, DPMSolverMultistepScheduler, StableDiffusionControlNetImg2ImgPipeline
from PIL import Image


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-model", type=Path, required=True)
    parser.add_argument("--controlnet", type=Path, required=True)
    parser.add_argument("--init-dir", type=Path, required=True)
    parser.add_argument("--depth-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--negative-prompt", required=True)
    parser.add_argument("--width", type=int, default=448)
    parser.add_argument("--height", type=int, default=768)
    parser.add_argument("--steps", type=int, default=22)
    parser.add_argument("--strength", type=float, default=0.55)
    parser.add_argument("--seed", type=int, default=20260803)
    parser.add_argument("--previous-blend", type=float, default=0.16)
    parser.add_argument("--guidance-scale", type=float, default=7.5)
    parser.add_argument("--control-scale", type=float, default=1.2)
    parser.add_argument("--control-end", type=float, default=0.94)
    args = parser.parse_args()

    torch.backends.cuda.matmul.allow_tf32 = True
    dtype = torch.float16
    controlnet = ControlNetModel.from_pretrained(
        args.controlnet,
        torch_dtype=dtype,
        variant="fp16",
        use_safetensors=True,
        local_files_only=True,
    )
    pipe = StableDiffusionControlNetImg2ImgPipeline.from_pretrained(
        args.base_model,
        controlnet=controlnet,
        torch_dtype=dtype,
        variant="fp16",
        use_safetensors=True,
        safety_checker=None,
        feature_extractor=None,
        requires_safety_checker=False,
        local_files_only=True,
    )
    pipe.scheduler = DPMSolverMultistepScheduler.from_config(
        pipe.scheduler.config,
        use_karras_sigmas=True,
        algorithm_type="dpmsolver++",
    )
    pipe.enable_model_cpu_offload(gpu_id=0)
    pipe.enable_attention_slicing("max")
    pipe.vae.enable_tiling()

    init_paths = sorted(args.init_dir.glob("*_whiteclay.jpg"))
    if not init_paths:
        raise SystemExit(f"No *_whiteclay.jpg files found in {args.init_dir}")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    previous: Image.Image | None = None
    for index, init_path in enumerate(init_paths, start=1):
        stem = init_path.stem.removesuffix("_whiteclay")
        depth_path = args.depth_dir / f"{stem}_depth.png"
        if not depth_path.exists():
            raise FileNotFoundError(depth_path)

        with Image.open(init_path) as image:
            init_image = image.convert("RGB").resize((args.width, args.height), Image.Resampling.LANCZOS)
        if previous is not None and args.previous_blend > 0:
            init_image = Image.blend(init_image, previous, args.previous_blend)
        with Image.open(depth_path) as image:
            depth = image.convert("RGB").resize((args.width, args.height), Image.Resampling.LANCZOS)

        generator = torch.Generator(device="cpu").manual_seed(args.seed)
        output = pipe(
            prompt=args.prompt,
            negative_prompt=args.negative_prompt,
            image=init_image,
            control_image=depth,
            strength=args.strength,
            num_inference_steps=args.steps,
            guidance_scale=args.guidance_scale,
            controlnet_conditioning_scale=args.control_scale,
            control_guidance_start=0.0,
            control_guidance_end=args.control_end,
            generator=generator,
        ).images[0]
        output.save(args.output_dir / f"frame_{index:04d}.png")
        previous = output
        print(f"[{index}/{len(init_paths)}] {stem}", flush=True)


if __name__ == "__main__":
    main()
