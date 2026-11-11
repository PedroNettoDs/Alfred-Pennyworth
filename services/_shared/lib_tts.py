"""
Alfred — síntese de voz e reprodução de áudio.

Motor de generate_tts, tts_save e fluxo_tts: Piper, local e em CPU (nada sai da máquina). As vozes (.onnx) ficam em
PIPER_VOZES_DIR (padrão ~/.local/share/piper-voices); baixar com
`venv/bin/python -m piper.download_voices --download-dir <pasta> pt_BR-faber-medium pt_BR-cadu-medium`.
Medido em 10/11/2026 num resumo de 15 s: 0,5 s de síntese + 0,2 s de ffmpeg (o edge-tts levava 1,3 a 4,3 s),
mais 1,8 s para carregar a voz na primeira vez de cada processo.

Extraído de services/briefing/tech-briefing.py para reuso pelo inbox-assistant.

    from _shared.lib_tts import generate_tts, fluxo_tts, tts_save, limpar_para_fala, tocar_audio
"""

import asyncio
import os
import re
import shutil
import subprocess
import threading
from typing import Optional
from pathlib import Path

from _shared.lib_alfred import log

DEFAULT_VOICE = "pt-BR-FranciscaNeural"

# perfis.yml e os chamadores ainda nomeiam as vozes do edge-tts; aqui cada uma vira uma voz do Piper.
# O Piper só tem vozes masculinas em pt-BR. Um nome fora da tabela é usado como nome de voz do Piper.
VOZES_PIPER = {"pt-BR-AntonioNeural": "pt_BR-faber-medium", "pt-BR-FranciscaNeural": "pt_BR-cadu-medium"}
_VOZES: dict = {}                  # voz carregada uma vez por processo (~1,8 s)
_VOZES_LOCK = threading.Lock()


def _voz_piper(voice: str):
    from piper import PiperVoice   # aqui dentro: quem só toca áudio importa este módulo sem carregar o Piper
    nome = VOZES_PIPER.get(voice or DEFAULT_VOICE, voice)
    with _VOZES_LOCK:
        if nome not in _VOZES:
            pasta = Path(os.getenv("PIPER_VOZES_DIR", "").strip() or Path.home() / ".local/share/piper-voices")
            _VOZES[nome] = PiperVoice.load(str(pasta / f"{nome}.onnx"))
        return _VOZES[nome]


def _ffmpeg_mp3(taxa: int, saida: str) -> list[str]:
    """ffmpeg que lê o PCM do Piper (16 bits, mono) na entrada padrão e grava .mp3 em `saida`.
    Sai sempre em 24 kHz, como o edge-tts saía: o podcast junta as falas com silêncio e vinheta nessa taxa."""
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "s16le", "-ar", str(taxa), "-ac", "1",
            "-i", "pipe:0", "-f", "mp3", "-ar", "24000", "-b:a", "48k", "-flush_packets", "1", saida]


def _salvar_piper(text: str, voice: str, output_path: Path):
    """Texto → .mp3. Levanta em erro (voz não baixada, ffmpeg)."""
    voz = _voz_piper(voice)
    pcm = b"".join(frase.audio_int16_bytes for frase in voz.synthesize(text))
    r = subprocess.run(_ffmpeg_mp3(voz.config.sample_rate, str(output_path)), input=pcm, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg: {r.stderr.decode(errors='replace').strip()[:300]}")


async def generate_tts(text: str, voice: str, output_path: Path):
    """Mesma assinatura usada pelo briefing: gera o .mp3; levanta em erro."""
    await asyncio.to_thread(_salvar_piper, text, voice, Path(output_path))


async def fluxo_tts(text: str, voice: str):
    """Os pedaços do .mp3 conforme o Piper sintetiza, frase a frase, sem esperar o áudio inteiro.
    Para quem toca enquanto baixa."""
    voz = await asyncio.to_thread(_voz_piper, voice)
    proc = await asyncio.create_subprocess_exec(
        *_ffmpeg_mp3(voz.config.sample_rate, "pipe:1"), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)

    async def alimentar():
        try:
            frases = iter(voz.synthesize(text))
            while (frase := await asyncio.to_thread(next, frases, None)) is not None:
                proc.stdin.write(frase.audio_int16_bytes)
                await proc.stdin.drain()
        finally:
            proc.stdin.close()

    tarefa = asyncio.create_task(alimentar())
    try:
        while pedaco := await proc.stdout.read(4096):
            yield pedaco
        await tarefa
    finally:
        tarefa.cancel()
        if proc.returncode is None:
            proc.kill()


def tts_save(text: str, voice: str, output_path: Path) -> bool:
    """Wrapper síncrono. Retorna True em sucesso; loga e retorna False em erro."""
    if not text or not text.strip():
        log("[tts] Texto vazio — pulando")
        return False
    try:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        _salvar_piper(text, voice, Path(output_path))
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


def iniciar_audio(path: Path) -> Optional["subprocess.Popen"]:
    """
    Começa a tocar sem bloquear e devolve o processo (para parar com parar_audio). Só players que
    tocam e saem sozinhos (INBOX_AUDIO_PLAYER, ffplay, mpv, pw-play) — xdg-open não serve aqui.
    """
    path = Path(path)
    if not path.exists():
        return None
    custom = os.getenv("INBOX_AUDIO_PLAYER", "").strip()
    candidatos: list[list[str]] = []
    if custom:
        candidatos.append(custom.split() + [str(path)])
    if shutil.which("ffplay"):
        candidatos.append(["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(path)])
    if shutil.which("mpv"):
        candidatos.append(["mpv", "--no-video", "--really-quiet", str(path)])
    for cmd in candidatos:
        try:
            return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    start_new_session=True)
        except Exception as e:
            log(f"[audio] Falha com {cmd[0]}: {e}")
    return None


def tocando(proc) -> bool:
    return proc is not None and proc.poll() is None


def parar_audio(proc) -> None:
    if tocando(proc):
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

