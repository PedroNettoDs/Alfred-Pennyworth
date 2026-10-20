"""
Alfred — transcrição de áudio 100% local.

    from _shared.lib_audio import transcrever_audio, para_wav, duracao_segundos

Ordem de tentativa:
  1. Ollama com modelo de entrada de áudio (gemma4). O áudio vai em base64 no campo
     `images` do /api/generate — o Ollama usa o mesmo campo para imagem e áudio.
     Só aceita WAV/MP3 (OGG/Opus do WhatsApp falha com "Failed to load image or audio
     file"), por isso tudo passa antes pelo ffmpeg → WAV mono 16 kHz.
     O gemma4 ouve ~30 s por chamada → áudios longos são fatiados em segmentos.
  2. faster-whisper, se o pacote estiver instalado (pip install faster-whisper).
  3. "" — o chamador decide o que fazer sem transcrição.

Nenhuma função levanta exceção: erros são logados e voltam no dict de resultado.
"""

import base64
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Union

from _shared.lib_alfred import log, ollama_generate

SEGMENTO_S = 30            # gemma4 só processa ~30 s de áudio por chamada
MAX_SEGUNDOS_DEFAULT = 600 # 10 min — acima disso o áudio é cortado (e avisado)

PROMPT_TRANSCRICAO = (
    "Transcreva o áudio em português brasileiro, literalmente, palavra por palavra. "
    "Responda SOMENTE com a transcrição: sem introdução, sem comentários, sem aspas, "
    "sem marcações. Se não houver fala alguma, responda exatamente: [sem fala]"
)

_EXT_POR_MIME = {
    "audio/ogg": ".ogg", "audio/opus": ".ogg", "audio/mpeg": ".mp3", "audio/mp3": ".mp3",
    "audio/mp4": ".m4a", "audio/m4a": ".m4a", "audio/aac": ".aac", "audio/wav": ".wav",
    "audio/x-wav": ".wav", "audio/webm": ".webm", "audio/amr": ".amr", "video/mp4": ".mp4",
}


def ffmpeg_disponivel() -> bool:
    return bool(shutil.which("ffmpeg"))


def modelo_transcricao() -> str:
    return os.getenv("MODEL_TRANSCRIBE", "").strip() or os.getenv("MODEL_CHAT", "").strip() or "gemma4:12b"


def extensao_por_mime(mimetype: str, padrao: str = ".ogg") -> str:
    base = (mimetype or "").split(";")[0].strip().lower()
    return _EXT_POR_MIME.get(base, padrao)


def duracao_segundos(path: Path) -> Optional[float]:
    """Duração via ffprobe (None se indisponível)."""
    if not shutil.which("ffprobe"):
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None
    except Exception:
        return None


def para_wav(src: Path, dst: Path, sample_rate: int = 16000, max_segundos: Optional[float] = None) -> bool:
    """Converte qualquer áudio para WAV mono PCM 16-bit (o formato que o gemma4 espera)."""
    if not ffmpeg_disponivel():
        log("[audio] ffmpeg não encontrado — não dá pra converter o áudio")
        return False
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src)]
    if max_segundos:
        cmd += ["-t", str(int(max_segundos))]
    cmd += ["-vn", "-ac", "1", "-ar", str(sample_rate), "-c:a", "pcm_s16le", str(dst)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            log(f"[audio] ffmpeg falhou: {(r.stderr or '').strip()[:200]}")
            return False
        return Path(dst).exists() and Path(dst).stat().st_size > 44
    except Exception as e:
        log(f"[audio] ffmpeg erro: {e}")
        return False


def _segmentar(wav: Path, pasta: Path, segundos: int = SEGMENTO_S) -> list[Path]:
    """Fatia o WAV em pedaços de N segundos (gemma4 ignora o que passa de ~30 s)."""
    dur = duracao_segundos(wav) or 0
    if dur <= segundos + 1:
        return [wav]
    padrao = pasta / "seg_%03d.wav"
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), "-f", "segment",
             "-segment_time", str(segundos), "-c", "copy", str(padrao)],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode != 0:
            log(f"[audio] segmentação falhou: {(r.stderr or '')[:200]} — usando o áudio inteiro")
            return [wav]
    except Exception as e:
        log(f"[audio] segmentação erro: {e}")
        return [wav]
    partes = sorted(pasta.glob("seg_*.wav"))
    return partes or [wav]


def _transcrever_ollama(wav: Path, model: Optional[str] = None) -> str:
    b64 = base64.b64encode(wav.read_bytes()).decode("ascii")
    out = ollama_generate(
        PROMPT_TRANSCRICAO, model=model or modelo_transcricao(), timeout=240,
        images=[b64], think=False, options={"temperature": 0.0, "num_predict": 700},
    )
    out = out.strip().strip('"').strip()
    if not out or out.lower().startswith("[sem fala]"):
        return ""
    return out


def _transcrever_whisper(wav: Path) -> str:
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError:
        return ""
    try:
        tamanho = os.getenv("WHISPER_MODEL", "small")
        modelo = WhisperModel(tamanho, device="auto", compute_type="auto")
        segs, _info = modelo.transcribe(str(wav), language="pt", vad_filter=True)
        return " ".join(s.text.strip() for s in segs).strip()
    except Exception as e:
        log(f"[audio] faster-whisper falhou: {e}")
        return ""


def transcrever_audio(
    fonte: Union[Path, str, bytes],
    mimetype: str = "",
    max_segundos: int = MAX_SEGUNDOS_DEFAULT,
    model: Optional[str] = None,
) -> dict:
    """
    Transcreve um áudio (caminho, bytes ou string base64).

    Retorna {"texto": str, "metodo": "ollama" | "whisper" | "", "duracao_s": float|None,
             "segmentos": int, "cortado": bool, "erro": str|None}.
    """
    res = {"texto": "", "metodo": "", "duracao_s": None, "segmentos": 0, "cortado": False, "erro": None}
    with tempfile.TemporaryDirectory(prefix="alfred-audio-") as tmp:
        pasta = Path(tmp)
        # 1. materializa a fonte num arquivo
        if isinstance(fonte, bytes):
            src = pasta / f"entrada{extensao_por_mime(mimetype)}"
            src.write_bytes(fonte)
        elif isinstance(fonte, (str, Path)) and Path(str(fonte)).is_file():
            src = Path(str(fonte))
        elif isinstance(fonte, str) and len(fonte) > 100:
            try:
                raw = fonte.split(",", 1)[1] if fonte.startswith("data:") else fonte
                src = pasta / f"entrada{extensao_por_mime(mimetype)}"
                src.write_bytes(base64.b64decode(raw))
            except Exception as e:
                res["erro"] = f"base64 inválido: {e}"
                return res
        else:
            res["erro"] = "fonte de áudio vazia ou inexistente"
            return res

        dur = duracao_segundos(src)
        res["duracao_s"] = round(dur, 1) if dur else None
        if dur and max_segundos and dur > max_segundos:
            res["cortado"] = True
            log(f"[audio] áudio de {dur:.0f}s — transcrevendo só os primeiros {max_segundos}s")

        # 2. WAV mono 16 kHz
        wav = pasta / "entrada.wav"
        if not para_wav(src, wav, max_segundos=max_segundos if res["cortado"] else None):
            res["erro"] = "não foi possível converter o áudio (ffmpeg)"
            return res

        # 3. gemma4 via Ollama, por segmento
        partes = _segmentar(wav, pasta)
        res["segmentos"] = len(partes)
        textos = []
        for i, parte in enumerate(partes, 1):
            t = _transcrever_ollama(parte, model)
            if t:
                textos.append(t)
            elif i == 1:
                break  # primeiro segmento falhou → modelo sem áudio? vai pro fallback
        if textos:
            res["texto"] = " ".join(textos).strip()
            res["metodo"] = "ollama"
            return res

        # 4. fallback faster-whisper
        t = _transcrever_whisper(wav)
        if t:
            res["texto"] = t
            res["metodo"] = "whisper"
            return res

        res["erro"] = ("nenhum transcritor respondeu — confira se MODEL_TRANSCRIBE/MODEL_CHAT tem capacidade "
                       "'audio' no Ollama (gemma4) ou instale faster-whisper")
        log(f"[audio] {res['erro']}")
        return res


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Uso: python3 lib_audio.py <arquivo de áudio>")
        sys.exit(1)
    import json
    print(json.dumps(transcrever_audio(Path(sys.argv[1])), ensure_ascii=False, indent=2))
