import os
import re
import requests
import subprocess
from urllib.parse import urljoin, urlparse, quote, unquote
from flask import Flask, request, Response, jsonify, stream_with_context
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # Allow Fire TV player to call this API

# ── HEADERS que simulan un navegador real ────────────────────────────────────
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "es-419,es;q=0.9",
    "Accept-Encoding": "identity",  # No gzip so we can rewrite URLs easily
    "Connection": "keep-alive",
}

CHUNK_SIZE = 1024 * 64  # 64 KB chunks for streaming


def is_youtube(url):
    return "youtube.com" in url or "youtu.be" in url


def is_m3u8(url):
    u = url.lower().split("?")[0]
    return u.endswith(".m3u8") or u.endswith(".m3u")


def is_mpd(url):
    return url.lower().split("?")[0].endswith(".mpd")


# ── /resolve — dado cualquier URL, devuelve el stream directo ────────────────
@app.route("/resolve")
def resolve():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    # YouTube → yt-dlp
    if is_youtube(url):
        return resolve_youtube(url)

    # m3u8/mpd directo → devolver proxied
    if is_m3u8(url) or is_mpd(url):
        proxy_url = f"/proxy/m3u8?url={quote(url, safe='')}"
        return jsonify({
            "type": "hls" if is_m3u8(url) else "dash",
            "url": proxy_url,
            "direct": url,
        })

    # Página web con player embebido → intentar extraer con yt-dlp
    return resolve_page(url)


def resolve_youtube(url):
    try:
        result = subprocess.run(
            ["yt-dlp", "-f", "best[ext=mp4]/best", "-g", "--no-playlist", url],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            stream_url = result.stdout.strip().split("\n")[0]
            return jsonify({"type": "mp4", "url": stream_url, "direct": stream_url})

        # Fallback: probar con formato de stream HLS
        result2 = subprocess.run(
            ["yt-dlp", "-f", "95/94/93/92/best", "-g", "--no-playlist", url],
            capture_output=True, text=True, timeout=30
        )
        if result2.returncode == 0:
            stream_url = result2.stdout.strip().split("\n")[0]
            return jsonify({"type": "mp4", "url": stream_url, "direct": stream_url})

        return jsonify({"error": "No se pudo extraer el stream de YouTube: " + result.stderr[:200]}), 422
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timeout extrayendo YouTube"}), 504
    except FileNotFoundError:
        return jsonify({"error": "yt-dlp no instalado en el servidor"}), 500


def resolve_page(url):
    """Intenta extraer un stream de una página web usando yt-dlp."""
    try:
        result = subprocess.run(
            ["yt-dlp", "-f", "best", "-g", "--no-playlist", url],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            stream_url = result.stdout.strip().split("\n")[0]
            stype = "hls" if is_m3u8(stream_url) else "mp4"
            if is_m3u8(stream_url):
                proxy_url = f"/proxy/m3u8?url={quote(stream_url, safe='')}&referer={quote(url, safe='')}"
                return jsonify({"type": stype, "url": proxy_url, "direct": stream_url})
            return jsonify({"type": stype, "url": stream_url, "direct": stream_url})

        # Si yt-dlp falla, intenta fetch directo buscando m3u8 en el HTML/JS
        extracted = extract_m3u8_from_page(url)
        if extracted:
            proxy_url = f"/proxy/m3u8?url={quote(extracted, safe='')}&referer={quote(url, safe='')}"
            return jsonify({"type": "hls", "url": proxy_url, "direct": extracted})

        return jsonify({"error": "No se encontró stream reproducible en esa URL"}), 422
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timeout resolviendo la página"}), 504


def extract_m3u8_from_page(url):
    """Scrapea el HTML/JS de la página buscando URLs m3u8."""
    try:
        referer = url
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        headers = {**BROWSER_HEADERS, "Referer": referer, "Origin": origin}
        r = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        text = r.text
        # Buscar patrones comunes
        patterns = [
            r'["\']?(https?://[^"\'>\s]+\.m3u8[^"\'>\s]*)["\']?',
            r'source["\']?\s*[:=]\s*["\']?(https?://[^"\'>\s]+\.m3u8[^"\'>\s]*)',
            r'file\s*[:=]\s*["\']?(https?://[^"\'>\s]+\.m3u8[^"\'>\s]*)',
            r'hls_src\s*=\s*["\']?(https?://[^"\'>\s]+\.m3u8[^"\'>\s]*)',
            r'src\s*[:=]\s*["\']?(https?://[^"\'>\s]+\.m3u8[^"\'>\s]*)',
        ]
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                return m.group(1).strip("'\"")
    except Exception:
        pass
    return None


# ── /proxy/m3u8 — proxea el .m3u8 reescribiendo URLs internas ───────────────
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

    # Reescribir URLs relativas y absolutas dentro del m3u8
    lines = content.splitlines()
    new_lines = []
    for line in lines:
        line = line.strip()
        if line.startswith("#"):
            # Reescribir URI= dentro de tags EXT-X-KEY, EXT-X-MAP, etc.
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
            # URL absoluta — puede ser sub-playlist o segmento .ts
            if is_m3u8(line):
                proxied = f"/proxy/m3u8?url={quote(line, safe='')}&referer={quote(url, safe='')}"
            else:
                proxied = f"/proxy/segment?url={quote(line, safe='')}&referer={quote(url, safe='')}"
            new_lines.append(proxied)
        else:
            # URL relativa
            full = urljoin(base_url, line)
            if is_m3u8(line):
                proxied = f"/proxy/m3u8?url={quote(full, safe='')}&referer={quote(url, safe='')}"
            else:
                proxied = f"/proxy/segment?url={quote(full, safe='')}&referer={quote(url, safe='')}"
            new_lines.append(proxied)

    rewritten = "\n".join(new_lines)
    return Response(rewritten, content_type="application/vnd.apple.mpegurl",
                    headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"})


# ── /proxy/segment — proxea segmentos .ts / .aac / claves AES ───────────────
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

    # Pasar Range si el cliente lo pide
    if "Range" in request.headers:
        headers["Range"] = request.headers["Range"]

    try:
        r = requests.get(url, headers=headers, stream=True, timeout=20)

        def generate():
            for chunk in r.iter_content(CHUNK_SIZE):
                yield chunk

        resp_headers = {
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "no-cache",
        }
        ct = r.headers.get("Content-Type", "video/MP2T")
        return Response(stream_with_context(generate()),
                        status=r.status_code,
                        content_type=ct,
                        headers=resp_headers)
    except Exception as e:
        return f"Segment error: {e}", 502


# ── /proxy/direct — proxea cualquier URL directo (mp4, etc.) ────────────────
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
        status = r.status_code

        def generate():
            for chunk in r.iter_content(CHUNK_SIZE):
                yield chunk

        return Response(stream_with_context(generate()),
                        status=status,
                        content_type=ct,
                        headers={"Access-Control-Allow-Origin": "*"})
    except Exception as e:
        return f"Direct proxy error: {e}", 502


# ── Health check ─────────────────────────────────────────────────────────────
@app.route("/")
def health():
    return jsonify({"status": "ok", "service": "StreamPlay Proxy"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
