import os, sys, json, time, base64, re
from io import StringIO
from pathlib import Path
import requests, pandas as pd
from dotenv import load_dotenv

ROOT=Path(__file__).resolve().parent
load_dotenv(ROOT/".env")
TMDB_BASE="https://api.themoviedb.org/3"
TMDB_IMAGE="https://image.tmdb.org/t/p/w500"
NETFLIX_URL="https://www.netflix.com/tudum/top10/data/all-weeks-countries.tsv"
OWNER=os.getenv("GITHUB_OWNER","namlekksvn1"); REPO=os.getenv("GITHUB_REPO","netflix-vietnam-catalog")
BRANCH=os.getenv("GITHUB_BRANCH","main"); TMDB_API_KEY=os.getenv("TMDB_API_KEY","").strip()
GITHUB_TOKEN=os.getenv("GITHUB_TOKEN","").strip()
S=requests.Session(); S.headers["User-Agent"]="NetflixVietnamCatalog/2.0"

def die(x): print("\nERROR:",x); sys.exit(1)
def norm(x): return " ".join(re.sub(r"[^a-z0-9]+"," ",str(x or "").lower()).split())

def fetch_netflix():
    print("[1/4] Downloading Netflix official country dataset...")
    r=S.get(NETFLIX_URL,timeout=90); r.raise_for_status()
    df=pd.read_csv(StringIO(r.text),sep="\t",encoding="utf-8-sig")
    df.columns=[str(c).strip() for c in df.columns]
    cc=next((c for c in ("country_iso2","country_iso","country_code") if c in df.columns),None)
    if cc: df=df[df[cc].astype(str).str.upper().eq("VN")].copy()
    if df.empty and "country_name" in df.columns:
        df=df[df.country_name.astype(str).str.strip().str.lower().eq("vietnam")].copy()
    if df.empty: die("Vietnam (VN) not found in Netflix dataset.")
    df["week"]=pd.to_datetime(df["week"],errors="coerce"); latest=df["week"].max()
    df=df[df.week.eq(latest)].copy()
    print("      Latest Netflix week:",latest.date())
    return df.sort_values("weekly_rank")

def split(df):
    c=df["category"].astype(str).str.lower()
    return (df[c.str.startswith("films")].drop_duplicates("show_title"),
            df[c.str.startswith("tv")].drop_duplicates("show_title"))

def year_of(row):
    for c in ("release_year","release_years","year"):
        if c in row.index:
            m=re.search(r"(19|20)\d{2}",str(row[c]))
            if m:return int(m.group())
    return None

def search(title,typ,year=None):
    p={"api_key":TMDB_API_KEY,"query":title,"language":"en-US","include_adult":"false","page":1}
    if year:p["year" if typ=="movie" else "first_air_date_year"]=year
    u=f"{TMDB_BASE}/search/{'movie' if typ=='movie' else 'tv'}"
    r=S.get(u,params=p,timeout=30)
    if r.status_code==401: die("Invalid/missing TMDB API key.")
    r.raise_for_status(); return r.json().get("results",[])

def choose(results,title,year,typ):
    target=norm(title); best=None
    for x in results:
        cand=norm(x.get("title") or x.get("name")); score=0
        if cand==target: score+=1000
        elif target in cand or cand in target: score+=250
        a,b=set(target.split()),set(cand.split())
        if a and b: score+=100*len(a&b)/len(a|b)
        date=x.get("release_date") if typ=="movie" else x.get("first_air_date")
        ty=int(str(date)[:4]) if date and str(date)[:4].isdigit() else None
        if year and ty: score += 300 if ty==year else (-80 if abs(ty-year)>1 else 50)
        if x.get("poster_path"): score+=10
        score+=min(float(x.get("popularity") or 0),100)/10
        if best is None or score>best[0]: best=(score,x)
    return best[1] if best else None

def resolve(title,typ,year,rank):
    results=search(title,typ,year)
    if not results and year: results=search(title,typ)
    x=choose(results,title,year,typ)
    if not x:return None
    u=f"{TMDB_BASE}/{'movie' if typ=='movie' else 'tv'}/{x['id']}"
    r=S.get(u,params={"api_key":TMDB_API_KEY,"language":"en-US"},timeout=30)
    r.raise_for_status(); d={**x,**r.json()}
    date=d.get("release_date") if typ=="movie" else d.get("first_air_date")
    ry=int(str(date)[:4]) if date and str(date)[:4].isdigit() else year
    m={"id":f"tmdb:{x['id']}","type":typ,"name":title,
       "poster":f"{TMDB_IMAGE}{d['poster_path']}" if d.get("poster_path") else None,
       "background":f"https://image.tmdb.org/t/p/w1280{d['backdrop_path']}" if d.get("backdrop_path") else None,
       "description":d.get("overview") or "","releaseInfo":str(ry) if ry else "",
       "genres":[g["name"] for g in d.get("genres",[]) if g.get("name")],"_rank":rank}
    return m

def build(df,typ):
    out=[]; bad=[]
    for _,row in df.head(10).iterrows():
        title=str(row["show_title"]).strip(); rank=int(row["weekly_rank"]); yr=year_of(row)
        print(f"      #{rank:02d} {title}"+(f" ({yr})" if yr else ""))
        try:m=resolve(title,typ,yr,rank)
        except requests.RequestException as e: print("         TMDB error:",e); m=None
        if m:out.append(m)
        else:bad.append({"rank":rank,"title":title,"year":yr,"type":typ})
        time.sleep(.25)
    out.sort(key=lambda x:x["_rank"])
    for m in out:
        m.pop("_rank",None)
        for k in ("poster","background","description","releaseInfo","genres"):
            if not m.get(k):m.pop(k,None)
    return {"metas":out},bad

def gh_put(path,data,msg):
    u=f"https://api.github.com/repos/{OWNER}/{REPO}/contents/{path}"
    h={"Accept":"application/vnd.github+json","Authorization":f"Bearer {GITHUB_TOKEN}","X-GitHub-Api-Version":"2022-11-28"}
    r=S.get(u,headers=h,params={"ref":BRANCH},timeout=30); sha=r.json().get("sha") if r.status_code==200 else None
    p={"message":msg,"content":base64.b64encode(data.encode()).decode(),"branch":BRANCH}
    if sha:p["sha"]=sha
    r=S.put(u,headers=h,json=p,timeout=30)
    if r.status_code not in (200,201):die(f"GitHub update failed: {r.status_code} {r.text[:400]}")

def main():
    if not TMDB_API_KEY:die("TMDB_API_KEY is missing.")
    if not GITHUB_TOKEN:die("GITHUB_TOKEN is missing.")
    df=fetch_netflix(); movies,series=split(df)
    print(f"      Movies available: {len(movies)}"); print(f"      Series available: {len(series)}")
    print("[2/4] Resolving movie titles with TMDB..."); mc,mb=build(movies,"movie")
    print("[3/4] Resolving series titles with TMDB..."); sc,sb=build(series,"series")
    (ROOT/"output").mkdir(exist_ok=True)
    (ROOT/"output/unresolved.json").write_text(json.dumps({"movies":mb,"series":sb},ensure_ascii=False,indent=2),encoding="utf-8")
    print("[4/4] Uploading catalog JSON to GitHub Pages...")
    gh_put("catalog/movie/netflix-vietnam-movies.json",json.dumps(mc,ensure_ascii=False,indent=2),"Update Netflix Vietnam movie catalog")
    gh_put("catalog/series/netflix-vietnam-series.json",json.dumps(sc,ensure_ascii=False,indent=2),"Update Netflix Vietnam series catalog")
    print("\nSUCCESS\n  Movies:",len(mc["metas"]),"\n  Series:",len(sc["metas"]),"\n  GitHub Pages files updated.")
if __name__=="__main__":main()
