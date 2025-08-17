# app.py — live counter + gallery + "Make Post" image generator
# Hosting: works locally and on Render (Gunicorn). Generates PNGs for IG/X/TikTok.
import csv, os, time, threading, io, random
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request

from flask import Flask, jsonify, make_response, send_from_directory, request, send_file

# Image generation deps
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import qrcode

# ------------------ Config ------------------
P0 = 2_000_000
CSV_URL = "https://data.techforpalestine.org/api/v2/casualties_daily.csv"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

SYNC_INTERVAL_HOURS = 24
IMAGE_DIR = os.path.join("static", "images")
ALLOWED_EXT = (".png", ".jpg", ".jpeg", ".webp", ".avif")

# Save a copy of each generated post image on the server (for local dev).
# On many hosts, the filesystem is ephemeral (files may disappear on redeploy).
SAVE_POSTS = True
POSTS_DIR = "posts"
# --------------------------------------------

app = Flask(__name__)

state = {
    "as_of_utc": None,
    "cumulative_deaths": None,
    "remaining_at_sync": None,   # int baseline after backfill
    "r14_daily": None,           # float
    "r14_prev": None,            # float
    "trend": "•",
    "last_sync_epoch": None,     # float aligned so next tick cadence is correct
    "last_report_cutoff_epoch": None,  # float (end of last report day, 00:00 next day UTC)
}

def log(msg): print(msg, flush=True)

# ------------------ Data sync & math ------------------

def fetch_csv_rows():
    req = Request(CSV_URL, headers={"User-Agent": USER_AGENT, "Accept": "text/csv,*/*;q=0.9"})
    with urlopen(req, timeout=30) as r:
        data = r.read().decode("utf-8", errors="replace").splitlines()
    return list(csv.DictReader(data))

def safe_int(v):
    if v is None or v == "": return None
    try: return int(v)
    except Exception:
        try: return int(float(v))
        except Exception: return None

def rolling_avg(vals, n=14):
    tail = [v for v in vals if v is not None][-n:]
    return (sum(tail) / len(tail)) if tail else 0.0

def parse_rows(rows):
    def get(row, *cands):
        keys = {k.strip().lower(): k for k in row.keys()}
        for c in cands:
            k = keys.get(c.lower())
            if k: return row[k]
        return None
    dates, daily, cumulative = [], [], []
    for row in rows:
        d  = get(row, "report_date", "date")
        k  = get(row, "killed", "deaths", "killed_gaza")
        kc = get(row, "killed_cum", "cumulative_deaths", "killed_cumulative")
        dates.append(d)
        daily.append(safe_int(k))
        cumulative.append(safe_int(kc))
    return dates, daily, cumulative

def parse_date_utc(date_str):
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return d + timedelta(days=1)  # cutoff = end of that report day (00:00 next day)

def run_sync_once():
    try:
        rows = fetch_csv_rows()
        dates, daily, cumulative = parse_rows(rows)
        if not cumulative or cumulative[-1] is None:
            raise ValueError("Missing cumulative deaths")

        last_date = dates[-1]
        cutoff_dt = parse_date_utc(last_date)     # end of last report day (UTC)
        cutoff_epoch = cutoff_dt.timestamp()

        D_cum = cumulative[-1]
        R14 = rolling_avg(daily, 14)
        prev = [v for v in daily if v is not None][-28:-14]
        R14_prev = (sum(prev)/len(prev)) if prev else 0.0
        trend = "▲" if R14 > R14_prev else ("▼" if R14 < R14_prev else "•")

        secs_per = (86400.0 / R14) if R14 and R14 > 0 else None

        now_epoch = time.time()
        extra = 0
        aligned_last_sync_epoch = now_epoch
        if secs_per:
            elapsed_since_cutoff = max(0, now_epoch - cutoff_epoch)
            extra = int(elapsed_since_cutoff // secs_per)
            residual = elapsed_since_cutoff % secs_per
            aligned_last_sync_epoch = now_epoch - residual

        remaining_now = max(0, P0 - (D_cum + extra))

        state["as_of_utc"] = datetime.now(timezone.utc).isoformat()
        state["cumulative_deaths"] = D_cum
        state["remaining_at_sync"] = int(remaining_now)
        state["r14_daily"] = float(round(R14, 2))
        state["r14_prev"] = float(round(R14_prev, 2))
        state["trend"] = trend
        state["last_sync_epoch"] = aligned_last_sync_epoch
        state["last_report_cutoff_epoch"] = cutoff_epoch

        log(f"[sync] OK — cum={D_cum:,} R14={R14:.2f} trend={trend} cutoff={last_date} "
            f"extra={extra:,} baseline={remaining_now:,}")
    except Exception as e:
        log(f"[sync] FAILED: {e}")

def cron_sync_loop():
    while True:
        time.sleep(SYNC_INTERVAL_HOURS * 3600)
        run_sync_once()

def seconds_per_person():
    r14 = state["r14_daily"] or 0.0
    return (86400.0 / r14) if r14 > 0 else None

def compute_remaining_now(now_epoch=None):
    """Compute current integer remaining from baseline + elapsed/secs_per."""
    if state["remaining_at_sync"] is None or state["last_sync_epoch"] is None:
        return None
    if now_epoch is None:
        now_epoch = time.time()
    s_per = seconds_per_person()
    if not s_per or s_per <= 0:
        return int(state["remaining_at_sync"])
    drops = int((now_epoch - state["last_sync_epoch"]) // s_per)
    return max(0, int(state["remaining_at_sync"]) - drops)

# ------------------ Images & gallery ------------------

def list_images_random():
    if not os.path.isdir(IMAGE_DIR): return []
    files = [f for f in os.listdir(IMAGE_DIR)
             if f.lower().endswith(ALLOWED_EXT) and not f.startswith(".")]
    files.sort(key=lambda n: (int(os.path.getmtime(os.path.join(IMAGE_DIR, n))), n.lower()))
    if not files: return []
    rot = int(time.time() // 60) % len(files)  # pseudo-random order per minute
    return files[rot:] + files[:rot]

def ensure_dirs():
    os.makedirs(IMAGE_DIR, exist_ok=True)
    if SAVE_POSTS:
        os.makedirs(POSTS_DIR, exist_ok=True)

# ------------------ Flask hooks & APIs ------------------

@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Cache-Control"] = "no-store"
    return resp

@app.route("/api/status.json")
def api_status():
    s_per = seconds_per_person()
    out = {
        "as_of_utc": state["as_of_utc"],
        "p0": P0,
        "cumulative_deaths": state["cumulative_deaths"],
        "rolling_14d_avg_daily": state["r14_daily"],
        "rolling_14d_prev_daily": state["r14_prev"],
        "trend": state["trend"],
        "remaining_at_sync": state["remaining_at_sync"],
        "last_sync_epoch": state["last_sync_epoch"],
        "seconds_per_person": int(s_per) if s_per else None,
        "last_report_cutoff_epoch": state["last_report_cutoff_epoch"],
        "last_updated_utc": datetime.now(timezone.utc).isoformat(),
        "source_csv": CSV_URL,
    }
    return jsonify(out)

@app.route("/api/gallery.json")
def api_gallery():
    items = list_images_random()
    out = [{"src": f"/static/images/{name}?v={int(os.path.getmtime(os.path.join(IMAGE_DIR,name)))}",
            "name": name} for name in items]
    return jsonify({"count": len(out), "items": out})

@app.route("/static/images/<path:filename>")
def image_file(filename):
    if ".." in filename.replace("\\","/"): return ("Not found", 404)
    return send_from_directory(IMAGE_DIR, filename)

# ------------------ Make Post: PNG generator ------------------

SIZE_MAP = {
    "square": (1080, 1080),   # IG feed, FB, X
    "story":  (1080, 1920),   # IG Stories/Reels, TikTok
    "x":      (1200, 675),    # X "card"
}

def pick_background(bg_mode, img_name_param=None):
    """Return absolute path to image file or None (for black)."""
    if bg_mode == "black":
        return None
    files = list_images_random()
    if not files:
        return None
    if img_name_param:
        # sanitize
        candidate = os.path.basename(img_name_param)
        if candidate in files:
            return os.path.join(IMAGE_DIR, candidate)
    if bg_mode == "current":
        # Use first in current rotation as proxy for "current"
        return os.path.join(IMAGE_DIR, files[0])
    if bg_mode == "random":
        return os.path.join(IMAGE_DIR, random.choice(files))
    # Fallback
    return os.path.join(IMAGE_DIR, files[0])

def load_font(size, bold=False):
    # Try a few common fonts; fallback to default
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:\\Windows\\Fonts\\arialbd.ttf" if bold else "C:\\Windows\\Fonts\\arial.ttf",
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
        "arialbd.ttf" if bold else "arial.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            continue
    return ImageFont.load_default()

def draw_centered_number(draw, canvas_w, canvas_h, text, base_color=(255,255,255)):
    # dynamic font sizing to fit width
    # Start big and shrink until it fits
    max_w = int(canvas_w * 0.9)
    target_px = int(min(canvas_w, canvas_h) * 0.19)
    fsize = max(24, target_px)
    font = load_font(fsize, bold=True)
    while True:
        tw, th = draw.textlength(text, font=font), font.size + int(font.size*0.35)
        if tw <= max_w or fsize <= 24:
            break
        fsize -= 4
        font = load_font(fsize, bold=True)

    x = (canvas_w - draw.textlength(text, font=font)) / 2
    y = (canvas_h - th) / 2

    # Outline for readability
    shadow = (0,0,0)
    for dx,dy in [(-2,-2),(2,-2),(-2,2),(2,2),(0,0)]:
        draw.text((x+dx, y+dy), text, font=font, fill=shadow if (dx or dy) else base_color)

def draw_footer(draw, canvas_w, canvas_h, r14, timestamp_utc, color=(255,255,255)):
    font_small = load_font(max(18, int(canvas_h*0.020)))
    footer = f"14-day avg ≈ {round(r14)}/day • source: data.techforpalestine.org • {timestamp_utc} UTC"
    pad = int(canvas_w * 0.03)
    y = canvas_h - int(canvas_h * 0.05)
    # subtle soft shadow
    draw.text((pad+2, y+2), footer, font=font_small, fill=(0,0,0))
    draw.text((pad, y), footer, font=font_small, fill=color)

def apply_bg(canvas, bg_path):
    if not bg_path:
        return  # keep black
    try:
        img = Image.open(bg_path).convert("RGB")
        cw, ch = canvas.size
        iw, ih = img.size
        scale = max(cw/iw, ch/ih)
        new_size = (int(iw*scale), int(ih*scale))
        img = img.resize(new_size, Image.LANCZOS)
        # center-crop
        left = (img.size[0]-cw)//2
        top  = (img.size[1]-ch)//2
        img = img.crop((left, top, left+cw, top+ch))
        canvas.paste(img)
        # readability overlay
        overlay = Image.new("RGBA", canvas.size, (0,0,0,0))
        ov_draw = ImageDraw.Draw(overlay)
        # bottom gradient
        for i in range(0,240):
            alpha = int(180 * (i/240))
            ov_draw.rectangle([0, canvas.size[1]-i, canvas.size[0], canvas.size[1]], fill=(0,0,0,alpha))
        canvas.alpha_composite(overlay)
    except Exception as e:
        log(f"[post] BG load failed: {e}")

@app.route("/post.png")
def post_png():
    """
    Generate a platform-ready PNG on demand.
    Params:
      size = square|story|x (default square)
      bg   = current|random|black  (default current)
      img  = specific filename from static/images (used when bg=current from UI)
      qr   = 0|1  include QR to this site (default 1)
    """
    size_key = (request.args.get("size") or "square").lower()
    bg_mode  = (request.args.get("bg") or "current").lower()
    img_name = request.args.get("img")
    include_qr = (request.args.get("qr") or "1") not in ("0", "false", "no")

    W,H = SIZE_MAP.get(size_key, SIZE_MAP["square"])
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.strftime("%Y-%m-%d %H:%M:%S")

    remaining = compute_remaining_now()
    r14 = state["r14_daily"] or 0.0

    # Canvas
    canvas = Image.new("RGBA", (W,H), (0,0,0,255))
    # Background
    bg_path = pick_background(bg_mode, img_name_param=img_name)
    apply_bg(canvas, bg_path)

    draw = ImageDraw.Draw(canvas)
    # Big number
    num_text = f"{remaining:,}" if remaining is not None else "—"
    draw_centered_number(draw, W, H, num_text)
    # Footer
    draw_footer(draw, W, H, r14, now_iso)

    # Optional QR (bottom-right)
    if include_qr:
        try:
            # Point to home page
            base_url = request.url_root.rstrip("/")
            qr = qrcode.QRCode(box_size=10, border=1)
            qr.add_data(base_url)
            qr.make(fit=True)
            qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
            qsize = 220 if H >= 1400 else 160
            qr_img = qr_img.resize((qsize, qsize), Image.NEAREST)
            # place with small white frame
            qr_pad = int(W*0.03)
            frame = Image.new("RGB", (qsize+16, qsize+16), (255,255,255))
            frame.paste(qr_img, (8,8))
            canvas.paste(frame, (W - qsize - 16 - qr_pad, H - qsize - 16 - qr_pad))
        except Exception as e:
            log(f"[post] QR failed: {e}")

    # Save to memory (and optionally to disk)
    out = io.BytesIO()
    final = canvas.convert("RGB")  # PNG RGB
    final.save(out, format="PNG", optimize=True)
    out.seek(0)

    if SAVE_POSTS:
        try:
            ensure_dirs()
            stamp = now_utc.strftime("%Y%m%d_%H%M%S")
            fname = f"{stamp}_{size_key}.png"
            final.save(os.path.join(POSTS_DIR, fname), format="PNG", optimize=True)
        except Exception as e:
            log(f"[post] save failed: {e}")

    # Force download
    resp = send_file(out, mimetype="image/png", as_attachment=True,
                     download_name=f"gaza_countdown_{size_key}.png")
    resp.headers["Cache-Control"] = "no-store"
    return resp

# ------------------ Page UI ------------------

@app.route("/")
def index():
    html = """
<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gaza — live gallery + countdown + Make Post</title>
<style>
  *{box-sizing:border-box}
  html,body{height:100%;margin:0;background:#000;color:#fff;font-family:system-ui,Segoe UI,Inter,Arial,sans-serif}
  .stage{position:fixed;inset:0;overflow:hidden}
  .slide{position:absolute;inset:0;background-size:cover;background-position:center;opacity:0;transition:opacity 1.4s ease}
  .slide.show{opacity:1}
  .overlay{position:fixed;left:0;right:0;bottom:0;padding:16px 20px;
           background:linear-gradient(180deg,rgba(0,0,0,0) 0%,rgba(0,0,0,.55) 30%,rgba(0,0,0,.7) 100%)}
  .num{font-weight:900;font-size:min(14vw,120px);letter-spacing:.02em;line-height:1}
  .row{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
  .pill{background:rgba(255,255,255,.12);border-radius:999px;padding:4px 10px;font-size:13px}
  .sub{opacity:.9;font-size:min(4.2vw,18px)}
  .meta{opacity:.75;font-size:min(3.6vw,14px)}
  .btn{position:fixed;top:14px;right:14px;z-index:9999;
       background:rgba(0,0,0,.35);border:1px solid rgba(255,255,255,.35);
       color:#fff;padding:10px 14px;border-radius:999px;font-size:14px;cursor:pointer;
       backdrop-filter:blur(6px)}
  .btn:active{transform:scale(0.98)}
  .panel{position:fixed;top:60px;right:14px;z-index:9998;background:rgba(0,0,0,.7);
         border:1px solid rgba(255,255,255,.25);border-radius:16px;padding:12px 12px;display:none;min-width:260px}
  .panel label{font-size:13px;opacity:.9}
  .panel select,.panel input[type=checkbox]{margin-top:6px;margin-bottom:8px}
  .dl{width:100%;margin-top:6px;padding:10px 14px;border-radius:12px;background:#fff;color:#000;border:0;cursor:pointer}
  .hint{position:fixed;top:14px;left:14px;z-index:9998;font-size:12px;opacity:.6}
</style>

<button id="nextBtn" class="btn" title="Advance image">••• Next</button>
<button id="postBtn" class="btn" style="right:120px" title="Make Post">Make Post</button>
<div class="panel" id="postPanel">
  <div style="font-weight:700;margin-bottom:6px">Make Post</div>
  <label>Size</label><br>
  <select id="pSize">
    <option value="square">Square (1080×1080)</option>
    <option value="story">Story/Reel (1080×1920)</option>
    <option value="x">X Card (1200×675)</option>
  </select><br>
  <label>Background</label><br>
  <select id="pBg">
    <option value="current">Current image</option>
    <option value="random">Random image</option>
    <option value="black">Black card</option>
  </select><br>
  <label><input type="checkbox" id="pQr" checked> Include QR</label><br>
  <button class="dl" id="pDownload">Download PNG</button>
</div>
<div class="hint">tap/double-tap or press <b>N</b></div>

<div class="stage" id="stage"></div>
<div class="overlay">
  <div class="num" id="num">—</div>
  <div class="row sub">
    <span class="pill">remaining</span>
    <span>P₀=2,000,000</span>
    <span>• 14d avg/day: <b id="r14">—</b> <span id="trend">•</span></span>
  </div>
  <div class="meta" id="asof">as of —</div>
</div>

<script>
  console.log("mode: public host + make-post");
  const STAGE=document.getElementById('stage');
  const NUM=document.getElementById('num');
  const R14=document.getElementById('r14');
  const TREND=document.getElementById('trend');
  const ASOF=document.getElementById('asof');
  const NEXT=document.getElementById('nextBtn');

  const POSTBTN=document.getElementById('postBtn');
  const PANEL=document.getElementById('postPanel');
  const P_SIZE=document.getElementById('pSize');
  const P_BG=document.getElementById('pBg');
  const P_QR=document.getElementById('pQr');
  const P_DL=document.getElementById('pDownload');

  let imgs=[], idx=-1;
  let baseline=null, lastSync=null, secsPer=null;
  let currentInt=null;

  function mkSlide(src){
    const d=document.createElement('div');
    d.className='slide';
    d.style.backgroundImage=`url("${src}")`;
    return d;
  }
  function showFirstSlide(){
    if(!imgs.length) return;
    idx=0; const s=mkSlide(imgs[idx]); STAGE.appendChild(s);
    requestAnimationFrame(()=>s.classList.add('show'));
  }
  function nextSlide(){
    if(!imgs.length) return;
    idx=(idx+1)%imgs.length;
    const s=mkSlide(imgs[idx]); STAGE.appendChild(s);
    requestAnimationFrame(()=>s.classList.add('show'));
    const olds=STAGE.querySelectorAll('.slide');
    if(olds.length>1){ const prev=olds[0]; prev.classList.remove('show'); setTimeout(()=>prev.remove(),1500); }
  }
  async function loadGallery(){
    const r=await fetch('/api/gallery.json',{cache:'no-store'});
    const j=await r.json();
    imgs=j.items.map(x=>x.src);
    if(imgs.length && STAGE.children.length===0){ showFirstSlide(); }
  }
  async function loadStatus(){
    const r=await fetch('/api/status.json',{cache:'no-store'});
    const j=await r.json();
    baseline = j.remaining_at_sync;
    lastSync = j.last_sync_epoch;
    const r14 = j.rolling_14d_avg_daily || 0;
    secsPer  = (j.seconds_per_person && j.seconds_per_person>0)
               ? j.seconds_per_person
               : (r14>0 ? Math.max(1, Math.round(86400 / r14)) : null);
    R14.textContent = Math.round(r14);
    TREND.textContent = j.trend || '•';
    ASOF.textContent = 'as of ' + (j.as_of_utc || '—');
    currentInt = null; tickDraw();
  }
  function computeNowInt(){
    if(baseline==null || lastSync==null || !secsPer || secsPer<=0) return null;
    const elapsed = (Date.now()/1000) - lastSync;
    const drops = Math.floor(elapsed / secsPer);
    return Math.max(0, baseline - drops);
  }
  function tickDraw(){
    const val = computeNowInt();
    if(val==null) return;
    if(currentInt===null){ currentInt = val; NUM.textContent = val.toLocaleString(); return; }
    if(val < currentInt){
      for(let i=0;i<Math.min(10, currentInt - val); i++){ nextSlide(); }
      currentInt = val;
      NUM.textContent = currentInt.toLocaleString();
    }
  }
  setInterval(tickDraw, 1000);
  NEXT.addEventListener('click', nextSlide);
  let lastTap=0;
  window.addEventListener('touchend', (e)=>{
    const now=Date.now();
    if(now-lastTap<350){ nextSlide(); }
    lastTap=now;
  }, {passive:true});
  window.addEventListener('keydown', (e)=>{ if((e.key||'').toLowerCase()==='n'){ nextSlide(); } });

  // Make Post panel
  POSTBTN.addEventListener('click', ()=>{
    PANEL.style.display = (PANEL.style.display==='block'?'none':'block');
  });
  P_DL.addEventListener('click', ()=>{
    const size = P_SIZE.value;
    const bg = P_BG.value;
    const qr = P_QR.checked ? 1 : 0;
    // if bg=current, pass the currently displayed filename to the server
    let params = new URLSearchParams({size, bg, qr});
    if(bg==='current' && imgs.length){
      // imgs[idx] looks like "/static/images/name.webp?v=123"
      const src = imgs[Math.max(0, idx)];
      const clean = src.split('/').pop().split('?')[0]; // filename
      params.set('img', clean);
    }
    const href = '/post.png?' + params.toString();
    const a = document.createElement('a');
    a.href = href;
    a.download = ''; // server sets filename; this hints download
    document.body.appendChild(a);
    a.click();
    a.remove();
  });

  loadStatus(); setInterval(loadStatus, 60000);
  loadGallery();
</script>
"""
    r = make_response(html)
    r.headers["Cache-Control"] = "no-store"
    return r

# ------------------ bootstrap ------------------

def start_background_sync():
    run_sync_once()
    t = threading.Thread(target=cron_sync_loop, daemon=True)
    t.start()

if __name__ == "__main__":
    ensure_dirs()
    start_background_sync()
    app.run(host="0.0.0.0", port=5000, debug=True)
