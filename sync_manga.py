#!/usr/bin/env python3
"""
sync_manga.py
=============
Synchronise automatiquement des manga/chapitres depuis l'API publique
MangaDex vers Firestore, au même format que ton site ManghWa/MangaPan.

Ce script est fait pour tourner via GitHub Actions (gratuit), sur un
cron régulier. Il ne coûte rien : API MangaDex gratuite, Firestore
Spark (gratuit) largement suffisant pour ce volume.

Modèle "agrégateur" : on ne stocke QUE les métadonnées + des liens
vers les pages hébergées par MangaDex (via leur API officielle
at-home). On n'héberge jamais nous-mêmes les scans -> pas de risque
de copyright direct, comme discuté pour MangaPan.
"""

import os
import sys
import time
import json
import base64
import requests
import firebase_admin
from firebase_admin import credentials, firestore

# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------
MANGADEX_API = "https://api.mangadex.org"
LANGUAGES = ["fr", "en"]          # langues de chapitres acceptées, dans l'ordre de préférence
CONTENT_RATINGS = ["safe", "suggestive"]  # pas de contenu explicite
MAX_MANGA_PER_RUN = 15            # combien de mangas on scanne à chaque run (rate-limit friendly)
MAX_NEW_CHAPTERS_PER_MANGA = 10   # pour ne pas exploser le quota Firestore d'un coup
REQUEST_DELAY = 0.3               # secondes entre 2 appels API (MangaDex tolère ~5 req/s)

# ----------------------------------------------------------------------
# INIT FIREBASE (clé de service via variable d'environnement)
# ----------------------------------------------------------------------
def init_firestore():
    # Option simple (recommandée) : coller directement le contenu du fichier .json
    # tel quel dans le secret GitHub FIREBASE_SERVICE_ACCOUNT_JSON.
    raw_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
    if raw_json:
        key_json = json.loads(raw_json)
    else:
        # Option alternative : version encodée en base64, si tu préfères ça.
        b64_key = os.environ.get("FIREBASE_SERVICE_ACCOUNT_B64")
        if not b64_key:
            print("ERREUR: aucune des deux variables d'environnement "
                  "(FIREBASE_SERVICE_ACCOUNT_JSON ou FIREBASE_SERVICE_ACCOUNT_B64) n'est définie.")
            sys.exit(1)
        key_json = json.loads(base64.b64decode(b64_key))

    cred = credentials.Certificate(key_json)
    firebase_admin.initialize_app(cred)
    return firestore.client()


# ----------------------------------------------------------------------
# HELPERS MANGADEX
# ----------------------------------------------------------------------
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "MangaPan-Sync/1.0 (personal project)"})


def api_get(path, params=None):
    resp = SESSION.get(f"{MANGADEX_API}{path}", params=params, timeout=20)
    time.sleep(REQUEST_DELAY)
    resp.raise_for_status()
    return resp.json()


def fetch_updated_manga(limit=MAX_MANGA_PER_RUN):
    """Récupère les mangas récemment mis à jour, avec cover + auteur inclus."""
    params = {
        "limit": limit,
        "order[latestUploadedChapter]": "desc",
        "contentRating[]": CONTENT_RATINGS,
        "includes[]": ["cover_art", "author"],
    }
    data = api_get("/manga", params=params)
    return data.get("data", [])


def extract_title(attrs):
    titles = attrs.get("title", {})
    return titles.get("en") or titles.get("fr") or next(iter(titles.values()), "Sans titre")


def extract_description(attrs):
    desc = attrs.get("description", {})
    return desc.get("en") or desc.get("fr") or next(iter(desc.values()), "")


def extract_cover_url(manga_id, relationships):
    for rel in relationships:
        if rel.get("type") == "cover_art" and "attributes" in rel:
            filename = rel["attributes"].get("fileName")
            if filename:
                return f"https://uploads.mangadex.org/covers/{manga_id}/{filename}.512.jpg"
    return "https://images.unsplash.com/photo-1507003211169-0a1dd7228f2d?w=400&q=80"


def extract_author(relationships):
    for rel in relationships:
        if rel.get("type") == "author" and "attributes" in rel:
            return rel["attributes"].get("name", "Inconnu")
    return "Inconnu"


def manga_to_doc(manga):
    """Convertit un objet MangaDex en document au format de ton site."""
    attrs = manga["attributes"]
    relationships = manga.get("relationships", [])
    status_map = {"ongoing": "ongoing", "completed": "completed", "hiatus": "hiatus", "cancelled": "hiatus"}
    return {
        "mangadexId": manga["id"],
        "title": extract_title(attrs),
        "author": extract_author(relationships),
        "type": "Manhwa" if attrs.get("originalLanguage") == "ko" else
                "Manhua" if attrs.get("originalLanguage") == "zh" else "Manga",
        "status": status_map.get(attrs.get("status"), "ongoing"),
        "genres": [t["attributes"]["name"].get("en", "") for t in attrs.get("tags", [])
                   if t["attributes"]["group"] == "genre"][:6],
        "cover": extract_cover_url(manga["id"], relationships),
        "description": extract_description(attrs)[:800],
    }


def fetch_new_chapters(mangadex_id, known_chapter_ids):
    """Récupère les chapitres pas encore connus, dans la langue dispo la plus prioritaire."""
    params = {
        "manga": mangadex_id,
        "limit": 100,
        "order[chapter]": "asc",
        "translatedLanguage[]": LANGUAGES,
        "contentRating[]": CONTENT_RATINGS,
    }
    data = api_get(f"/chapter", params=params)
    new_chapters = []
    for ch in data.get("data", []):
        if ch["id"] in known_chapter_ids:
            continue
        attrs = ch["attributes"]
        if not attrs.get("chapter"):
            continue
        new_chapters.append(ch)
        if len(new_chapters) >= MAX_NEW_CHAPTERS_PER_MANGA:
            break
    return new_chapters


def fetch_chapter_pages(chapter_id):
    """Utilise l'API officielle at-home pour récupérer les URLs de pages (hotlink autorisé)."""
    try:
        data = api_get(f"/at-home/server/{chapter_id}")
        base_url = data["baseUrl"]
        chapter_hash = data["chapter"]["hash"]
        filenames = data["chapter"]["data"]
        return [f"{base_url}/data/{chapter_hash}/{fn}" for fn in filenames]
    except Exception as e:
        print(f"  ! impossible de récupérer les pages du chapitre {chapter_id}: {e}")
        return []


def chapter_to_doc(chapter):
    attrs = chapter["attributes"]
    try:
        number = float(attrs["chapter"])
        if number.is_integer():
            number = int(number)
    except (TypeError, ValueError):
        number = 0
    pages = fetch_chapter_pages(chapter["id"])
    return {
        "mangadexChapterId": chapter["id"],
        "number": number,
        "title": attrs.get("title") or f"Chapter {number}",
        "date": (attrs.get("publishAt") or "")[:10],
        "pages": pages,
    }


# ----------------------------------------------------------------------
# SYNC LOGIC
# ----------------------------------------------------------------------
def sync():
    db = init_firestore()
    manga_ref = db.collection("manga")

    print("Récupération des mangas récemment mis à jour sur MangaDex...")
    updated_manga = fetch_updated_manga()
    print(f"{len(updated_manga)} mangas trouvés.")

    for manga in updated_manga:
        mangadex_id = manga["id"]
        title = extract_title(manga["attributes"])
        print(f"\n-> {title} ({mangadex_id})")

        # Cherche si ce manga existe déjà (par mangadexId)
        existing_query = manga_ref.where("mangadexId", "==", mangadex_id).limit(1).get()
        doc_data = manga_to_doc(manga)

        if existing_query:
            doc_ref = existing_query[0].reference
            existing = existing_query[0].to_dict()
            # On met à jour les métadonnées SANS écraser views/comments/chapters
            doc_ref.update({
                "title": doc_data["title"],
                "author": doc_data["author"],
                "status": doc_data["status"],
                "genres": doc_data["genres"],
                "cover": doc_data["cover"],
                "description": doc_data["description"],
            })
            known_chapter_ids = {c.get("mangadexChapterId") for c in existing.get("chapters", [])}
            print("   manga déjà en base, vérification des nouveaux chapitres...")
        else:
            doc_data.update({
                "chapters": [],
                "comments": [],
                "views": 0,
                "createdAt": firestore.SERVER_TIMESTAMP,
            })
            doc_ref = manga_ref.document()
            doc_ref.set(doc_data)
            known_chapter_ids = set()
            print("   nouveau manga ajouté.")

        # Chapitres
        new_chapters = fetch_new_chapters(mangadex_id, known_chapter_ids)
        if not new_chapters:
            print("   aucun nouveau chapitre.")
            continue

        for ch in new_chapters:
            ch_doc = chapter_to_doc(ch)
            if not ch_doc["pages"]:
                continue  # on ignore les chapitres sans pages récupérables
            doc_ref.update({"chapters": firestore.ArrayUnion([ch_doc])})
            print(f"   + chapitre {ch_doc['number']} ajouté ({len(ch_doc['pages'])} pages)")

    print("\nSynchronisation terminée.")


if __name__ == "__main__":
    sync()
