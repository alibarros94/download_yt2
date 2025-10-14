import os, time, re, io, json
from typing import Dict, Any
from fastapi import FastAPI, Request, HTTPException, Query, Response
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
from yt_dlp import YoutubeDL
from collections import defaultdict

APP_DOMAIN = os.getenv("APP_DOMAIN", "https://d.end.yt")
TURNSTILE_SECRET = os.getenv("TURNSTILE_SECRET", "")  # defina no Coolify
RATE_WINDOW_SEC = 1800  # 30min
RATE_MAX_ANALYZE = 10
RATE_MAX_DL = 5
COOKIE_PATH = os.path.join(os.path.dirname(__file__), "..", "cookies.txt")

app = FastAPI(title="d.end.yt downloader")

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
    "cookiefile": COOKIE_PATH,
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
        with YoutubeDL({"quiet": True, "no_warnings": True, "cookiefile": COOKIE_PATH}) as ydl:
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


@app.get("/", response_class=HTMLResponse)
async def root():
    return """
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1.0" />
        <title>d.end.yt Downloader</title>
        <script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
        <style>
            body {
                font-family: Arial, sans-serif;
                background: #111;
                color: #eee;
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                height: 100vh;
                margin: 0;
            }
            h1 { margin-bottom: 1rem; }
            form {
                display: flex;
                flex-direction: column;
                align-items: center;
                gap: 0.5rem;
                width: 90%;
                max-width: 500px;
            }
            input[type="url"] {
                width: 100%;
                padding: 10px;
                font-size: 16px;
                border-radius: 8px;
                border: 1px solid #555;
                background: #222;
                color: #eee;
            }
            button {
                background: #28a745;
                color: white;
                border: none;
                padding: 10px 20px;
                font-size: 16px;
                border-radius: 8px;
                cursor: pointer;
            }
            button:disabled { opacity: 0.5; cursor: not-allowed; }
            #msg {
                margin-top: 1rem;
                font-size: 14px;
                text-align: center;
            }
        </style>
    </head>
    <body>
        <h1>🎥 PH piratarias Downloader NYT</h1>
        <form id="form">
            <input type="url" id="url" placeholder="Cole aqui a URL do vídeo" required />
        
            <!-- Widget Cloudflare -->
            <div class="cf-turnstile" data-sitekey="0x4AAAAAAB6b69VJxARp_Wtj"></div>
        
            <!-- Dropdown de resoluções -->
            <select id="formatSelect" disabled>
                <option value="">Selecione a resolução</option>
            </select>
        
            <button type="submit" disabled>Baixar</button>
            <div id="msg"></div>
        </form>


        <script>
            const form = document.getElementById('form');
            const urlInput = document.getElementById('url');
            const msg = document.getElementById('msg');
            const formatSelect = document.getElementById('formatSelect');
            const submitBtn = form.querySelector('button');
            
            form.addEventListener('submit', async (e) => {
                e.preventDefault();
            
                if (!formatSelect.value) {
                    msg.textContent = "❌ Escolha uma resolução.";
                    return;
                }
            
                const videoUrl = urlInput.value.trim();
                const chosenFormat = formatSelect.value;
            
                msg.textContent = '⬇️ Preparando download...';
                const dlUrl = `/download?url=${encodeURIComponent(videoUrl)}&format_id=${chosenFormat}`;
                window.location.href = dlUrl;
            });
            
            // Quando digitar a URL, dispara análise automática
            urlInput.addEventListener('change', async () => {
                const videoUrl = urlInput.value.trim();
                if (!videoUrl) return;
            
                msg.textContent = '🔍 Analisando vídeo...';
                formatSelect.innerHTML = '<option>Carregando...</option>';
                submitBtn.disabled = true;
            
                const captchaToken = document.querySelector('input[name="cf-turnstile-response"]')?.value;
            
                try {
                    const res = await fetch('/analyze', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            url: videoUrl,
                            captchaToken: captchaToken
                        })
                    });
            
                    if (!res.ok) {
                        const err = await res.json();
                        throw new Error(err.detail || 'Erro ao analisar');
                    }
            
                    const data = await res.json();
            
                    if (!data.formats || data.formats.length === 0) {
                        throw new Error('Nenhum formato disponível.');
                    }
            
                    // Monta a lista de resoluções
                    formatSelect.innerHTML = '';
                    data.formats
                        .filter(f => f.vcodec !== 'none') // pega só formatos de vídeo
                        .forEach(f => {
                            const optText = `${f.height || 'audio'}p ${f.ext} ${f.format_note || ''}`.trim();
                            const opt = document.createElement('option');
                            opt.value = f.format_id;
                            opt.textContent = optText;
                            formatSelect.appendChild(opt);
                        });
            
                    msg.textContent = '✅ Formatos carregados. Selecione um.';
                    formatSelect.disabled = false;
                    submitBtn.disabled = false;
            
                } catch (err) {
                    msg.textContent = '❌ ' + err.message;
                    formatSelect.disabled = true;
                    submitBtn.disabled = true;
                }
            });

        </script>
    </body>
    </html>
    """



