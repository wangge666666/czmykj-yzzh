from __future__ import annotations

import argparse
from pathlib import Path

import torch
from diffusers import (
    ControlNetModel,
    DPMSolverMultistepScheduler,
    StableDiffusionControlNetImg2ImgPipeline,
)
from PIL import Image


PROMPT = (
    "professional 3D white clay animatic, real solid geometry, uniform matte white material, monochrome white "
    "and light gray, realistic sculpted anatomy, clear face planes, defined eyelids nose lips jaw, detailed "
    "sculpted hair and cloth folds, natural hands, soft studio top light, strong form shadows, ambient occlusion, "
    "bright clean background, sharp focus, crisp detail, realistic proportions"
)

NEGATIVE_PROMPT = (
    "color, colored skin, colored clothing, black outline, ink outline, line art, pencil, sketch, anime, cartoon, "
    "flat shading, bas relief, embossed image, depth map, foggy, washed out, blurry, low detail, low resolution, "
    "text, subtitles, watermark, logo, poster, extra fingers, fused fingers, malformed hands, deformed body, "
    "duplicate person, plastic shine, metallic material, dark background"
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-model", type=Path, required=True)
    parser.add_argument("--controlnet", type=Path, required=True)
    parser.add_argument("--depth", type=Path, required=True)
    parser.add_argument("--init", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--width", type=int, default=448)
    parser.add_argument("--height", type=int, default=768)
    parser.add_argument("--steps", type=int, default=22)
    parser.add_argument("--seed", type=int, default=20260803)
    parser.add_argument("--strength", type=float, default=0.62)
    parser.add_argument("--prompt", default=PROMPT)
    parser.add_argument("--negative-prompt", default=NEGATIVE_PROMPT)
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

    with Image.open(args.depth) as image:
        depth = image.convert("RGB").resize((args.width, args.height), Image.Resampling.LANCZOS)
    with Image.open(args.init) as image:
        init_image = image.convert("RGB").resize((args.width, args.height), Image.Resampling.LANCZOS)

    generator = torch.Generator(device="cpu").manual_seed(args.seed)
    result = pipe(
        prompt=args.prompt,
        negative_prompt=args.negative_prompt,
        image=init_image,
        control_image=depth,
        strength=args.strength,
        num_inference_steps=args.steps,
        guidance_scale=7.5,
        controlnet_conditioning_scale=1.15,
        control_guidance_start=0.0,
        control_guidance_end=0.92,
        generator=generator,
    ).images[0]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    result.save(args.output, quality=96)
    print(args.output, flush=True)


if __name__ == "__main__":
    main()
