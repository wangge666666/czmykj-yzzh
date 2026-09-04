from __future__ import annotations

import argparse
import wave
from pathlib import Path

import numpy as np
import torch

from demucs.apply import apply_model
from demucs.pretrained import get_model


def read_pcm16_wav(path: Path) -> tuple[torch.Tensor, int]:
    with wave.open(str(path), "rb") as stream:
        channels = stream.getnchannels()
        sample_rate = stream.getframerate()
        sample_width = stream.getsampwidth()
        frames = stream.readframes(stream.getnframes())
    if sample_width != 2:
        raise RuntimeError(f"Expected 16-bit PCM WAV, received {sample_width * 8}-bit audio.")
    samples = np.frombuffer(frames, dtype="<i2").reshape(-1, channels).T.copy()
    return torch.from_numpy(samples).float().div_(32768.0), sample_rate


def write_pcm16_wav(path: Path, audio: torch.Tensor, sample_rate: int) -> None:
    data = audio.detach().cpu().float().numpy()
    peak = float(np.max(np.abs(data))) if data.size else 0.0
    if peak > 0.99:
        data = data * (0.99 / peak)
    pcm = np.clip(data, -1.0, 1.0)
    pcm = np.round(pcm.T * 32767.0).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(int(data.shape[0]))
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(pcm.tobytes())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="htdemucs_ft")
    parser.add_argument("--shifts", type=int, default=1)
    parser.add_argument("--overlap", type=float, default=0.25)
    args = parser.parse_args()

    model = get_model(args.model)
    model.cpu()
    model.eval()
    waveform, sample_rate = read_pcm16_wav(args.input)
    if sample_rate != int(model.samplerate):
        raise RuntimeError(
            f"Input WAV sample rate {sample_rate} does not match model sample rate {model.samplerate}."
        )
    if waveform.shape[0] != int(model.audio_channels):
        raise RuntimeError(
            f"Input WAV has {waveform.shape[0]} channels; model expects {model.audio_channels}."
        )

    reference = waveform.mean(0)
    reference_mean = reference.mean()
    reference_std = reference.std().clamp_min(1e-8)
    normalized = (waveform - reference_mean) / reference_std
    with torch.inference_mode():
        sources = apply_model(
            model,
            normalized[None],
            device="cpu",
            shifts=args.shifts,
            split=True,
            overlap=args.overlap,
            progress=True,
            num_workers=1,
        )[0]
    sources = sources * reference_std + reference_mean

    vocals_index = list(model.sources).index("vocals")
    vocals = sources[vocals_index]
    background = torch.zeros_like(vocals)
    for index, source in enumerate(sources):
        if index != vocals_index:
            background += source

    stem_dir = args.output / args.model / args.input.stem
    write_pcm16_wav(stem_dir / "vocals.wav", vocals, int(model.samplerate))
    write_pcm16_wav(stem_dir / "no_vocals.wav", background, int(model.samplerate))
    print(f"dialogue={stem_dir / 'vocals.wav'}")


if __name__ == "__main__":
    main()
