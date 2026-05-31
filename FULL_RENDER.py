#!/usr/bin/env python3
"""
FULL_RENDER.py — production pipeline for full-length music video
Pipeline:
  1. Python → intro.mp4   (200 frames: art + lightning + logo/text)
  2. FFmpeg → spin.mp4    (vinyl spinning for rest of track duration)
  3. FFmpeg → concat.mp4  (intro + spin)
  4. FFmpeg → blend.mp4   (same art, opposite pan, screen blend)
  5. FFmpeg → glitch.mp4  (chromatic aberration + scanlines + noise)
  6. FFmpeg → final.mp4   (audio + fade-out)

Usage:
  python3 Instrument/FFmpeg/FULL_RENDER.py \
    --art path/to/art.png \
    --audio path/to/track.mp3 \
    --text "yaromat - sun" \
    --output output/final.mp4
"""
import io, math, random, subprocess, tempfile, time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ── Config ───────────────────────────────────────────────────────────────────
W = H      = 1080
FPS        = 30
INTRO_F    = 200       # 6.67s intro (phases 1+2)
PAN        = 80

R          = int(W * 0.493)
LABEL_R    = int(R * 0.44)
HOLE_R     = int(R * 0.034)
CX = CY    = W // 2
GROOVES    = 18
OMEGA      = (16.67 / 60) * 2 * math.pi

LOGO_SZ    = 180
FONT_SZ    = 64
FONT_PATH  = "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"
GLOW_COL   = (255, 255, 255)
CYAN       = (0, 243, 255)
WHITE      = (255, 255, 255)
GRID_STEP  = 80

LOGO_START  = 45
LIGHTNING_F = 42
FADE_SEC    = 2.0      # fade-out duration at end

# ── Helpers ──────────────────────────────────────────────────────────────────
def ease3(t):  return 1 - (1-t)**3
def ease2(t):  return 1 - (1-t)**2
def clamp(v,a,b): return max(a, min(b, v))
def interp(f, f0, f1, v0, v1, ease=None):
    t = clamp((f-f0)/(f1-f0), 0, 1)
    if ease: t = ease(t)
    return v0 + (v1-v0)*t

def run(cmd, **kw):
    print(f"  $ {' '.join(str(c) for c in cmd[:6])}...")
    subprocess.run(cmd, check=True, **kw)

# ── Art ──────────────────────────────────────────────────────────────────────
def load_art(path, extra=0):
    img = Image.open(path).convert("RGB")
    tw, th = W+extra*2, H+extra*2
    s = max(tw/img.width, th/img.height)
    nw, nh = int(img.width*s), int(img.height*s)
    img = img.resize((nw,nh), Image.LANCZOS)
    x, y = (nw-tw)//2, (nh-th)//2
    return img.crop((x,y,x+tw,y+th))

def art_pan_frame(art_pan, frame, total=INTRO_F):
    t  = clamp(frame/total, 0, 1)
    ox = int(PAN*(1-math.cos(t*math.pi)))
    oy = int(PAN*math.sin(t*math.pi*0.5))
    return art_pan.crop((ox,oy,ox+W,oy+H))

def darken(img, f=0.45):
    return Image.blend(img, Image.new("RGB", img.size, 0), f)

def make_grid_arr(base_arr):
    g = int(CYAN[1]*0.08)
    ov = np.zeros((H,W,3), dtype=np.uint8)
    ov[::GRID_STEP,:,1] = g; ov[:,::GRID_STEP,1] = g
    return np.clip(base_arr.astype(np.int16)+ov, 0, 255).astype(np.uint8)

# ── Lightning ─────────────────────────────────────────────────────────────────
def _make_bolt(seed=7):
    rng = random.Random(seed)
    pts = [(CX+rng.randint(-40,40), 80)]
    x, y = pts[0]
    while y < H-100:
        x += rng.randint(-160,160); y += rng.randint(70,120)
        pts.append((clamp(int(x),60,W-60), min(int(y),H-80)))
    return pts

BOLT = _make_bolt()

def apply_lightning(img, frame):
    rel = frame - LIGHTNING_F
    if rel < 0 or rel >= 7: return img
    alphas = [0.6,0.9,0.55,0.35,0.2,0.1,0.05]
    fa = alphas[rel]
    if fa > 0: img = Image.blend(img, Image.new("RGB",(W,H),(240,245,255)), fa)
    if rel < 4:
        draw = ImageDraw.Draw(img)
        lw = max(1,4-rel)
        draw.line(BOLT, fill=(200,220,255), width=lw+2)
        draw.line(BOLT, fill=(255,255,255), width=lw)
    return img

# ── Vinyl ─────────────────────────────────────────────────────────────────────
def make_vinyl_rgba(art):
    img  = Image.new("RGBA",(W,H),(0,0,0,0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([CX-R,CY-R,CX+R,CY+R], fill=(20,20,20,255))
    for i in range(GROOVES):
        t = (i+1)/(GROOVES+1)
        r = int(LABEL_R+(R*0.97-LABEL_R)*t)
        draw.ellipse([CX-r,CY-r,CX+r,CY+r], outline=(30,30,30,255),
                     width=2 if i%4==0 else 1)
    art_s = art.resize((LABEL_R*2,LABEL_R*2), Image.LANCZOS)
    mask  = Image.new("L",(LABEL_R*2,LABEL_R*2),0)
    ImageDraw.Draw(mask).ellipse([0,0,LABEL_R*2-1,LABEL_R*2-1], fill=255)
    img.paste(art_s,(CX-LABEL_R,CY-LABEL_R),mask)
    draw.ellipse([CX-LABEL_R,CY-LABEL_R,CX+LABEL_R,CY+LABEL_R],
                 outline=(50,50,50,200), width=1)
    draw.ellipse([CX-HOLE_R,CY-HOLE_R,CX+HOLE_R,CY+HOLE_R], fill=(0,0,0,0))
    return img

def make_vinyl_ring():
    img  = Image.new("RGBA",(W,H),(0,0,0,0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([CX-R,CY-R,CX+R,CY+R], fill=(20,20,20,255))
    for i in range(GROOVES):
        t = (i+1)/(GROOVES+1)
        r = int(LABEL_R+(R*0.97-LABEL_R)*t)
        draw.ellipse([CX-r,CY-r,CX+r,CY+r], outline=(30,30,30,255),
                     width=2 if i%4==0 else 1)
    draw.ellipse([CX-LABEL_R-1,CY-LABEL_R-1,CX+LABEL_R+1,CY+LABEL_R+1], fill=(0,0,0,0))
    return img

def clip_circle(art, radius):
    out  = Image.new("RGBA",(W,H),(0,0,0,0))
    mask = Image.new("L",(W,H),0)
    ImageDraw.Draw(mask).ellipse([CX-radius,CY-radius,CX+radius,CY+radius], fill=255)
    out.paste(art.convert("RGBA"), mask=mask)
    return out

MAX_R = int(math.sqrt(CX**2+CY**2))+20

# ── Logo ──────────────────────────────────────────────────────────────────────
def _rline(draw, p1, p2, color, lw):
    draw.line([p1,p2], fill=color, width=lw)
    r = lw//2
    for px,py in (p1,p2): draw.ellipse([px-r,py-r,px+r,py+r], fill=color)

def make_logo(color):
    sz=LOGO_SZ+40; c2=sz//2; pad=12
    img=Image.new("RGBA",(sz,sz),(0,0,0,0)); draw=ImageDraw.Draw(img)
    fork_y=int(sz*0.42); arm_len=int(sz*0.57); lw=14
    tip_y=fork_y-int(arm_len*math.sin(math.radians(45))); col=(*color,255)
    _rline(draw,(c2,tip_y),(c2,sz-pad),col,lw)
    lx=c2-int(arm_len*math.cos(math.radians(45)))
    _rline(draw,(c2,fork_y),(lx,tip_y),col,lw)
    rx=c2+int(arm_len*math.cos(math.radians(45)))
    _rline(draw,(c2,fork_y),(rx,tip_y),col,lw)
    mid_y=(tip_y+fork_y)//2; blen=int(arm_len*0.55)
    bx=c2+int(blen*math.cos(math.radians(45)))
    by=mid_y-int(blen*math.sin(math.radians(45)))
    _rline(draw,(c2,mid_y),(bx,by),col,lw)
    return img

def make_letter_cache(text, font):
    cache={}; tmp_d=ImageDraw.Draw(Image.new("RGBA",(1,1)))
    asc,_=font.getmetrics(); bl=asc+4; lh=FONT_SZ+30; PAD=6
    for i,ch in enumerate(text):
        bbox=tmp_d.textbbox((0,0),ch,font=font,anchor="ls")
        cw=max(1,bbox[2]-bbox[0])
        ltr=Image.new("RGBA",(cw+PAD*2,lh),(0,0,0,0))
        ImageDraw.Draw(ltr).text((PAD-bbox[0],bl),ch,font=font,
                                  fill=(*WHITE,255),anchor="ls")
        glow=ltr.filter(ImageFilter.GaussianBlur(5))
        r,g,b,a=glow.split(); r=r.point(lambda p:0)
        glow=Image.merge("RGBA",(r,g,b,a))
        cache[i]=(np.array(glow),np.array(ltr),cw+PAD*2)
    return cache

def letter_pos(text, cache, margin=None):
    if margin is None: margin=FONT_SZ
    cw=[cache[i][2] for i in range(len(text))]
    avail=W-2*margin; n=max(1,len(text)-1)
    gap=(avail-sum(cw))/n*0.90; bw=sum(cw)+gap*n
    x=margin+max(0,(avail-bw)/2); pos=[]
    for i in range(len(text)):
        pos.append(int(x)); x+=cw[i]+gap
    return pos

def draw_logo_text(img, local_f, lb, lg, lc, lpos, text):
    draw=ImageDraw.Draw(img)
    sy=int((local_f/100)*H)%H; sc=tuple(int(c*0.25) for c in CYAN)
    for dy in range(-2,3):
        a=max(0,1-abs(dy)*0.4); c=tuple(int(v*a) for v in sc)
        if 0<=sy+dy<H: draw.line([(0,sy+dy),(W,sy+dy)],fill=c)
    lx_=interp(local_f,0,23,-W/2,0,ease3); lrot=interp(local_f,0,28,-45,0,ease3)
    gr=18; bsz=LOGO_SZ+40; pad=gr*3; csz=bsz+pad*2; off=pad
    gl=Image.new("RGBA",(csz,csz),(0,0,0,0)); gl.paste(lg,(off,off),lg)
    gl=gl.filter(ImageFilter.GaussianBlur(gr))
    ga=np.array(gl); ga[:,:,3]=np.clip(ga[:,:,3].astype(int)*55//255,0,255).astype(np.uint8)
    gl=Image.fromarray(ga,"RGBA")
    bl2=Image.new("RGBA",(csz,csz),(0,0,0,0)); bl2.paste(lb,(off,off),lb)
    cm=Image.alpha_composite(gl,bl2).rotate(-lrot,resample=Image.BICUBIC)
    lcx=int(W/2+lx_); lcy=int(H*0.38)
    img.paste(cm.convert("RGB"),(lcx-csz//2,lcy-csz//2),cm.split()[3])
    ty_b=int(H*0.57)
    for i in range(len(text)):
        sf=10+i*2; op=clamp(interp(local_f,sf,sf+4,0,1),0,1)
        yof=interp(local_f,sf,sf+6,20,0,ease2)
        if op<=0: continue
        ga2,sa2,_=lc[i]; lxi=lpos[i]; lyi=int(ty_b+yof)
        if op>=1:
            gi=Image.fromarray(ga2,"RGBA"); si=Image.fromarray(sa2,"RGBA")
        else:
            g_=ga2.copy(); g_[:,:,3]=(g_[:,:,3]*op).astype(np.uint8)
            s_=sa2.copy(); s_[:,:,3]=(s_[:,:,3]*op).astype(np.uint8)
            gi=Image.fromarray(g_,"RGBA"); si=Image.fromarray(s_,"RGBA")
        img.paste(gi.convert("RGB"),(lxi,lyi-5),gi.split()[3])
        img.paste(si.convert("RGB"),(lxi,lyi),  si.split()[3])
    return img

# ── Phase renderers ───────────────────────────────────────────────────────────
def render_p1(frame, art_pan, lb, lg, lc, lpos, text):
    moving=art_pan_frame(art_pan, frame)
    alpha=clamp(frame/20,0,1)
    bg=Image.blend(Image.new("RGB",(W,H),(0,0,0)),moving,alpha) if alpha<1 else moving.copy()
    bg=apply_lightning(bg,frame)
    if frame>=LOGO_START:
        base=Image.fromarray(make_grid_arr(np.array(darken(moving))))
        bg=draw_logo_text(base,frame-LOGO_START,lb,lg,lc,lpos,text)
    return np.array(bg)

def render_p2(frame, art_pan, lb, lg, lc, lpos, text):
    moving=art_pan_frame(art_pan, 100+frame)
    base=Image.fromarray(make_grid_arr(np.array(darken(moving))))
    local_f=(100-LOGO_START)+frame
    return np.array(draw_logo_text(base,local_f,lb,lg,lc,lpos,text))

def render_p3_transition(frame, art, art_pan, vinyl_rgba, vinyl_ring):
    """Первые 70 кадров фазы 3: сжатие арта в лейбл."""
    SQUEEZE=70
    t=clamp(frame/SQUEEZE,0,1); et=ease3(t)
    cur_r=int(LABEL_R+(MAX_R-LABEL_R)*(1-et))
    img=darken(art_pan_frame(art_pan, 200+frame))
    if t<1:
        ra=clamp(et*1.5,0,1)
        rng=np.array(vinyl_ring.copy())
        rng[:,:,3]=(rng[:,:,3]*ra).astype(np.uint8)
        ri=Image.fromarray(rng,"RGBA")
        img.paste(ri.convert("RGB"),(0,0),ri.split()[3])
        circle=clip_circle(art,cur_r)
        img.paste(circle.convert("RGB"),(0,0),circle.split()[3])
    else:
        angle=math.degrees(OMEGA*(frame-SQUEEZE)/FPS)
        rot=vinyl_rgba.rotate(-angle,resample=Image.BICUBIC)
        img.paste(rot.convert("RGB"),(0,0),rot.split()[3])
    return np.array(img)

# ── Step 1: Render intro (Python) ─────────────────────────────────────────────
def step1_render_intro(art_path, text, out_path):
    print("\n[1/6] Рендерю интро (Python, 200 кадров)...")
    t0 = time.time()
    font=ImageFont.truetype(FONT_PATH,FONT_SZ)
    art_pan=load_art(art_path,extra=PAN)
    art=load_art(art_path)
    lb,lg=make_logo((0,0,0)),make_logo(GLOW_COL)
    lc=make_letter_cache(text,font); lpos=letter_pos(text,lc)
    vinyl_rgba=make_vinyl_rgba(art); vinyl_ring=make_vinyl_ring()

    ff=subprocess.Popen(
        ["ffmpeg","-y","-f","rawvideo","-vcodec","rawvideo",
         "-pix_fmt","rgb24","-s",f"{W}x{H}","-r",str(FPS),"-i","pipe:0",
         "-c:v","libx264","-crf","22","-preset","ultrafast",
         "-pix_fmt","yuv420p","-movflags","+faststart",str(out_path)],
        stdin=subprocess.PIPE)

    PHASE=100
    for i in range(INTRO_F):
        if i%50==0: print(f"    кадр {i}/{INTRO_F}")
        if   i<PHASE:  arr=render_p1(i,art_pan,lb,lg,lc,lpos,text)
        elif i<PHASE*2: arr=render_p2(i-PHASE,art_pan,lb,lg,lc,lpos,text)
        ff.stdin.write(arr.tobytes())
    ff.stdin.close(); ff.wait()
    print(f"    → {out_path} ({time.time()-t0:.1f}с)")
    return vinyl_rgba, vinyl_ring, art, art_pan

# ── Step 2: Render vinyl spin (Python cycle in-memory → stdin pipe) ──────────
def step2_render_spin(vinyl_rgba, art_pan, spin_duration, out_path, tmp_dir):
    total_frames = round(spin_duration * FPS)
    cycle_f      = round(FPS * 60 / 16.67)  # 108 кадров = 1 оборот
    print(f"\n[2/6] Пластинка {spin_duration:.1f}с ({total_frames} кадров, цикл={cycle_f})...")
    t0 = time.time()

    # Pre-compute 108 ротаций в памяти — один раз, без промежуточного файла
    t_bake = time.time()
    cycle = []
    for i in range(cycle_f):
        angle = -(i / cycle_f) * 360
        cycle.append(np.asarray(vinyl_rgba.rotate(angle, resample=Image.BICUBIC, expand=False)).tobytes())
    print(f"    цикл запечён за {time.time()-t_bake:.1f}с")

    art_pan_path = tmp_dir / "art_pan.png"
    art_pan.save(str(art_pan_path), "PNG")

    half      = spin_duration / 2
    pan_expr  = f"'{PAN//2}*(1-cos(t*3.14159/{half:.1f}))'"
    darken_eq = "eq=brightness=-0.22:saturation=0.85"

    # Фон из файла (вход 0), пластинка из stdin (вход 1)
    ff = subprocess.Popen([
        "ffmpeg","-y",
        "-r",str(FPS),"-loop","1","-i",str(art_pan_path),
        "-f","rawvideo","-vcodec","rawvideo","-pix_fmt","rgba",
        "-s",f"{W}x{H}","-r",str(FPS),"-i","pipe:0",
        "-filter_complex",
        f"[0:v]scale=-1:{H}[sc];"
        f"[sc]crop={W}:{H}:{pan_expr}:0,"
        f"{darken_eq}[bg];"
        f"[bg][1:v]overlay=0:0:format=auto[out]",
        "-map","[out]",
        "-t",str(spin_duration),
        "-r",str(FPS),
        "-c:v","libx264","-crf","22","-preset","ultrafast",
        "-pix_fmt","yuv420p","-movflags","+faststart",
        str(out_path)
    ], stdin=subprocess.PIPE)

    for i in range(total_frames):
        if i % 900 == 0: print(f"    кадр {i}/{total_frames}")
        ff.stdin.write(cycle[i % cycle_f])
    ff.stdin.close()
    ff.wait()
    print(f"    → {out_path} ({time.time()-t0:.1f}с)")

# ── Step 3: Concat ────────────────────────────────────────────────────────────
def step3_concat(intro, spin, out_path, tmp_dir):
    print("\n[3/6] Склеиваю интро + пластинка...")
    t0 = time.time()
    list_file = tmp_dir / "concat.txt"
    list_file.write_text(f"file '{intro.resolve()}'\nfile '{spin.resolve()}'\n")
    run(["ffmpeg","-y","-f","concat","-safe","0","-i",str(list_file),
         "-c","copy",str(out_path)])
    print(f"    → {out_path} ({time.time()-t0:.1f}с)")

# ── Steps 4-6: Blend + глитч + аудио + fade (один проход) ────────────────────
def step4_6_final(concat_path, art_path, audio_path, total_duration, out_path):
    print("\n[4-6/6] Blend + глитч + аудио + fade (один проход)...")
    t0 = time.time()
    fade_start = total_duration - FADE_SEC
    run([
        "ffmpeg","-y",
        "-i",str(concat_path),
        "-r",str(FPS),"-loop","1","-i",art_path,
        "-i",audio_path,
        "-filter_complex",
        f"[1:v]scale=-1:{H}[s];"
        f"[s]crop={W}:{H}:'(iw-{W})*t/{total_duration:.1f}':0[moving];"
        f"[0:v][moving]blend=all_mode=screen:all_opacity=0.28[blended];"
        f"[blended]rgbashift=rh=-3:bh=3,"
        f"drawgrid=x=0:y=0:w=0:h=2:t=1:color=black@0.18,"
        f"fade=t=out:st={fade_start:.2f}:d={FADE_SEC}[out]",
        "-map","[out]","-map","2:a",
        "-t",str(total_duration),
        "-c:v","libx264","-crf","22","-preset","fast",
        "-c:a","aac","-b:a","160k",
        "-pix_fmt","yuv420p","-movflags","+faststart",
        str(out_path)
    ])
    print(f"    → {out_path} ({time.time()-t0:.1f}с)")

# ── Main ──────────────────────────────────────────────────────────────────────
def get_duration(path):
    r = subprocess.run(
        ["ffprobe","-v","quiet","-show_entries","format=duration",
         "-of","csv=p=0",str(path)],
        capture_output=True, text=True)
    return float(r.stdout.strip())

def render(art_path, audio_path, text, out_path):
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    out_path = Path(out_path)

    audio_dur  = get_duration(audio_path)
    intro_dur  = INTRO_F / FPS          # 6.67s
    spin_dur   = audio_dur - intro_dur
    total_dur  = audio_dur

    print(f"\n=== FULL RENDER ===")
    print(f"Трек: {audio_dur:.1f}с | Интро: {intro_dur:.1f}с | Пластинка: {spin_dur:.1f}с")
    print(f"Текст: '{text}' | Арт: {art_path}")

    t_total = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        intro_mp4  = tmp / "intro.mp4"
        spin_mp4   = tmp / "spin.mp4"
        concat_mp4 = tmp / "concat.mp4"

        vinyl_rgba, vinyl_ring, art, art_pan = \
            step1_render_intro(art_path, text, intro_mp4)

        step2_render_spin(vinyl_rgba, art_pan, spin_dur, spin_mp4, tmp)
        step3_concat(intro_mp4, spin_mp4, concat_mp4, tmp)
        step4_6_final(concat_mp4, art_path, audio_path, total_dur, out_path)

    elapsed = time.time() - t_total
    print(f"\n✓ Готово за {elapsed:.0f}с: {out_path}")
    size = Path(out_path).stat().st_size // (1024*1024)
    print(f"  Размер: {size} MB | Длина: {total_dur:.1f}с")

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--art",    required=True)
    p.add_argument("--audio",  required=True)
    p.add_argument("--text",   default="yaromat - sun")
    p.add_argument("--output", default="Instrument/FFmpeg/outputs/full_render.mp4")
    a = p.parse_args()
    render(a.art, a.audio, a.text, a.output)
