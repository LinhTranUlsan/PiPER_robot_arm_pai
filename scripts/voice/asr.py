"""Whisper speech-to-text through `transformers`, which SmolVLA already needs.

No openai-whisper / faster-whisper: the first pulls numba and its own torch pin, the
second needs CUDA 12 libraries next to this env's CUDA 13 torch. transformers runs
Whisper on the torch that is already here.

The model downloads once into ~/.cache/huggingface (small = ~1 GB). It stays on the GPU
next to SmolVLA; both together are far inside the 32 GB card.
"""
from __future__ import annotations

import time

import numpy as np

from audio import RATE

DEFAULT_MODEL = "openai/whisper-small"


class Whisper:
    def __init__(self, model: str = DEFAULT_MODEL, language: str | None = "en",
                 device: str | None = None, hint: str | None = None):
        import torch
        from transformers import WhisperForConditionalGeneration, WhisperProcessor

        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.dtype = torch.float16 if self.device.startswith("cuda") else torch.float32
        t0 = time.perf_counter()
        self.processor = WhisperProcessor.from_pretrained(model)
        self.model = WhisperForConditionalGeneration.from_pretrained(model, dtype=self.dtype)
        self.model.to(self.device).eval()
        # transformers warns on EVERY generate() call (max_new_tokens vs max_length, the
        # attention mask Whisper does not need); at one call per command that buries the
        # session output. Errors still show.
        from transformers.utils import logging as hf_logging
        hf_logging.set_verbosity_error()
        # English-only checkpoints (*.en) reject a language argument outright.
        self.language = None if model.endswith(".en") else language
        # An optional prompt biases decoding toward the colour words. Off by default: on noise
        # a prompted Whisper can "hear" the prompt itself -- parse_color() then sees every
        # colour and refuses, which is safe, but it makes a noisy room look like a bad mic.
        self.prompt_ids = (self.processor.get_prompt_ids(hint, return_tensors="pt").to(self.device)
                           if hint else None)
        print(f"  [asr] {model} on {self.device} ({self.dtype}), loaded in {time.perf_counter()-t0:.1f}s",
              flush=True)

    def transcribe(self, audio: np.ndarray) -> str:
        import torch

        feats = self.processor(audio, sampling_rate=RATE, return_tensors="pt").input_features
        feats = feats.to(self.device, dtype=self.dtype)
        kw = {"task": "transcribe", "max_new_tokens": 40}
        if self.language:
            kw["language"] = self.language
        if self.prompt_ids is not None:
            kw["prompt_ids"] = self.prompt_ids
        with torch.inference_mode():
            ids = self.model.generate(feats, **kw)
        text = self.processor.batch_decode(ids, skip_special_tokens=True)[0].strip()
        # A prompted generate() returns the prompt too; keep only what was heard.
        if self.prompt_ids is not None:
            hint_text = self.processor.decode(self.prompt_ids[1:], skip_special_tokens=True).strip()
            if hint_text and text.startswith(hint_text):
                text = text[len(hint_text):].strip()
        return text

    def encode(self, audio: np.ndarray) -> np.ndarray:
        """Whisper's encoder states for just the spoken part: [frames, dim], 50 frames/s.

        This is what the voice profile (voiceprint.py) matches on: the representation Whisper
        itself hears through, so it copes with room noise far better than raw spectra.
        """
        import torch

        feats = self.processor(audio, sampling_rate=RATE, return_tensors="pt").input_features
        feats = feats.to(self.device, dtype=self.dtype)
        with torch.inference_mode():
            h = self.model.model.encoder(feats).last_hidden_state[0]
        n = max(1, min(h.shape[0], int(np.ceil(len(audio) / RATE * 50))))
        return h[:n].float().cpu().numpy()
