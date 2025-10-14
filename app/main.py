import os, time, re, io, json
from typing import Dict, Any
from fastapi import FastAPI, Request, HTTPException, Query, Response
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
from yt_dlp import YoutubeDL
from collections import defaultdict

APP_DOMAIN = os.getenv("APP_DOMAIN", "https://d.end.yt")
TURNSTILE_SECRET = os.getenv("TURNSTILE_SECRET", "")  # defina no Coolify
RATE_WINDOW_SEC = 1800  # 30min
RATE_MAX_ANALYZE = 10
RATE_MAX_DL = 5

app = FastAPI(title="canal.yt downloader")

# CORS estrito: só o seu domínio
app.add_middleware(
    CORSMiddleware,
    allow_origins=[APP_DOMAIN],
    allow_credentials=True,
    allow_methods=["POST", "GET"],
    allow_headers=["Content-Type", "X-CSRF-Token"],
)

YTDLP_OPTS_PROBE = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "noplaylist": True,
    "extract_flat": False,
}

rate_hits_analyze: Dict[str, list] = defaultdict(list)
rate_hits_download: Dict[str, list] = defaultdict(list)
meta_cache: Dict[str, Dict[str, Any]] = {}
meta_cache_ttl: Dict[str, float] = {}

VIDEO_URL_ALLOWED = re.compile(
    r"^(https?://)?(www\.)?(youtube\.com|youtu\.be|music\.youtube\.com)/"
)

def client_ip(req: Request) -> str:
    xff = req.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return xff or req.client.host

def rate_ok(bucket: Dict[str, list], ip: str, limit: int) -> bool:
    now = time.time()
    bucket[ip] = [t for t in bucket[ip] if now - t < RATE_WINDOW_SEC]
    if len(bucket[ip]) >= limit:
        return False
    bucket[ip].append(now)
    return True

async def verify_turnstile(token: str, ip: str) -> bool:
    if not TURNSTILE_SECRET:
        return True  # fallback em dev
    url = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, data={"secret": TURNSTILE_SECRET, "response": token, "remoteip": ip})
    data = r.json()
    return bool(data.get("success"))

def validate_url(url: str):
    if not VIDEO_URL_ALLOWED.search(url or ""):
        raise HTTPException(status_code=400, detail="URL inválida ou domínio não permitido.")

def extract_meta(url: str) -> Dict[str, Any]:
    with YoutubeDL(YTDLP_OPTS_PROBE) as ydl:
        info = ydl.extract_info(url, download=False)
    formats = []
    for f in (info.get("formats") or []):
        if not f.get("url"):
            continue
        formats.append({
            "format_id": f.get("format_id"),
            "ext": f.get("ext"),
            "vcodec": f.get("vcodec"),
            "acodec": f.get("acodec"),
            "height": f.get("height"),
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "fps": f.get("fps"),
            "tbr": f.get("tbr"),
            "format_note": f.get("format_note"),
        })
    return {
        "id": info.get("id"),
        "title": info.get("title"),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader"),
        "formats": formats
    }

@app.post("/analyze")
async def analyze(req: Request):
    body = await req.json()
    url = (body.get("url") or "").strip()
    captcha = body.get("captchaToken")
    ip = client_ip(req)
    ua = req.headers.get("user-agent", "")

    if not url or not captcha:
        raise HTTPException(status_code=400, detail="Parâmetros ausentes.")

    if "curl" in ua.lower():
        raise HTTPException(status_code=403, detail="Agente não autorizado.")

    if not rate_ok(rate_hits_analyze, ip, RATE_MAX_ANALYZE):
        raise HTTPException(status_code=429, detail="Muitas análises. Tente mais tarde.")

    if not await verify_turnstile(captcha, ip):
        raise HTTPException(status_code=403, detail="Falha na verificação humana.")

    validate_url(url)

    cache_key = url
    if cache_key in meta_cache and time.time() < meta_cache_ttl.get(cache_key, 0):
        return JSONResponse(meta_cache[cache_key])

    try:
        data = extract_meta(url)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Falha ao extrair metadados: {e}")

    meta_cache[cache_key] = data
    meta_cache_ttl[cache_key] = time.time() + 600  # 10 min

    return JSONResponse(data)

@app.get("/download")
async def download(req: Request,
                   url: str = Query(...),
                   format_id: str = Query(...),
                   csrf: str = Query(None)):
    ip = client_ip(req)
    referer = req.headers.get("referer", "")
    if not referer.startswith(APP_DOMAIN):
        raise HTTPException(status_code=403, detail="Referer inválido.")

    if not rate_ok(rate_hits_download, ip, RATE_MAX_DL):
        raise HTTPException(status_code=429, detail="Muitos downloads. Tente mais tarde.")

    validate_url(url)
    if not format_id:
        raise HTTPException(status_code=400, detail="Formato ausente.")

    try:
        with YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            info = ydl.extract_info(url, download=False)
            fmts = {f["format_id"]: f for f in info.get("formats", []) if f.get("url")}
            chosen = fmts.get(format_id)
            if not chosen:
                raise HTTPException(status_code=404, detail="Formato não encontrado.")
            src_url = chosen["url"]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Falha ao preparar download: {e}")

    async def iter_stream():
        chunk = 64 * 1024
        async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
            async with client.stream("GET", src_url, headers={"User-Agent": "Mozilla/5.0"}) as r:
                r.raise_for_status()
                async for b in r.aiter_bytes(chunk_size=chunk):
                    yield b

    filename = f"canal-yt-{info.get('id','video')}-{format_id}.{chosen.get('ext','mp4')}"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(iter_stream(), headers=headers, media_type="application/octet-stream")
