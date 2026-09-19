#!/usr/bin/env python3
"""
build_library.py

Scanne un dossier music/Artiste/Album/*.mp3, complète les métadonnées
et les pochettes via l'API iTunes Search (gratuite, sans clé), écrit
un album.json dans chaque dossier d'album, puis un library.json global
à la racine du repo.

Structure attendue :
  music/
    Nom Artiste/
      Nom Album (Année)/
        01 - Titre.mp3
        02 - Titre.mp3

Usage :
  pip install mutagen requests
  python build_library.py [--music-dir music] [--out data/library.json] [--no-fetch]

--no-fetch : ne fait aucun appel réseau, utilise uniquement les tags
             présents dans les fichiers mp3 (utile hors-ligne / en CI
             sans accès réseau).
"""

import argparse
import json
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from mutagen import File as MutagenFile
    from mutagen.easyid3 import EasyID3
    from mutagen.id3 import ID3, APIC
    from mutagen.mp3 import MP3
except ImportError:
    print("Il manque 'mutagen'. Installe-le avec : pip install mutagen", file=sys.stderr)
    sys.exit(1)

AUDIO_EXTENSIONS = {".mp3"}
ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
REQUEST_DELAY_SECONDS = 0.35  # reste sous la limite de l'API iTunes (~20 req / min conseillé)


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "untitled"


def humanize(text: str) -> str:
    """Convertit les underscores en espaces et nettoie les doubles espaces.
    'Doja_Cat' -> 'Doja Cat', 'Amala_(Deluxe)' -> 'Amala (Deluxe)'."""
    if not text:
        return text
    text = text.replace("__", " ").replace("_", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def fuzzy_match(a: str, b: str) -> bool:
    """Comparaison souple, insensible à la casse et aux espaces, pour
    rapprocher des variantes comme 'Amala' et 'Amala (Deluxe)'."""
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    if not a or not b:
        return False
    return a == b or a in b or b in a


def read_local_tags(mp3_path: Path):
    """Lit les tags ID3 déjà présents dans le fichier (fallback / base)."""
    data = {"title": None, "artist": None, "album": None, "track": None,
            "year": None, "genre": None, "duration": None}
    try:
        audio = MP3(mp3_path, ID3=EasyID3)
        data["title"] = (audio.get("title") or [None])[0]
        data["artist"] = (audio.get("artist") or [None])[0]
        data["album"] = (audio.get("album") or [None])[0]
        track = (audio.get("tracknumber") or [None])[0]
        if track:
            data["track"] = int(str(track).split("/")[0] or 0)
        year = (audio.get("date") or [None])[0]
        if year:
            data["year"] = int(str(year)[:4])
        data["genre"] = (audio.get("genre") or [None])[0]
        data["duration"] = int(audio.info.length)
    except Exception as e:
        print(f"  ! Impossible de lire les tags de {mp3_path.name}: {e}", file=sys.stderr)
    return data


def fetch_itunes_metadata(artist: str, track_title: str, album: str = None):
    """Cherche le morceau sur iTunes Search API. Retourne un dict ou None."""
    term = f"{artist} {track_title}"
    params = {"term": term, "media": "music", "entity": "song", "limit": 5}
    url = f"{ITUNES_SEARCH_URL}?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "library-builder/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  ! Requête iTunes échouée pour '{term}': {e}", file=sys.stderr)
        return None

    results = payload.get("results", [])
    if not results:
        return None

    # Si on a un nom d'album, on privilégie une correspondance sur l'album
    best = results[0]
    if album:
        # Cherche d'abord une correspondance exacte, puis une correspondance
        # souple (ex: dossier 'Amala (Deluxe)' vs 'Amala' sur iTunes, ou
        # l'inverse) pour éviter d'attraper la pochette d'un single quand
        # le morceau est en fait rangé dans le dossier d'un album/deluxe.
        exact = [r for r in results if r.get("collectionName", "").strip().lower() == album.strip().lower()]
        fuzzy = [r for r in results if fuzzy_match(r.get("collectionName", ""), album)]
        if exact:
            best = exact[0]
        elif fuzzy:
            best = fuzzy[0]

    cover = best.get("artworkUrl100", "")
    cover_hq = cover.replace("100x100bb", "1000x1000bb") if cover else None

    return {
        "title": best.get("trackName"),
        "artist": best.get("artistName"),
        "album": best.get("collectionName"),
        "genre": best.get("primaryGenreName"),
        "year": int(best["releaseDate"][:4]) if best.get("releaseDate") else None,
        "track": best.get("trackNumber"),
        "cover_url": cover_hq,
        "duration_ms": best.get("trackTimeMillis"),
    }


def download_cover(url: str, dest: Path):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "library-builder/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            dest.write_bytes(resp.read())
        return True
    except Exception as e:
        print(f"  ! Téléchargement pochette échoué ({url}): {e}", file=sys.stderr)
        return False


def parse_album_folder_name(name: str):
    """'Nom_Album_(2023)' -> ('Nom Album', 2023) ; 'Amala_(Deluxe)' -> ('Amala (Deluxe)', None)."""
    name = humanize(name)
    m = re.match(r"^(.*?)\s*\((\d{4})\)\s*$", name)
    if m:
        return m.group(1).strip(), int(m.group(2))
    return name.strip(), None


def build_album(artist_name: str, album_dir: Path, fetch: bool, force_cover: bool):
    album_title, year_from_folder = parse_album_folder_name(album_dir.name)
    mp3_files = sorted(
        [p for p in album_dir.iterdir() if p.suffix.lower() in AUDIO_EXTENSIONS],
        key=lambda p: p.name,
    )
    if not mp3_files:
        return None

    tracks = []
    album_genre, album_year = None, year_from_folder
    cover_saved = False
    cover_path = album_dir / "cover.jpg"

    for i, mp3_path in enumerate(mp3_files, start=1):
        local = read_local_tags(mp3_path)
        title = local["title"] or humanize(mp3_path.stem)
        track_num = local["track"] or i
        duration = local["duration"] or 0
        genre = local["genre"]

        if fetch:
            remote = fetch_itunes_metadata(artist_name, title, album_title)
            time.sleep(REQUEST_DELAY_SECONDS)
            if remote:
                title = remote["title"] or title
                genre = remote["genre"] or genre
                album_year = album_year or remote["year"]
                album_genre = album_genre or remote["genre"]
                if remote["duration_ms"]:
                    duration = round(remote["duration_ms"] / 1000)
                if remote["cover_url"] and (force_cover or not cover_path.exists()):
                    if download_cover(remote["cover_url"], cover_path):
                        cover_saved = True

        tracks.append({
            "title": title,
            "file": mp3_path.name,
            "track": track_num,
            "duration": duration,
        })

    tracks.sort(key=lambda t: t["track"])

    album_data = {
        "artist": artist_name,
        "album": album_title,
        "year": album_year,
        "genre": album_genre,
        "cover": "cover.jpg" if (cover_path.exists() or cover_saved) else None,
        "tracks": tracks,
    }

    (album_dir / "album.json").write_text(
        json.dumps(album_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return album_data


def build_library(music_dir: Path, fetch: bool, force_cover: bool):
    library = {"artists": [], "albums": []}

    if not music_dir.exists():
        print(f"Dossier introuvable : {music_dir}", file=sys.stderr)
        sys.exit(1)

    for artist_dir in sorted(p for p in music_dir.iterdir() if p.is_dir()):
        artist_name = humanize(artist_dir.name)
        artist_has_album = False

        for album_dir in sorted(p for p in artist_dir.iterdir() if p.is_dir()):
            print(f"→ {artist_name} / {album_dir.name}")
            album_data = build_album(artist_name, album_dir, fetch, force_cover)
            if album_data is None:
                continue
            artist_has_album = True
            library["albums"].append({
                "id": f"{slugify(artist_name)}__{slugify(album_data['album'])}",
                "path": f"music/{artist_dir.name}/{album_dir.name}",
                **album_data,
            })

        if artist_has_album:
            library["artists"].append(artist_name)

    return library


def main():
    parser = argparse.ArgumentParser(description="Génère library.json à partir du dossier music/")
    parser.add_argument("--music-dir", default="music", help="Dossier racine contenant les artistes")
    parser.add_argument("--out", default="data/library.json", help="Chemin de sortie de library.json")
    parser.add_argument("--no-fetch", action="store_true", help="N'appelle pas l'API iTunes (tags locaux uniquement)")
    parser.add_argument("--force-cover", action="store_true", help="Retélécharge la pochette même si cover.jpg existe déjà")
    args = parser.parse_args()

    music_dir = Path(args.music_dir)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    library = build_library(music_dir, fetch=not args.no_fetch, force_cover=args.force_cover)
    out_path.write_text(json.dumps(library, ensure_ascii=False, indent=2), encoding="utf-8")

    n_albums = len(library["albums"])
    n_artists = len(library["artists"])
    print(f"\n✓ {n_artists} artiste(s), {n_albums} album(s) écrits dans {out_path}")


if __name__ == "__main__":
    main()
