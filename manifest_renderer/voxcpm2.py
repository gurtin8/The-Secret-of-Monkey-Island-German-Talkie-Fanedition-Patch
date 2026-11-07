"""Pure argv construction for VoxCPM2 Combined render methods."""
from __future__ import annotations

from pathlib import Path
from typing import Sequence


def _prompt_text(parts: Sequence[str]) -> str:
    # The Combined CLI concatenates prompt_text with target_text directly.
    return " ".join(part.strip() for part in parts) + " "


def build_argv(*, cli: Path | str, models: Sequence[Path | str], anchor: Path,
               output: Path, target_text: str, seed: int | str,
               method: str = "normal", short: bool = False, use_prewarm: bool | None = None,
               line_audio: Path | None = None, line_text: str | None = None,
               main_anchor_audio: Path | None = None, main_anchor_text: str | None = None,
               materialized_prompt_wav: Path | None = None,
               prewarm_audio: Path | None = None, prewarm_text: str | None = None,
               performance_prompt_missing: bool = False) -> list[str]:
    """Build argv only; caller materializes normalized/combined prompt WAVs."""
    if method not in {"normal", "standard", "without_emotion", "without_fixed_anchor", "main_anchor_only"}:
        raise ValueError(f"Unbekannte VoxCPM2-Methode: {method}")
    reference = line_audio if method == "without_fixed_anchor" else anchor
    if reference is None:
        raise ValueError("Rendermethode benötigt eine Referenz-WAV")
    if method == "without_fixed_anchor" and not use_prewarm:
        # Keep the historical isolated production command, including argv order.
        return [str(cli), "-t", target_text, "--seed", str(seed), "-o", str(output),
                "-r", str(reference), *map(str, models)]
    prewarm_enabled = short if use_prewarm is None else use_prewarm
    argv = [str(cli), "-t", target_text, "-r", str(reference)]
    audio: list[Path] = []
    texts: list[str] = []
    if method == "without_fixed_anchor":
        if prewarm_audio is None or not prewarm_text:
            raise ValueError("Originalreferenz mit Prewarm benötigt deutsche Prewarm-WAV und Text")
        audio = [prewarm_audio]
        texts = [prewarm_text]
    elif method in {"normal", "standard"}:
        if prewarm_enabled and not performance_prompt_missing:
            if not all((main_anchor_audio, main_anchor_text, line_audio, line_text,
                        prewarm_audio, prewarm_text)):
                raise ValueError("Kurzer Normalpfad benötigt Anker, Zeilenprompt und deutschen Prewarm")
            audio = [main_anchor_audio, line_audio, prewarm_audio]
            texts = [main_anchor_text, line_text, prewarm_text]
        elif prewarm_enabled:
            if main_anchor_audio is None or not main_anchor_text or prewarm_audio is None or not prewarm_text:
                raise ValueError("Kurze Zeile ohne Performance-Prompt benötigt Anker und deutschen Prewarm")
            audio = [main_anchor_audio, prewarm_audio]
            texts = [main_anchor_text, prewarm_text]
        elif not performance_prompt_missing and line_audio is not None and line_text:
            audio.append(line_audio)
            texts.append(line_text)
    elif method == "without_emotion":
        if prewarm_enabled:
            if main_anchor_audio is None or not main_anchor_text or prewarm_audio is None or not prewarm_text:
                raise ValueError("Kurze Eskalation benötigt Anker und deutschen Prewarm")
            audio = [main_anchor_audio, prewarm_audio]
            texts = [main_anchor_text, prewarm_text]
    # main_anchor_only deliberately uses only -r and target text.
    if audio:
        if len(audio) != len(texts):
            raise ValueError("Prompt-Audio und -Text müssen paarweise in gleicher Reihenfolge vorliegen")
        prompt_wav = materialized_prompt_wav if len(audio) > 1 else audio[0]
        if prompt_wav is None:
            raise ValueError("Mehrteiliger Prompt benötigt die materialisierte kombinierte WAV")
        argv.extend(["--prompt-wav", str(prompt_wav),
                     "--prompt-text", _prompt_text(texts)])
    argv.extend(["--seed", str(seed), "-o", str(output), *map(str, models)])
    return argv
