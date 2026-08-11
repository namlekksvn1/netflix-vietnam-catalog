import os
import sys
import json
import time
import base64
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

NETFLIX_COUNTRIES_URL = "https://www.netflix.com/tudum/top10/data/all-weeks-countries.tsv"
TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE = "https://image.tmdb.org/t/p/w500"

OWNER = os.getenv("GITHUB_OWNER", "namlekksvn1")
REPO = os.getenv("GITHUB_REPO", "netflix-vietnam-catalog")
BRANCH = os.getenv("GITHUB_BRANCH", "main")
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "NetflixVietnamCatalog/1.0"})

def die(msg):
    print(f"\nERROR: {msg}")
    sys.exit(1)

def normalize(s):
    import re
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())

def fetch_netflix():
    print("[1/4] Downloading Netflix official country dataset...")
    r = SESSION.get(NETFLIX_COUNTRIES_URL, timeout=90)
    r.raise_for_status()

    from io import StringIO
    df = pd.read_csv(StringIO(r.text), sep="\t", encoding="utf-8-sig")
    df.columns = [str(c).strip() for c in df.columns]

    # Netflix's current country dataset uses country_iso2.
    # Keep a fallback for older/alternate column names.
    country_col = None
    for candidate in ("country_iso2", "country_iso", "country_code"):
        if candidate in df.columns:
            country_col = candidate
            break

    if country_col:
        df = df[df[country_col].astype(str).str.upper().eq("VN")].copy()

    if df.empty and "country_name" in df.columns:
        df = df[df["country_name"].astype(str).str.strip().str.lower().eq("vietnam")].copy()

    if df.empty:
        available = ", ".join(df.columns.astype(str))
        die(f"Could not find Vietnam in Netflix country dataset. Available columns: {available}")

    df["week"] = pd.to_datetime(df["week"], errors="coerce")
    latest = df["week"].max()
    df = df[df["week"].eq(latest)].copy()

    if latest is pd.NaT:
        die("Netflix dataset contains no valid week dates.")

    print(f"      Latest Netflix week: {latest.date()}")
    return df.sort_values("weekly_rank")

def split_titles(df):
    # Netflix publishes Films and TV categories, including English/non-English.
    if "category" not in df.columns:
        die("Netflix dataset has no 'category' column.")

    cat = df["category"].astype(str).str.strip().str.lower()
    movies = df[cat.str.startswith("films")].copy()
    series = df[cat.str.startswith("tv")].copy()

    movies = movies.sort_values("weekly_rank").drop_duplicates("show_title")
    series = series.sort_values("weekly_rank").drop_duplicates("show_title")
    return movies, series

def tmdb_search(title, media_type):
    endpoint = f"{TMDB_BASE}/search/{'movie' if media_type == 'movie' else 'tv'}"
    params = {
        "api_key": TMDB_API_KEY,
        "query": title,
        "language": "en-US",
        "include_adult": "false",
        "page": 1,
    }
    r = SESSION.get(endpoint, params=params, timeout=30)
    if r.status_code == 401:
        die("TMDB API key is invalid or missing.")
    r.raise_for_status()
    return r.json().get("results", [])

def choose_result(results, title):
    if not results:
        return None

    target = normalize(title)
    exact = [x for x in results if normalize(x.get("title") or x.get("name")) == target]
    if exact:
        return exact[0]

    # Prefer close lexical containment before popularity.
    scored = []
    for x in results:
        candidate = normalize(x.get("title") or x.get("name"))
        score = 0
        if target in candidate or candidate in target:
            score += 100
        score += min(float(x.get("popularity") or 0), 50) / 10
        scored.append((score, x))
    scored.sort(key=lambda z: z[0], reverse=True)
    return scored[0][1]

def resolve_tmdb(title, media_type):
    results = tmdb_search(title, media_type)
    item = choose_result(results, title)
    if not item:
        return None
    tmdb_id = item.get("id")
    poster_path = item.get("poster_path")
    if not tmdb_id:
        return None
    return {
        "id": f"tmdb:{tmdb_id}",
        "type": media_type,
        "name": item.get("title") or item.get("name") or title,
        "poster": f"{TMDB_IMAGE}{poster_path}" if poster_path else None,
        "description": item.get("overview") or "",
        "rank": None,
    }

def build_catalog(df, media_type, limit=10):
    metas = []
    unresolved = []
    for _, row in df.head(limit).iterrows():
        title = str(row["show_title"]).strip()
        rank = int(row["weekly_rank"])
        print(f"      #{rank:02d} {title}")
        try:
            meta = resolve_tmdb(title, media_type)
        except requests.RequestException as e:
            print(f"         TMDB error: {e}")
            meta = None

        if meta:
            meta["rank"] = rank
            # Keep Netflix ordering via a non-standard field; Stremio ignores unknown fields.
            meta["behaviorHints"] = {"defaultVideoId": f"netflix-rank-{rank}"}
            metas.append(meta)
        else:
            unresolved.append(title)
        time.sleep(0.25)

    # Remove rank helper from output after sorting.
    metas.sort(key=lambda x: x.get("rank", 999))
    for m in metas:
        m.pop("rank", None)
        m.pop("behaviorHints", None)
        if not m.get("poster"):
            m.pop("poster", None)

    return {"metas": metas}, unresolved

def github_put(path, content, message):
    if not GITHUB_TOKEN:
        die("GITHUB_TOKEN is missing. Create a fine-grained token with Contents: Read and write, then put it in .env.")

    url = f"https://api.github.com/repos/{OWNER}/{REPO}/contents/{path}"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # Get existing SHA when file already exists.
    r = SESSION.get(url, headers=headers, params={"ref": BRANCH}, timeout=30)
    sha = r.json().get("sha") if r.status_code == 200 else None

    payload = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": BRANCH,
    }
    if sha:
        payload["sha"] = sha

    r = SESSION.put(url, headers=headers, json=payload, timeout=30)
    if r.status_code not in (200, 201):
        die(f"GitHub update failed for {path}: {r.status_code} {r.text[:500]}")

def save_local(path, data):
    p = ROOT / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def main():
    if not TMDB_API_KEY:
        die("TMDB_API_KEY is missing. Put your TMDB API key in .env.")
    if not GITHUB_TOKEN:
        die("GITHUB_TOKEN is missing. Put your GitHub token in .env.")

    df = fetch_netflix()
    movies, series = split_titles(df)

    print(f"      Movies available: {len(movies)}")
    print(f"      Series available: {len(series)}")

    print("[2/4] Resolving movie titles with TMDB...")
    movie_catalog, movie_unresolved = build_catalog(movies, "movie", 10)

    print("[3/4] Resolving series titles with TMDB...")
    series_catalog, series_unresolved = build_catalog(series, "series", 10)

    # Local copies for debugging.
    save_local("output/netflix-vietnam-movies.json", movie_catalog)
    save_local("output/netflix-vietnam-series.json", series_catalog)
    save_local("output/unresolved.json", {
        "movies": movie_unresolved,
        "series": series_unresolved
    })

    print("[4/4] Uploading catalog JSON to GitHub Pages...")
    github_put(
        "catalog/movie/netflix-vietnam-movies.json",
        json.dumps(movie_catalog, ensure_ascii=False, indent=2),
        "Update Netflix Vietnam movie catalog"
    )
    github_put(
        "catalog/series/netflix-vietnam-series.json",
        json.dumps(series_catalog, ensure_ascii=False, indent=2),
        "Update Netflix Vietnam series catalog"
    )

    print("\nSUCCESS")
    print(f"  Movies: {len(movie_catalog['metas'])}")
    print(f"  Series: {len(series_catalog['metas'])}")
    print("  GitHub Pages files updated.")
    if movie_unresolved or series_unresolved:
        print("\nUnresolved titles were saved to output/unresolved.json")

if __name__ == "__main__":
    main()
