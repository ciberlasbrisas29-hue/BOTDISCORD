import os
import re
import requests
import subprocess
from urllib.parse import urljoin, urlparse, quote
from flask import Flask, request, Response, jsonify, stream_with_context
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "es-419,es;q=0.9",
    "Accept-Encoding": "identity",
    "Connection": "keep-alive",
}
CHUNK_SIZE = 1024 * 64


def is_youtube(url):
    return "youtube.com" in url or "youtu.be" in url

def is_m3u8(url):
    u = url.lower().split("?")[0]
    return u.endswith(".m3u8") or u.endswith(".m3u")

def is_mpd(url):
    return url.lower().split("?")[0].endswith(".mpd")

def extract_yt_id(url):
    """Extrae el video ID de YouTube."""
    patterns = [
        r'youtu\.be/([^?&/]+)',
        r'youtube\.com/watch\?.*v=([^&]+)',
        r'youtube\.com/embed/([^?&/]+)',
        r'youtube\.com/shorts/([^?&/]+)',
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None


# ── /resolve ─────────────────────────────────────────────────────────────────
@app.route("/resolve")
def resolve():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    # YouTube → devolver embed ID (el player lo maneja con iframe)
    if is_youtube(url):
        vid_id = extract_yt_id(url)
        if vid_id:
            return jsonify({"type": "youtube", "videoId": vid_id})
        return jsonify({"error": "No se pudo extraer el ID de YouTube"}), 422

    # m3u8/mpd directo
    if is_m3u8(url) or is_mpd(url):
        proxy_url = f"/proxy/m3u8?url={quote(url, safe='')}"
        return jsonify({
            "type": "hls" if is_m3u8(url) else "dash",
            "url": proxy_url,
            "direct": url,
        })

    # Página web → intentar múltiples métodos
    return resolve_page(url)


def resolve_page(url):
    # Método 1: yt-dlp
    try:
        result = subprocess.run(
            ["yt-dlp", "-f", "best", "-g", "--no-playlist",
             "--no-check-certificates", url],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            stream_url = result.stdout.strip().split("\n")[0]
            if stream_url.startswith("http"):
                stype = "hls" if is_m3u8(stream_url) else "mp4"
                if is_m3u8(stream_url):
                    proxy_url = f"/proxy/m3u8?url={quote(stream_url, safe='')}&referer={quote(url, safe='')}"
                    return jsonify({"type": stype, "url": proxy_url, "direct": stream_url})
                return jsonify({"type": stype, "url": stream_url, "direct": stream_url})
    except Exception:
        pass

    # Método 2: fetch + regex en HTML/JS
    extracted = extract_m3u8_from_page(url)
    if extracted:
        proxy_url = f"/proxy/m3u8?url={quote(extracted, safe='')}&referer={quote(url, safe='')}"
        return jsonify({"type": "hls", "url": proxy_url, "direct": extracted})

    # Método 3: seguir redirects del parámetro ?r= (base64)
    extracted2 = resolve_r_param(url)
    if extracted2:
        proxy_url = f"/proxy/m3u8?url={quote(extracted2, safe='')}&referer={quote(url, safe='')}"
        return jsonify({"type": "hls", "url": proxy_url, "direct": extracted2})

    return jsonify({"error": "No se encontró stream reproducible. Prueba pegando el .m3u8 directo."}), 422


def resolve_r_param(url):
    """Maneja URLs con ?r=BASE64 como las de futbol-libres."""
    import base64
    parsed = urlparse(url)
    params = dict(p.split("=", 1) for p in parsed.query.split("&") if "=" in p)
    r_val = params.get("r", "")
    if not r_val:
        return None
    try:
        inner_url = base64.b64decode(r_val + "==").decode("utf-8")
        if inner_url.startswith("http"):
            # Intentar extraer m3u8 de la URL interna
            result = extract_m3u8_from_page(inner_url)
            if result:
                return result
            # yt-dlp en la URL interna
            try:
                res = subprocess.run(
                    ["yt-dlp", "-f", "best", "-g", "--no-playlist", inner_url],
                    capture_output=True, text=True, timeout=30
                )
                if res.returncode == 0:
                    s = res.stdout.strip().split("\n")[0]
                    if s.startswith("http"):
                        return s
            except Exception:
                pass
    except Exception:
        pass
    return None


def extract_m3u8_from_page(url):
    """Scrapea HTML/JS buscando URLs m3u8."""
    try:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        headers = {**BROWSER_HEADERS, "Referer": url, "Origin": origin}
        r = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        text = r.text
        patterns = [
            r'["\']?(https?://[^"\'>\s]+\.m3u8[^"\'>\s]*)["\']?',
            r'source["\']?\s*[:=]\s*["\']?(https?://[^"\'>\s]+\.m3u8[^"\'>\s]*)',
            r'file\s*[:=]\s*["\']?(https?://[^"\'>\s]+\.m3u8[^"\'>\s]*)',
            r'hls_src\s*=\s*["\']?(https?://[^"\'>\s]+\.m3u8[^"\'>\s]*)',
            r'src\s*[:=]\s*["\']?(https?://[^"\'>\s]+\.m3u8[^"\'>\s]*)',
            r'url\s*[:=]\s*["\']?(https?://[^"\'>\s]+\.m3u8[^"\'>\s]*)',
        ]
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                found = m.group(1).strip("'\"")
                if found.startswith("http"):
                    return found
    except Exception:
        pass
    return None


# ── /proxy/m3u8 ───────────────────────────────────────────────────────────────
@app.route("/proxy/m3u8")
def proxy_m3u8():
    url = request.args.get("url", "").strip()
    referer = request.args.get("referer", "")
    if not url:
        return "No URL", 400

    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    base_url = url.rsplit("/", 1)[0] + "/"

    headers = {**BROWSER_HEADERS}
    if referer:
        headers["Referer"] = referer
        headers["Origin"] = f"{urlparse(referer).scheme}://{urlparse(referer).netloc}"
    else:
        headers["Referer"] = origin + "/"
        headers["Origin"] = origin

    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        content = r.text
    except Exception as e:
        return f"Error fetching m3u8: {e}", 502

    lines = content.splitlines()
    new_lines = []
    for line in lines:
        line = line.strip()
        if line.startswith("#"):
            def rewrite_uri(m):
                inner = m.group(1)
                if inner.startswith("http"):
                    full = inner
                elif inner.startswith("/"):
                    full = origin + inner
                else:
                    full = base_url + inner
                proxied = f"/proxy/segment?url={quote(full, safe='')}&referer={quote(url, safe='')}"
                return f'URI="{proxied}"'
            line = re.sub(r'URI="([^"]+)"', rewrite_uri, line)
            new_lines.append(line)
        elif line == "":
            new_lines.append(line)
        elif line.startswith("http"):
            if is_m3u8(line):
                proxied = f"/proxy/m3u8?url={quote(line, safe='')}&referer={quote(url, safe='')}"
            else:
                proxied = f"/proxy/segment?url={quote(line, safe='')}&referer={quote(url, safe='')}"
            new_lines.append(proxied)
        else:
            full = urljoin(base_url, line)
            if is_m3u8(line):
                proxied = f"/proxy/m3u8?url={quote(full, safe='')}&referer={quote(url, safe='')}"
            else:
                proxied = f"/proxy/segment?url={quote(full, safe='')}&referer={quote(url, safe='')}"
            new_lines.append(proxied)

    rewritten = "\n".join(new_lines)
    return Response(rewritten, content_type="application/vnd.apple.mpegurl",
                    headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"})


# ── /proxy/segment ────────────────────────────────────────────────────────────
@app.route("/proxy/segment")
def proxy_segment():
    url = request.args.get("url", "").strip()
    referer = request.args.get("referer", "")
    if not url:
        return "No URL", 400

    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    headers = {**BROWSER_HEADERS}
    if referer:
        headers["Referer"] = referer
        headers["Origin"] = f"{urlparse(referer).scheme}://{urlparse(referer).netloc}"
    else:
        headers["Referer"] = origin + "/"
        headers["Origin"] = origin

    if "Range" in request.headers:
        headers["Range"] = request.headers["Range"]

    try:
        r = requests.get(url, headers=headers, stream=True, timeout=20)
        def generate():
            for chunk in r.iter_content(CHUNK_SIZE):
                yield chunk
        ct = r.headers.get("Content-Type", "video/MP2T")
        return Response(stream_with_context(generate()),
                        status=r.status_code, content_type=ct,
                        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"})
    except Exception as e:
        return f"Segment error: {e}", 502


# ── /proxy/direct ─────────────────────────────────────────────────────────────
@app.route("/proxy/direct")
def proxy_direct():
    url = request.args.get("url", "").strip()
    referer = request.args.get("referer", "")
    if not url:
        return "No URL", 400
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    headers = {**BROWSER_HEADERS, "Referer": referer or origin + "/", "Origin": origin}
    if "Range" in request.headers:
        headers["Range"] = request.headers["Range"]
    try:
        r = requests.get(url, headers=headers, stream=True, timeout=20)
        ct = r.headers.get("Content-Type", "video/mp4")
        def generate():
            for chunk in r.iter_content(CHUNK_SIZE):
                yield chunk
        return Response(stream_with_context(generate()),
                        status=r.status_code, content_type=ct,
                        headers={"Access-Control-Allow-Origin": "*"})
    except Exception as e:
        return f"Direct proxy error: {e}", 502



# ── /check — verifica si un stream está vivo ─────────────────────────────────
@app.route("/check")
def check_stream():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"ok": False, "error": "No URL"}), 400

    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    headers = {**BROWSER_HEADERS, "Referer": origin + "/", "Origin": origin}

    try:
        r = requests.head(url, headers=headers, timeout=6, allow_redirects=True)
        ok = r.status_code < 400
        return jsonify({"ok": ok, "status": r.status_code})
    except Exception as e:
        try:
            r = requests.get(url, headers=headers, timeout=6, stream=True)
            ok = r.status_code < 400
            r.close()
            return jsonify({"ok": ok, "status": r.status_code})
        except Exception as e2:
            return jsonify({"ok": False, "error": str(e2)})

# ── Health ────────────────────────────────────────────────────────────────────
@app.route("/")
def health():
    return jsonify({"status": "ok", "service": "StreamPlay Proxy"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
