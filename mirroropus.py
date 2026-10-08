#!/usr/bin/env python3
"""
mirroropus.py

Espelha uma biblioteca de música (master) para uma cópia "leve" em Opus,
mantendo a estrutura de pastas.

  - Lossy (MP3, Opus, Ogg Vorbis, AAC, WMA lossy, Musepack, ...) -> copiado como está
  - Lossless (FLAC, WAV, AIFF, ALAC, APE, WV, TTA, TAK, SHN, WMA lossless,
    Ogg-FLAC, DSD (.dsf/.dff), CAF/AIFC lossless, ...)           -> convertido para .opus
  - Playlists (.cue, .m3u, .m3u8)                                -> copiadas e reescritas
    para apontar para os .opus (ex.: FILE "faixa.flac" passa a FILE "faixa.opus")
  - Tudo o resto                                                -> copiado como está:
    accurip (.accurip), .log, .nfo, .txt, .lrc, capas (.jpg/.png),
    scans e booklets (ex.: [album]/scans/, .7z/.zip/.rar/.pdf com scans),
    e qualquer outro arquivo ou subpasta. A estrutura é mantida igual à origem.

Encoder: opusenc (opus-tools). Copia tags e capas embutidas de FLAC.
Formatos que o opusenc não lê passam primeiro pelo ffmpeg, que decodifica
para FLAC em pipe (com tags e capa), e esse pipe é lido pelo opusenc.

Modo incremental (por padrão): só processa o que é novo ou mudou na
origem (mtime da origem mais recente que o do destino). Arquivos que
não mudaram são ignorados.
AVISO: mudar o bitrate (-b) não reconverte o que já existe; o modo
incremental ignora arquivos já atualizados mesmo que o bitrate seja diferente.
Para aplicar um novo bitrate a tudo, use --overwrite.

Sempre ignorados (lixo): dotfiles/pastas com ponto (.DS_Store, ._*, .Trash-1000,
.Trashes, .Spotlight-V100, ...), @eaDir, .AppleDouble, Thumbs.db, desktop.ini,
$RECYCLE.BIN, "System Volume Information", etc. Symlinks são sempre ignorados
(com aviso) e nunca seguidos.

--prune apaga do destino tudo o que já não tem correspondente na origem
(arquivos removidos/renomeados na master). Apaga na hora, mas:
  - sempre avisa quantos arquivos e álbuns serão apagados;
  - não roda sem -y/--yes se forem afetados mais álbuns do que
    --prune-limit (por padrão 3);
  - nunca roda se a origem não tiver nenhum arquivo de áudio
    (disco desmontado, caminho errado, ...).
Com --dry-run, só lista o que seria apagado.
Arquivos temporários *.part nunca contam para o prune e são limpos no início.

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
import os
import shutil
import subprocess
import sys
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
    "found.000", "$recycle.bin",
}
# Arquivos de lixo que nunca são espelhados.
IGNORE_FILES = {
    ".ds_store", "thumbs.db", "desktop.ini", ".apdisk",
}


def _is_junk_dir(name: str) -> bool:
    n = name.lower()
    return (
        name.startswith(".")  # .Trash-1000, .Trashes, .Spotlight-*, ...
        or n in IGNORE_DIRS
    )


def _is_junk_file(name: str) -> bool:
    n = name.lower()
    return (
        name.startswith("._")  # AppleDouble
        or name.startswith(".")  # dotfiles: a biblioteca não tem dotfiles
        or n in IGNORE_FILES
    )


@dataclass
class Job:
    src: Path
    dst: Path
    action: str  # "copy" | "opusenc" | "ffmpeg" | "rewrite"
    rel: str


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


def build_plan(src_root: Path, dst_root: Path, jobs_hint: int) -> tuple[list[Job], dict[str, int]]:
    raw: list[tuple[Path, str, str]] = []  # (src, rel, ext)
    skipped = {"junk": 0, "symlink": 0}
    to_probe: list[tuple[Path, str]] = []

    for dirpath, dirnames, filenames in os.walk(src_root, followlinks=False):
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
            if src.is_symlink() or _is_junk_file(name):
                skipped["symlink" if src.is_symlink() else "junk"] += 1
                continue
            ext = src.suffix.lower()
            rel = str(src.relative_to(src_root))
            raw.append((src, rel, ext))
            if ext in NEEDS_PROBE:
                to_probe.append((src, ext))

    # ffprobe só é obrigatório se houver arquivos ambíguos.
    codecs: dict[Path, str] = {}
    if to_probe:
        if shutil.which("ffprobe") is None:
            sys.exit("ffprobe é necessário para distinguir lossless de lossy "
                     "(.m4a/.wma/.ogg/.caf) mas não foi encontrado.")
        workers = max(1, min(jobs_hint or 4, len(to_probe), (os.cpu_count() or 4) * 2))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(probe_codec, p): (p, e) for p, e in to_probe}
            for fut in as_completed(futs):
                p, _e = futs[fut]
                try:
                    codecs[p] = fut.result()
                except Exception as e:  # noqa: BLE001
                    print(f"AVISO: erro ao detectar codec de {p}: {e}",
                          file=sys.stderr, flush=True)
                    codecs[p] = ""

    jobs: list[Job] = []
    for src, rel, ext in raw:
        action = "copy"
        if ext in OPUSENC_NATIVE:
            action = "opusenc"
        elif ext in VIA_FFMPEG:
            action = "ffmpeg"
        elif ext in NEEDS_PROBE:
            codec = codecs.get(src, "")
            if not codec:
                print(f"AVISO: sem codec detectado, a copiar como está: {rel}",
                      file=sys.stderr, flush=True)
            elif classify_lossless(ext, codec):
                action = "ffmpeg"

        dst = dst_root / rel
        if action in ("opusenc", "ffmpeg"):
            dst = dst.with_suffix(".opus")
        jobs.append(Job(src, dst, action, rel))

    # Playlists: se a pasta tiver conversões, passam a "rewrite" para atualizar
    # as referências (faixa.flac -> faixa.opus). Se não houver conversão na
    # pasta, ficam como "copy" normal.
    converted_basenames: dict[str, dict[str, str]] = {}  # dir_rel -> {src_name: dst_name}
    for j in jobs:
        if j.action in ("opusenc", "ffmpeg"):
            d = str(Path(j.rel).parent)
            converted_basenames.setdefault(d, {})[Path(j.src).name] = j.dst.name
    for j in jobs:
        if j.action == "copy" and j.src.suffix.lower() in PLAYLIST_EXTS:
            if str(Path(j.rel).parent) in converted_basenames:
                j.action = "rewrite"

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
    return unique, skipped


def rewrite_map_for(job: Job, jobs_by_dir: dict[str, dict[str, str]]) -> dict[str, str]:
    return jobs_by_dir.get(str(Path(job.rel).parent), {})


def rewrite_playlist_text(text: str, mapping: dict[str, str]) -> str:
    """Substitui referências a arquivos convertidos (nome exato)."""
    out = text
    for src_name, dst_name in mapping.items():
        if src_name in out:
            out = out.replace(src_name, dst_name)
    return out


def up_to_date(job: Job) -> bool:
    try:
        d = job.dst.stat()
        s = job.src.stat()
    except FileNotFoundError:
        return False
    return d.st_size > 0 and d.st_mtime >= s.st_mtime - TIMESTAMP_TOLERANCE


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
def run_job(job: Job, args: argparse.Namespace,
            jobs_by_dir: dict[str, dict[str, str]]) -> tuple[str, str]:
    if not args.overwrite and up_to_date(job):
        return "skipped", ""

    job.dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = job.dst.with_name(job.dst.name + ".part")
    try:
        if job.action == "copy":
            shutil.copy2(job.src, tmp)
        elif job.action == "rewrite":
            mapping = rewrite_map_for(job, jobs_by_dir)
            raw = None
            for enc in ("utf-8-sig", "utf-8", "latin-1"):
                try:
                    raw = job.src.read_text(encoding=enc)
                    break
                except (UnicodeDecodeError, OSError):
                    continue
            if raw is None:
                raise RuntimeError("não foi possível ler a playlist")
            out = rewrite_playlist_text(raw, mapping)
            tmp.write_text(out, encoding="utf-8")
            st = job.src.stat()
            os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        else:
            enc = encode_opusenc if job.action == "opusenc" else encode_ffmpeg
            ok, err = enc(job.src, tmp, args.bitrate)
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
        if job.action in ("copy", "rewrite"):
            return ("rewritten" if job.action == "rewrite" else "copied"), ""
        return "converted", ""
    except Exception as e:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        return "failed", str(e)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Espelha uma biblioteca de música: lossless -> Opus, resto copiado.",
        epilog="Nota: o modo incremental ignora arquivos já atualizados mesmo que "
               "--bitrate tenha mudado. Para aplicar um novo bitrate a tudo, "
               "use --overwrite.",
    )
    ap.add_argument("source", type=Path, help="pasta da biblioteca master")
    ap.add_argument("dest", type=Path, help="pasta de destino")
    ap.add_argument("-b", "--bitrate", type=int, default=128,
                    help="bitrate Opus em kbps (por padrão: 128)")
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4,
                    help="conversões em paralelo (por padrão: nº de CPUs)")
    ap.add_argument("--overwrite", action="store_true",
                    help="refazer tudo, mesmo o que já existe no destino "
                         "(obrigatório ao mudar de bitrate)")
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

    src_root = args.source.resolve()
    dst_root = args.dest.resolve()

    if not src_root.is_dir():
        sys.exit(f"Origem não é uma pasta: {src_root}")
    if dst_root == src_root or src_root in dst_root.parents:
        sys.exit("O destino não pode estar dentro da origem.")
    if src_root in dst_root.parents:
        sys.exit("A origem não pode estar dentro do destino.")
    if shutil.which("opusenc") is None:
        sys.exit("opusenc não encontrado (instala o pacote opus-tools).")

    jobs, skipped = build_plan(src_root, dst_root, args.jobs)

    if any(j.action in ("opusenc", "ffmpeg") for j in jobs) \
            and shutil.which("opusenc") is None:
        sys.exit("opusenc não encontrado (instala o pacote opus-tools).")
    if any(j.action == "ffmpeg" for j in jobs) and shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg é necessário para ALAC/APE/WV/TTA/TAK/SHN/DSD/WMA-lossless "
                 "mas não foi encontrado.")

    jobs_by_dir: dict[str, dict[str, str]] = {}
    for j in jobs:
        if j.action in ("opusenc", "ffmpeg"):
            jobs_by_dir.setdefault(str(Path(j.rel).parent), {})[j.src.name] = j.dst.name

    total = len(jobs)
    n_convert = sum(1 for j in jobs if j.action in ("opusenc", "ffmpeg"))
    print(f"{total} arquivos na origem ({n_convert} a converter). "
          f"Bitrate: {args.bitrate} kbps. "
          f"Modo: {'overwrite' if args.overwrite else 'incremental'}.", flush=True)
    if skipped["junk"] or skipped["symlink"]:
        print(f"Ignorados: {skipped['junk']} de lixo, "
              f"{skipped['symlink']} links simbólicos.", flush=True)
    if not args.overwrite:
        print("AVISO: modo incremental ignora arquivos já atualizados mesmo que "
              "--bitrate tenha mudado; use --overwrite para reconverter tudo "
              "com um novo bitrate.", file=sys.stderr, flush=True)

    # ---- Prune: planear e validar ANTES de fazer qualquer coisa ----
    orphans: list[Path] = []
    if args.prune:
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
            if not args.overwrite and up_to_date(j):
                counts["skipped"] += 1
                continue
            if j.action in ("opusenc", "ffmpeg"):
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
        return 0

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
    try:
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
            futures = {ex.submit(run_job, j, args, jobs_by_dir): j for j in jobs}
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
                    print(f"[{done}/{total}] {STATUS_PT.get(status, status)}: {job.rel}", flush=True)
    except KeyboardInterrupt:
        print("\nInterrompido. Você pode rodar de novo para continuar de onde parou.",
              file=sys.stderr, flush=True)
        return 130

    print(f"\nConcluído: {stats['converted']} convertidos, {stats['copied']} copiados, "
          f"{stats['rewritten']} playlists reescritas, "
          f"{stats['skipped']} já atualizados, {stats['failed']} com falha"
          + (f", {pruned} apagados do destino." if args.prune else "."), flush=True)

    if failures:
        log = Path.cwd() / "mirroropus_errors.log"
        with log.open("w", encoding="utf-8") as f:
            for rel, err in failures:
                f.write(f"{rel}\n    {err}\n")
        print(f"Detalhes dos erros em: {log}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
