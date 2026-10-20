"""
Alfred — síntese de voz (edge-tts) e reprodução de áudio.

Extraído de services/briefing/tech-briefing.py para reuso pelo inbox-assistant.

    from _shared.lib_tts import generate_tts, tts_save, limpar_para_fala, tocar_audio
"""

import asyncio
import os
import re
import shutil
import subprocess
from pathlib import Path

import edge_tts

from _shared.lib_alfred import log

DEFAULT_VOICE = "pt-BR-FranciscaNeural"


async def generate_tts(text: str, voice: str, output_path: Path):
    """Mesma assinatura usada pelo briefing: gera .mp3 via edge-tts."""
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(output_path))


def tts_save(text: str, voice: str, output_path: Path) -> bool:
    """Wrapper síncrono. Retorna True em sucesso; loga e retorna False em erro."""
    if not text or not text.strip():
        log("[tts] Texto vazio — pulando")
        return False
    try:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        asyncio.run(generate_tts(text, voice or DEFAULT_VOICE, Path(output_path)))
        return Path(output_path).exists() and Path(output_path).stat().st_size > 0
    except Exception as e:
        log(f"[tts] Erro ao gerar {Path(output_path).name}: {e}")
        return False


def limpar_para_fala(md: str) -> str:
    """
    Fallback quando o LLM não devolve roteiro: remove markdown básico
    para que o texto escrito seja falável.
    """
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", md)      # [texto](url) → texto
    text = re.sub(r"https?://\S+", "", text)                 # URLs soltas
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)       # headers
    text = re.sub(r"[*_`>|]", "", text)                       # ênfase, código, citações, tabelas
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)      # bullets
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def tocar_audio(path: Path, bloquear: bool = False) -> bool:
    """
    Toca um .mp3 com o primeiro player disponível (ffplay, mpv, xdg-open).
    Respeita INBOX_AUDIO_PLAYER no .env se definido (comando completo).
    """
    path = Path(path)
    if not path.exists():
        log(f"[audio] Arquivo não existe: {path}")
        return False

    custom = os.getenv("INBOX_AUDIO_PLAYER", "").strip()
    candidatos: list[list[str]] = []
    if custom:
        candidatos.append(custom.split() + [str(path)])
    if shutil.which("ffplay"):
        candidatos.append(["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(path)])
    if shutil.which("mpv"):
        candidatos.append(["mpv", "--no-video", "--really-quiet", str(path)])
    if shutil.which("xdg-open"):
        candidatos.append(["xdg-open", str(path)])

    for cmd in candidatos:
        try:
            if bloquear:
                r = subprocess.run(cmd, timeout=600, capture_output=True, text=True)
                if r.returncode != 0:   # ex.: ffplay sem dispositivo de áudio → tenta o próximo player
                    log(f"[audio] {cmd[0]} saiu com {r.returncode}: {(r.stderr or '').strip()[:160]}")
                    continue
            else:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
            return True
        except Exception as e:
            log(f"[audio] Falha com {cmd[0]}: {e}")
    log("[audio] Nenhum player disponível (ffplay/mpv/xdg-open)")
    return False
