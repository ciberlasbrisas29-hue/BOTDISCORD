# StreamPlay — Fire TV Player + Proxy

## Estructura

```
streamplay/
├── app.py           ← Backend Flask (sube a Render)
├── requirements.txt
├── render.yaml
└── player.html      ← Abre en Silk Browser del Fire TV
```

## Deploy en Render

1. Sube `app.py`, `requirements.txt`, `render.yaml` a un repo de GitHub
2. En Render → **New Web Service** → conecta el repo
3. Render detecta `render.yaml` automáticamente
4. Espera el deploy (~3 min la primera vez)
5. Copia la URL: `https://streamplay-proxy.onrender.com`

## Usar en Fire TV

1. Abre `player.html` en el navegador Silk del Fire TV
   - Puedes subirlo a GitHub Pages / Netlify gratis
   - O acceder desde `file://` si tienes el archivo en el device
2. En el campo **Proxy**, pega la URL de Render
3. Pega cualquier link en el campo de URL → Reproducir

## Links que soporta

| Tipo | Ejemplo | Necesita proxy |
|------|---------|----------------|
| YouTube | https://youtube.com/watch?v=... | ✅ Sí |
| FutbolLibre | https://futbol-libres.su/... | ✅ Sí |
| HLS directo | https://cdn.../stream.m3u8 | Recomendado |
| MP4 sin CORS | https://cdn.../video.mp4 | ❌ No |
| IPTV m3u8 | https://iptv.../canal.m3u8 | Recomendado |

## Notas

- yt-dlp se actualiza solo en cada deploy de Render
- Los tokens de FutbolLibre expiran — si el stream se corta, recarga el link
- Render free tier se "duerme" tras 15min sin uso → primer request tarda ~30s
