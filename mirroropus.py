#!/usr/bin/env python3
"""
mirroropus.py

Espelha uma biblioteca de música (master) para uma cópia "leve" em Opus,
mantendo a estrutura de pastas.

  - Lossy (MP3, Opus, Ogg Vorbis, AAC, WMA lossy, Musepack, ...) -> copiado como está
  - Lossless (FLAC, WAV, AIFF, ALAC, APE, WV, TTA, TAK, SHN, WMA lossless,
    Ogg-FLAC, DSD (.dsf/.dff), CAF/AIFC lossless, ...)           -> convertido para .opus
  - Playlists (.cue, .m3u, .m3u8)                                -> copiadas e reescritas
    para apontar para os .opus (ex.: FILE "faixa.flac" passa a FILE "faixa.opus"),
    mantendo a codificação e as quebras de linha originais
  - Tudo o resto                                                -> copiado como está:
    accurip (.accurip), .log, .nfo, .txt, .lrc, capas (.jpg/.png),
    scans e booklets (ex.: [album]/scans/, .7z/.zip/.rar/.pdf com scans),
    e qualquer outro arquivo ou subpasta. A estrutura é mantida igual à origem.

Encoder: opusenc (opus-tools). Copia tags e capas embutidas de FLAC.
Formatos que o opusenc não lê passam primeiro pelo ffmpeg, que decodifica
para FLAC em pipe (com tags e capa), e esse pipe é lido pelo opusenc.

Modo incremental (por padrão): só processa o que é novo ou mudou na
origem (mtime diferente do destino, ou tamanho diferente nas cópias diretas).
O bitrate usado fica guardado num arquivo de estado ao lado da pasta de destino
(.NOME.mirroropus.json). Se você mudar -b, só os arquivos convertidos são
refeitos; as cópias diretas não são tocadas. --overwrite refaz tudo.

Sempre ignorados (lixo): dotfiles/pastas com ponto (.DS_Store, ._*, .Trash-1000,
.Trashes, .Spotlight-V100, ...), @eaDir, .AppleDouble, Thumbs.db, desktop.ini,
$RECYCLE.BIN, "System Volume Information", etc. Exceção: .ndignore (Navidrome)
é copiado. Symlinks são sempre ignorados e nunca seguidos.

--prune apaga do destino tudo o que já não tem correspondente na origem
(arquivos removidos/renomeados na master). Apaga na hora, mas:
  - sempre avisa quantos arquivos e álbuns serão apagados;
  - não roda sem -y/--yes se forem afetados mais álbuns do que
    --prune-limit (por padrão 3);
  - nunca roda se a origem não tiver nenhum arquivo de áudio
    (disco desmontado, caminho errado, ...);
  - nunca roda se alguma pasta da origem não pôde ser lida (permissões, erro de I/O),
    porque os arquivos dela pareceriam "removidos".
Com --dry-run, só lista o que seria apagado.
Arquivos temporários *.part nunca contam para o prune e são limpos no início.

Por padrão o script baixa a própria prioridade (nice 10) para não atrapalhar
outros serviços no servidor (ex.: Navidrome). Use --nice 0 para desativar.

Uso:
  ./mirroropus.py ORIGEM DESTINO
  ./mirroropus.py ORIGEM DESTINO -b 160 -j 8
  ./mirroropus.py ORIGEM DESTINO --overwrite
  ./mirroropus.py ORIGEM DESTINO --dry-run
  ./mirroropus.py ORIGEM DESTINO --prune
  ./mirroropus.py ORIGEM DESTINO --prune -y --prune-limit 20

Requisitos: opusenc; ffmpeg + ffprobe (para ALAC/APE/WV/TTA/TAK/SHN/DSD/WMA-lossless/Ogg-FLAC/.m4a/.caf).
"""
from __future__ import annotations

import argparse
import codecs
import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

# Lidos diretamente pelo opusenc (PCM/AIFF/FLAC simples)
OPUSENC_NATIVE = {".flac", ".wav", ".aif", ".aiff"}
# Lossless que passam sempre pelo ffmpeg (opusenc não lê)
VIA_FFMPEG = {
    ".ape", ".wv", ".tta", ".tak", ".shn",
    ".dsf", ".dff", ".dsd",
}
# Contentores ambíguos: só convertem se o codec real for lossless (ver probe_codec)
# .m4a/.mp4: alac -> converte, aac -> copia
# .wma: wmalossless -> converte, resto (wmav1/wmav2/wmapro/wmavoice) -> copia
# .ogg/.oga: flac -> converte, resto (vorbis/opus/...) -> copia
# .caf/.aifc: alac/pcm/flac -> converte, resto (aac/mp3/...) -> copia
NEEDS_PROBE = {".m4a", ".mp4", ".wma", ".ogg", ".oga", ".caf", ".aifc"}
# Playlists com referências a arquivos de áudio (reescritas no destino)
PLAYLIST_EXTS = {".cue", ".m3u", ".m3u8"}

CONVERT_ACTIONS = ("opusenc", "ffmpeg")

TIMESTAMP_TOLERANCE = 2.0  # segundos (sistemas de arquivos com mtime grosseiro)

# Extensões consideradas "música" (para contar álbuns e validar a origem).
# Inclui lossy + lossless + contentores ambíguos.
AUDIO_EXTS = {
    ".opus", ".mp3", ".m4a", ".mp4", ".aac", ".ogg", ".oga", ".wma",
    ".flac", ".wav", ".aif", ".aiff", ".aifc", ".caf",
    ".ape", ".wv", ".tta", ".tak", ".shn", ".mpc", ".mpp", ".mp+",
    ".dsf", ".dff", ".dsd",
}

# Pastas de lixo que nunca são percorridas nem espelhadas.
IGNORE_DIRS = {
    "@eadir", ".appledouble",
    "$recycle.bin", "system volume information",
    ".trashes", ".spotlight-v100", ".fseventsd", ".temporaryitems",
    ".documentrevisions-v100", ".pk installsandboxmanager",
    "found.000",
}
# Arquivos de lixo que nunca são espelhados.
IGNORE_FILES = {
    ".ds_store", "thumbs.db", "desktop.ini", ".apdisk",
}
# Dotfiles que têm significado para o servidor de música e DEVEM ser copiados.
KEEP_DOTFILES = {".ndignore"}


def _is_junk_dir(name: str) -> bool:
    return name.startswith(".") or name.lower() in IGNORE_DIRS


def _is_junk_file(name: str) -> bool:
    n = name.lower()
    if n in KEEP_DOTFILES:
        return False
    return name.startswith(".") or n in IGNORE_FILES  # inclui ._* (AppleDouble)


@dataclass
class Job:
    src: Path
    dst: Path
    action: str  # "copy" | "opusenc" | "ffmpeg" | "rewrite"
    rel: str
    # Só para playlists: caminhos (relativos à pasta da playlist, com "/")
    # dos arquivos que viram .opus no destino.
    convert_set: set[str] | None = None


# --------------------------------------------------------------------------
# Planeamento
# --------------------------------------------------------------------------
def probe_codec(path: Path) -> str:
    """Retorna o codec do 1.º stream de áudio (minúsculas) ou "" se falhar."""
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_name", "-of", "csv=p=0",
                str(path),
            ],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as e:
        print(f"AVISO: ffprobe falhou em {path}: {e}", file=sys.stderr, flush=True)
        return ""
    if r.returncode != 0:
        print(f"AVISO: ffprobe não conseguiu ler {path}: "
              f"{r.stderr.strip()[:200]}", file=sys.stderr, flush=True)
        return ""
    return r.stdout.strip().split(",")[0].strip().lower()


def classify_lossless(ext: str, codec: str) -> bool:
    """Diz se uma extensão ambígua deve ser convertida (codec lossless)."""
    if ext in (".m4a", ".mp4"):
        return codec == "alac"
    if ext == ".wma":
        return codec == "wmalossless"
    if ext in (".ogg", ".oga"):
        return codec == "flac"
    if ext in (".caf", ".aifc"):
        return codec == "alac" or codec == "flac" or codec.startswith("pcm_")
    return False


def build_plan(src_root: Path, dst_root: Path, jobs_hint: int
               ) -> tuple[list[Job], dict[str, int], list[OSError]]:
    raw: list[tuple[Path, str, str]] = []  # (src, rel, ext)
    skipped = {"junk": 0, "symlink": 0}
    to_probe: list[Path] = []
    walk_errors: list[OSError] = []

    for dirpath, dirnames, filenames in os.walk(
            src_root, followlinks=False, onerror=walk_errors.append):
        # Não desce para pastas de lixo nem links simbólicos.
        keep: list[str] = []
        for d in dirnames:
            full = Path(dirpath) / d
            if full.is_symlink():
                skipped["symlink"] += 1
                continue
            if _is_junk_dir(d):
                skipped["junk"] += 1
                continue
            keep.append(d)
        dirnames[:] = keep

        for name in filenames:
            src = Path(dirpath) / name
            if src.is_symlink():
                skipped["symlink"] += 1
                continue
            if _is_junk_file(name):
                skipped["junk"] += 1
                continue
            ext = src.suffix.lower()
            rel = str(src.relative_to(src_root))
            raw.append((src, rel, ext))
            if ext in NEEDS_PROBE:
                to_probe.append(src)

    # ffprobe só é obrigatório se houver arquivos ambíguos.
    codecs_by_path: dict[Path, str] = {}
    if to_probe:
        if shutil.which("ffprobe") is None:
            sys.exit("ffprobe é necessário para distinguir lossless de lossy "
                     "(.m4a/.wma/.ogg/.caf) mas não foi encontrado.")
        workers = max(1, min(jobs_hint or 4, len(to_probe), (os.cpu_count() or 4) * 2))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(probe_codec, p): p for p in to_probe}
            for fut in as_completed(futs):
                p = futs[fut]
                try:
                    codecs_by_path[p] = fut.result()
                except Exception as e:  # noqa: BLE001
                    print(f"AVISO: erro ao detectar codec de {p}: {e}",
                          file=sys.stderr, flush=True)
                    codecs_by_path[p] = ""

    jobs: list[Job] = []
    for src, rel, ext in raw:
        action = "copy"
        if ext in OPUSENC_NATIVE:
            action = "opusenc"
        elif ext in VIA_FFMPEG:
            action = "ffmpeg"
        elif ext in NEEDS_PROBE:
            codec = codecs_by_path.get(src, "")
            if not codec:
                print(f"AVISO: sem codec detectado, a copiar como está: {rel}",
                      file=sys.stderr, flush=True)
            elif classify_lossless(ext, codec):
                action = "ffmpeg"

        dst = dst_root / rel
        if action in CONVERT_ACTIONS:
            dst = dst.with_suffix(".opus")
        jobs.append(Job(src, dst, action, rel))

    # Se dois arquivos colidirem no mesmo destino (ex.: faixa.flac e faixa.opus),
    # ganha a cópia direta; os restantes são avisados e ignorados.
    jobs.sort(key=lambda j: (j.action not in ("copy", "rewrite"), j.rel))
    seen: dict[Path, Job] = {}
    unique: list[Job] = []
    for j in jobs:
        if j.dst in seen:
            print(f"AVISO: colisão de destino, ignorando {j.rel} "
                  f"(já existe {seen[j.dst].rel})", file=sys.stderr, flush=True)
            continue
        seen[j.dst] = j
        unique.append(j)
    unique.sort(key=lambda j: j.rel)

    attach_playlist_maps(unique)
    return unique, skipped, walk_errors


def attach_playlist_maps(jobs: list[Job]) -> None:
    """Playlists que referenciam arquivos convertidos passam a "rewrite".

    Cada playlist recebe o conjunto de caminhos (relativos à sua pasta, incluindo
    subpastas como CD1/01.flac) que viram .opus. Se a playlist não referencia
    nada convertido nessa árvore, continua como cópia simples.
    """
    playlists = [j for j in jobs
                 if j.action == "copy" and j.src.suffix.lower() in PLAYLIST_EXTS]
    if not playlists:
        return
    play_dirs = {Path(j.rel).parent for j in playlists}
    by_dir: dict[Path, set[str]] = defaultdict(set)
    for j in jobs:
        if j.action not in CONVERT_ACTIONS:
            continue
        rel = Path(j.rel)
        d = rel.parent
        while True:
            if d in play_dirs:
                by_dir[d].add(rel.relative_to(d).as_posix())
            if d == d.parent:
                break
            d = d.parent
    for j in playlists:
        conv = by_dir.get(Path(j.rel).parent)
        if conv:
            j.action = "rewrite"
            j.convert_set = conv


_CUE_FILE_QUOTED = re.compile(r'^(\s*FILE\s+")([^"]*)(".*)$', re.IGNORECASE)
_CUE_FILE_BARE = re.compile(r'^(\s*FILE\s+)(\S+)(\s+\S+\s*)$', re.IGNORECASE)


def _swap_ext(ref: str, convert_set: set[str]) -> str:
    """Troca a extensão de ref por .opus se ref aponta para um arquivo convertido."""
    norm = ref.replace("\\", "/")
    key = norm[2:] if norm.startswith("./") else norm
    if key in convert_set:
        base, _dot, _ext = ref.rpartition(".")
        return base + ".opus"
    return ref


def rewrite_playlist_text(text: str, convert_set: set[str], is_cue: bool) -> str:
    """Reescreve referências a arquivos convertidos, linha a linha.

    Só mexe em referências que casam exatamente com um arquivo convertido
    (o nome completo, não um pedaço dele), por isso "01.flac" não estraga "101.flac".
    """
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        eol = line[len(body):]
        if is_cue:
            m = _CUE_FILE_QUOTED.match(body)
            if m:
                body = m.group(1) + _swap_ext(m.group(2), convert_set) + m.group(3)
            else:
                m = _CUE_FILE_BARE.match(body)
                if m:
                    body = m.group(1) + _swap_ext(m.group(2), convert_set) + m.group(3)
        elif body.strip() and not body.lstrip().startswith("#"):
            lead = body[:len(body) - len(body.lstrip())]
            tail = body[len(body.rstrip()):]
            body = lead + _swap_ext(body.strip(), convert_set) + tail
        out.append(body + eol)
    return "".join(out)


def detect_text_encoding(data: bytes) -> str:
    """BOM -> codificação do BOM; senão UTF-8 se válido; senão latin-1."""
    if data.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if data[:2] in (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE):
        return "utf-16"
    try:
        data.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "latin-1"


def up_to_date(job: Job) -> bool:
    """O destino foi feito a partir desta versão da origem?

    O mtime do destino é copiado da origem, então tem que ser (quase) igual:
    se a origem for substituída por uma versão mais antiga (restore de backup)
    também é detectado. Para cópias diretas o tamanho também tem que bater.
    """
    try:
        d = job.dst.stat()
        s = job.src.stat()
    except FileNotFoundError:
        return False
    if d.st_size == 0:
        return False
    if job.action == "copy" and d.st_size != s.st_size:
        return False
    return abs(d.st_mtime - s.st_mtime) <= TIMESTAMP_TOLERANCE


def is_current(job: Job, args: argparse.Namespace) -> bool:
    if args.overwrite:
        return False
    if args.redo_converted and job.action in CONVERT_ACTIONS:
        return False
    return up_to_date(job)


# --------------------------------------------------------------------------
# Estado (bitrate usado na última execução completa)
# --------------------------------------------------------------------------
def state_path(dst_root: Path) -> Path:
    # Fica FORA da pasta de destino para o Navidrome nunca ver este arquivo.
    return dst_root.parent / f".{dst_root.name}.mirroropus.json"


def load_state(dst_root: Path) -> dict:
    try:
        data = json.loads(state_path(dst_root).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(dst_root: Path, bitrate: int) -> None:
    try:
        state_path(dst_root).write_text(
            json.dumps({"bitrate": bitrate}) + "\n", encoding="utf-8")
    except OSError as e:
        print(f"AVISO: não consegui gravar o estado do bitrate "
              f"({state_path(dst_root)}): {e}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Prune
# --------------------------------------------------------------------------
def find_orphans(dst_root: Path, expected: set[Path]) -> list[Path]:
    """Arquivos no destino que não correspondem a nada na origem."""
    if not dst_root.is_dir():
        return []
    orphans: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(dst_root):
        for name in filenames:
            if name.endswith(".part"):
                continue  # temporários de rodadas interrompidas; limpos à parte
            p = Path(dirpath) / name
            if p not in expected:
                orphans.append(p)
    return sorted(orphans)


def clean_stale_parts(dst_root: Path, dry_run: bool) -> int:
    """Apaga *.part de rodadas interrompidas. Retorna quantos encontrou."""
    if not dst_root.is_dir():
        return 0
    parts = list(dst_root.rglob("*.part"))
    if not parts:
        return 0
    if dry_run:
        print(f"Encontrados {len(parts)} arquivo(s) temporário(s) *.part "
              f"(seriam removidos).", flush=True)
        return len(parts)
    n = 0
    for p in parts:
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    if n:
        print(f"Removidos {n} arquivo(s) temporário(s) *.part de rodada anterior.",
              flush=True)
    return n


def remove_empty_dirs(dst_root: Path) -> int:
    removed = 0
    for dirpath, _dirnames, _filenames in os.walk(dst_root, topdown=False):
        p = Path(dirpath)
        if p == dst_root:
            continue
        try:
            p.rmdir()  # só funciona se estiver vazia
            removed += 1
        except OSError:
            pass
    return removed


# --------------------------------------------------------------------------
# Encoding
# --------------------------------------------------------------------------
def encode_opusenc(src: Path, tmp: Path, bitrate: int) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["opusenc", "--quiet", "--bitrate", str(bitrate), str(src), str(tmp)],
            capture_output=True, text=True, timeout=3600,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return r.returncode == 0, r.stderr.strip()


def _run_pipeline(ff_cmd: list[str], enc_cmd: list[str]) -> tuple[bool, str]:
    ff = enc = None
    try:
        ff = subprocess.Popen(ff_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        enc = subprocess.Popen(enc_cmd, stdin=ff.stdout, stderr=subprocess.PIPE)
        ff.stdout.close()  # o SIGPIPE passa para o ffmpeg se o opusenc sair
        _, enc_err = enc.communicate(timeout=3600)
        _, ff_err = ff.communicate(timeout=60)
        ok = ff.returncode == 0 and enc.returncode == 0
        msg = ((ff_err or b"") + (enc_err or b"")).decode(errors="replace").strip()
        return ok, msg
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    finally:
        for p in (enc, ff):
            if p is not None and p.poll() is None:
                try:
                    p.kill()
                except OSError:
                    pass


def encode_ffmpeg(src: Path, tmp: Path, bitrate: int) -> tuple[bool, str]:
    base = [
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(src),
        "-map", "0:a:0", "-map_metadata", "0",
    ]
    audio = ["-c:a", "flac", "-compression_level", "0", "-f", "flac", "pipe:1"]
    cover = ["-map", "0:v:0?", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    enc_cmd = ["opusenc", "--quiet", "--bitrate", str(bitrate), "-", str(tmp)]

    # 1.ª tentativa com capa embebida; se falhar, 2.ª sem capa
    ok, msg = _run_pipeline(base + cover + audio, enc_cmd)
    if ok:
        return True, ""
    ok2, msg2 = _run_pipeline(base + audio, enc_cmd)
    return ok2, msg2 if not ok2 else ""


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------
def run_job(job: Job, args: argparse.Namespace) -> tuple[str, str]:
    if is_current(job, args):
        return "skipped", ""

    job.dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = job.dst.with_name(job.dst.name + ".part")
    try:
        if job.action == "copy":
            shutil.copy2(job.src, tmp)
        elif job.action == "rewrite":
            data = job.src.read_bytes()
            enc = detect_text_encoding(data)
            text = data.decode(enc)
            out = rewrite_playlist_text(
                text, job.convert_set or set(), job.src.suffix.lower() == ".cue")
            tmp.write_bytes(out.encode(enc))
            st = job.src.stat()
            os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        else:
            encoder = encode_opusenc if job.action == "opusenc" else encode_ffmpeg
            ok, err = encoder(job.src, tmp, args.bitrate)
            if (not ok or not tmp.exists() or tmp.stat().st_size == 0) \
                    and job.action == "opusenc":
                # Fallback: WAV/AIFF exóticos que o opusenc não lê
                # (ex.: WAV com MP3 dentro) tentam via ffmpeg.
                tmp.unlink(missing_ok=True)
                ok, err = encode_ffmpeg(job.src, tmp, args.bitrate)
            if not ok or not tmp.exists() or tmp.stat().st_size == 0:
                raise RuntimeError(err or "falha desconhecida")
            st = job.src.stat()
            os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        os.replace(tmp, job.dst)
        if job.action == "copy":
            return "copied", ""
        if job.action == "rewrite":
            return "rewritten", ""
        return "converted", ""
    except Exception as e:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        return "failed", str(e)


def human_size(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TiB"


def size_summary(jobs: list[Job]) -> str | None:
    src_total = dst_total = 0
    for j in jobs:
        try:
            s = j.src.stat().st_size
            d = j.dst.stat().st_size
        except OSError:
            continue  # falhou ou ainda não existe: fora da conta, para ser justa
        src_total += s
        dst_total += d
    if not src_total:
        return None
    return (f"Tamanho: origem {human_size(src_total)} -> destino "
            f"{human_size(dst_total)} ({dst_total / src_total * 100:.0f}%).")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Espelha uma biblioteca de música: lossless -> Opus, resto copiado.",
        epilog="Nota: ao mudar --bitrate, só os arquivos convertidos são refeitos. "
               "Use --overwrite para refazer absolutamente tudo.",
    )
    ap.add_argument("source", type=Path, help="pasta da biblioteca master")
    ap.add_argument("dest", type=Path, help="pasta de destino")
    ap.add_argument("-b", "--bitrate", type=int, default=128,
                    help="bitrate Opus em kbps (por padrão: 128)")
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4,
                    help="conversões em paralelo (por padrão: nº de CPUs)")
    ap.add_argument("--nice", type=int, default=10, metavar="N",
                    help="prioridade do processo, 0-19 (por padrão: 10; 0 desativa)")
    ap.add_argument("--overwrite", action="store_true",
                    help="refazer tudo, mesmo o que já existe no destino")
    ap.add_argument("--dry-run", action="store_true",
                    help="mostrar o que seria feito, sem escrever nada")
    ap.add_argument("--prune", action="store_true",
                    help="apagar do destino o que já não existe na origem")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="com --prune: permitir apagar mais álbuns do que o limite")
    ap.add_argument("--prune-limit", type=int, default=3, metavar="N",
                    help="com --prune: máximo de álbuns afetados sem -y (por padrão: 3)")
    args = ap.parse_args()

    if not 6 <= args.bitrate <= 512:
        sys.exit("Bitrate inválido: use 6-512 kbps.")
    if args.jobs < 1:
        sys.exit("--jobs tem que ser >= 1.")
    if not 0 <= args.nice <= 19:
        sys.exit("--nice tem que estar entre 0 e 19.")

    src_root = args.source.resolve()
    dst_root = args.dest.resolve()

    if not src_root.is_dir():
        sys.exit(f"Origem não é uma pasta: {src_root}")
    if dst_root == src_root or src_root in dst_root.parents:
        sys.exit("O destino não pode estar dentro da origem.")
    if dst_root in src_root.parents:
        sys.exit("A origem não pode estar dentro do destino.")

    if args.nice and hasattr(os, "nice"):
        try:
            os.nice(args.nice)  # opusenc/ffmpeg herdam
        except OSError:
            pass

    jobs, skipped, walk_errors = build_plan(src_root, dst_root, args.jobs)

    if any(j.action in CONVERT_ACTIONS for j in jobs) and shutil.which("opusenc") is None:
        sys.exit("opusenc não encontrado (instala o pacote opus-tools).")
    if any(j.action == "ffmpeg" for j in jobs) and shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg é necessário para ALAC/APE/WV/TTA/TAK/SHN/DSD/WMA-lossless "
                 "mas não foi encontrado.")

    # Bitrate mudou desde a última execução completa? Então refaz só os convertidos.
    prev_bitrate = load_state(dst_root).get("bitrate")
    bitrate_changed = isinstance(prev_bitrate, int) and prev_bitrate != args.bitrate
    args.redo_converted = bitrate_changed and not args.overwrite

    total = len(jobs)
    n_convert = sum(1 for j in jobs if j.action in CONVERT_ACTIONS)
    print(f"{total} arquivos na origem ({n_convert} a converter). "
          f"Bitrate: {args.bitrate} kbps. "
          f"Modo: {'overwrite' if args.overwrite else 'incremental'}.", flush=True)
    if skipped["junk"] or skipped["symlink"]:
        print(f"Ignorados: {skipped['junk']} de lixo, "
              f"{skipped['symlink']} links simbólicos.", flush=True)
    if args.redo_converted:
        print(f"Bitrate mudou ({prev_bitrate} -> {args.bitrate} kbps): "
              f"os arquivos convertidos serão refeitos.", flush=True)
    if walk_errors:
        print(f"AVISO: {len(walk_errors)} pasta(s) da origem não puderam ser lidas "
              f"(os arquivos delas NÃO foram espelhados):", file=sys.stderr, flush=True)
        for e in walk_errors[:10]:
            print(f"  {e.filename}: {e.strerror or e}", file=sys.stderr)
        if len(walk_errors) > 10:
            print(f"  ... e mais {len(walk_errors) - 10}", file=sys.stderr)

    # ---- Prune: planear e validar ANTES de fazer qualquer coisa ----
    orphans: list[Path] = []
    if args.prune:
        if walk_errors:
            sys.exit("--prune recusado: algumas pastas da origem não puderam ser lidas "
                     "e os arquivos delas pareceriam removidos. Nada foi apagado.")
        if not any(j.src.suffix.lower() in AUDIO_EXTS for j in jobs):
            sys.exit("--prune recusado: a origem não tem nenhum arquivo de áudio "
                     "(disco desmontado ou caminho errado?). Nada foi apagado.")

        expected = {j.dst for j in jobs}
        orphans = find_orphans(dst_root, expected)
        albums = sorted({p.parent for p in orphans if p.suffix.lower() in AUDIO_EXTS})
        too_many = len(albums) > args.prune_limit

        if orphans:
            print(f"\nAVISO: --prune vai apagar {len(orphans)} arquivo(s) do destino, "
                  f"afetando {len(albums)} álbum(ns).", file=sys.stderr, flush=True)

            if args.dry_run:
                for p in orphans:
                    print(f"[apagar] {p.relative_to(dst_root)}")
                if too_many and not args.yes:
                    print(f"(sem -y isso seria recusado: {len(albums)} álbuns > "
                          f"limite de {args.prune_limit})")
            elif too_many and not args.yes:
                print(f"\nRECUSADO: {len(albums)} álbuns seriam afetados, o limite sem -y "
                      f"é {args.prune_limit}. Nada foi feito (nem conversões nem exclusões).",
                      file=sys.stderr, flush=True)
                print("Álbuns afetados:", file=sys.stderr)
                for a in albums[:30]:
                    print(f"  {a.relative_to(dst_root)}", file=sys.stderr)
                if len(albums) > 30:
                    print(f"  ... e mais {len(albums) - 30}", file=sys.stderr)
                print("Se for realmente isso que você quer, rode de novo com -y "
                      "(ou use --dry-run para ver a lista completa).", file=sys.stderr)
                return 2
        else:
            print("\n--prune: nada para apagar no destino.", flush=True)

    clean_stale_parts(dst_root, args.dry_run)

    if args.dry_run:
        counts = {"skipped": 0, "copy": 0, "convert": 0, "rewrite": 0}
        for j in jobs:
            if is_current(j, args):
                counts["skipped"] += 1
                continue
            if j.action in CONVERT_ACTIONS:
                counts["convert"] += 1
                print(f"[converter] {j.rel}")
            elif j.action == "rewrite":
                counts["rewrite"] += 1
                print(f"[reescrever] {j.rel}")
            else:
                counts["copy"] += 1
                print(f"[copiar] {j.rel}")
        print(f"\nDry-run: {counts['convert']} a converter, {counts['copy']} a copiar, "
              f"{counts['rewrite']} playlists a reescrever, "
              f"{counts['skipped']} já atualizados"
              + (f", {len(orphans)} a apagar." if args.prune else "."), flush=True)
        return 1 if walk_errors else 0

    stats = {"skipped": 0, "copied": 0, "rewritten": 0, "converted": 0, "failed": 0}
    failures: list[tuple[str, str]] = []
    done = 0

    # ---- Prune: apagar já (a validação passou) ----
    pruned = 0
    if orphans:
        for p in orphans:
            try:
                p.unlink()
                pruned += 1
                print(f"[apagar] {p.relative_to(dst_root)}", flush=True)
            except OSError as e:
                failures.append((f"(apagar) {p.relative_to(dst_root)}", str(e)))
        remove_empty_dirs(dst_root)

    STATUS_PT = {"copied": "copiado", "converted": "convertido",
                 "rewritten": "reescrito", "skipped": "ignorado"}

    # Executor manual (não `with`): num Ctrl-C o `with` esperaria TODOS os jobs
    # pendentes terminarem, ou seja, a biblioteca inteira.
    ex = ThreadPoolExecutor(max_workers=args.jobs)
    interrupted = False
    try:
        futures = {ex.submit(run_job, j, args): j for j in jobs}
        for fut in as_completed(futures):
            job = futures[fut]
            try:
                status, err = fut.result()
            except Exception as e:  # noqa: BLE001
                status, err = "failed", str(e)
            stats[status] += 1
            done += 1
            if status == "failed":
                failures.append((job.rel, err))
                print(f"[{done}/{total}] ERRO: {job.rel}", file=sys.stderr, flush=True)
            elif status != "skipped":
                print(f"[{done}/{total}] {STATUS_PT.get(status, status)}: {job.rel}",
                      flush=True)
    except KeyboardInterrupt:
        interrupted = True
        print("\nInterrompido: a cancelar o que ainda não começou...",
              file=sys.stderr, flush=True)
        ex.shutdown(wait=True, cancel_futures=True)
    else:
        ex.shutdown(wait=True)

    if interrupted:
        print("Você pode rodar de novo para continuar de onde parou.",
              file=sys.stderr, flush=True)
        return 130

    print(f"\nConcluído: {stats['converted']} convertidos, {stats['copied']} copiados, "
          f"{stats['rewritten']} playlists reescritas, "
          f"{stats['skipped']} já atualizados, {stats['failed']} com falha"
          + (f", {pruned} apagados do destino." if args.prune else "."), flush=True)
    summary = size_summary(jobs)
    if summary:
        print(summary, flush=True)

    # Só grava o bitrate novo se nada falhou: um arquivo que falhou continua com o
    # bitrate antigo e, sem isto, seria considerado atualizado para sempre.
    if not (bitrate_changed and failures):
        save_state(dst_root, args.bitrate)
    elif failures:
        print("O bitrate novo NÃO foi gravado no estado porque houve falhas; "
              "a próxima execução refaz os convertidos de novo.",
              file=sys.stderr, flush=True)

    if failures:
        log = Path.cwd() / "mirroropus_errors.log"
        with log.open("w", encoding="utf-8") as f:
            for rel, err in failures:
                f.write(f"{rel}\n    {err}\n")
        print(f"Detalhes dos erros em: {log}", file=sys.stderr, flush=True)
        return 1
    return 1 if walk_errors else 0


if __name__ == "__main__":
    sys.exit(main())
