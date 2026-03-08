"""
Flappy Bird — Python + pygame  (з голосовим керуванням!)
Встановіть залежності: pip install pygame
Запуск: python flappy_bird.py

Керування:
  UP / SPACE        — летіти вгору
  🎙  КРИК/ХЛОПОК  — летіти вгору (голосове керування)
  Q                 — Settings panel
  ESC               — головне меню

Голосове керування:
  Використовує мікрофон — кричи, хлопай, свищи, дуй!
  Потрібен: sounddevice  (pip install sounddevice)
         або pyaudio     (pip install pyaudio)
         або ffmpeg      (системний — зазвичай вже є)
  Якщо нічого не знайдено — лише клавіатура.
"""

import pygame, sys, math, random, struct, threading, time, queue
pygame.init()
pygame.mixer.init(frequency=44100, size=-16, channels=1, buffer=512)

W, H        = 400, 620
FPS         = 60
BIRD_X      = 90
BIRD_SIZE   = 28
GRAVITY     = 0.32
JUMP_FORCE  = -6.5
PIPE_W      = 52
PIPE_GAP    = 155
PIPE_SPAWN  = 110
MILESTONE   = 100
NIGHT_SKY   = ((5,5,20),(10,20,40))

screen = pygame.display.set_mode((W, H), pygame.RESIZABLE)
pygame.display.set_caption("Flappy Bird mic")
clock  = pygame.time.Clock()

def on_resize(new_w, new_h):
    global W, H, BIRD_X, screen, stars, _bg_cache
    W      = max(280, new_w)
    H      = max(400, new_h)
    BIRD_X = max(60, W // 4)
    screen = pygame.display.set_mode((W, H), pygame.RESIZABLE)
    _bg_cache.clear()
    stars[:] = [
        {"x": random.uniform(0, W), "y": random.uniform(0, H - 80),
         "r": random.uniform(0.4, 1.8), "speed": random.uniform(0.08, 0.35)}
        for _ in range(100)
    ]

font_big   = pygame.font.SysFont("couriernew", 36, bold=True)
font_med   = pygame.font.SysFont("couriernew", 17, bold=True)
font_small = pygame.font.SysFont("couriernew", 13)
font_tiny  = pygame.font.SysFont("couriernew", 11)

class VoiceController:
    SAMPLE_RATE  = 16000
    CHUNK_SIZE   = 512        # ~32 ms
    COOLDOWN_S   = 0.25       # min seconds between jumps
    CALIB_CHUNKS = 40         # ~1.3 s calibration window

    def __init__(self):
        self.jump_queue   = queue.Queue()
        self.enabled      = False
        self.backend      = None
        self.threshold    = 800.0
        self.volume_level = 0.0   # 0-1 for display
        self._stop        = threading.Event()
        self._thread      = None
        self._last_jump   = 0.0
        self._calib_done  = False
        self._status_msg  = "ініціалізація…"
        # sensitivity multiplier (set via settings)
        self.sensitivity  = 1.0

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def poll_jump(self):
        try:
            self.jump_queue.get_nowait()
            return True
        except queue.Empty:
            return False

    @property
    def status(self):
        return self._status_msg

    def _run(self):
        for name, fn in [("sounddevice", self._run_sounddevice),
                          ("pyaudio",    self._run_pyaudio),
                          ("ffmpeg",     self._run_ffmpeg)]:
            try:
                self.backend = name
                self._status_msg = f"спроба {name}…"
                fn()
                return
            except Exception as e:
                self._status_msg = f"{name}: {e}"
        self.enabled = False
        self.backend = None
        self._status_msg = "мікрофон недоступний (лише клавіатура)"

    def _run_sounddevice(self):
        import sounddevice as sd
        import numpy as np
        calib = []

        def cb(indata, frames, t, status):
            rms = float(np.sqrt(np.mean(indata[:, 0] ** 2))) * 32768
            calib.append(rms)
            self.volume_level = min(1.0, rms / max(1, self.threshold * 3))
            if self._calib_done and rms > self.threshold * (1.0 / self.sensitivity):
                self._maybe_jump()

        with sd.InputStream(samplerate=self.SAMPLE_RATE, channels=1,
                             blocksize=self.CHUNK_SIZE, callback=cb):
            self.enabled = True
            self._status_msg = "калібрування…"
            while len(calib) < self.CALIB_CHUNKS and not self._stop.is_set():
                time.sleep(0.05)
            if calib:
                self.threshold = max(200.0, (sum(calib)/len(calib)) * 5.0)
            self._calib_done = True
            self._status_msg = "🎙 голос активний"
            while not self._stop.is_set():
                time.sleep(0.05)

    def _run_pyaudio(self):
        import pyaudio
        pa = pyaudio.PyAudio()
        st = pa.open(format=pyaudio.paInt16, channels=1, rate=self.SAMPLE_RATE,
                     input=True, frames_per_buffer=self.CHUNK_SIZE)
        self.enabled = True
        self._status_msg = "калібрування…"
        calib = []
        try:
            while not self._stop.is_set():
                raw = st.read(self.CHUNK_SIZE, exception_on_overflow=False)
                rms = self._rms16(raw)
                if not self._calib_done:
                    calib.append(rms)
                    if len(calib) >= self.CALIB_CHUNKS:
                        self.threshold = max(200.0, (sum(calib)/len(calib)) * 5.0)
                        self._calib_done = True
                        self._status_msg = "🎙 голос активний"
                else:
                    self.volume_level = min(1.0, rms / max(1, self.threshold * 3))
                    if rms > self.threshold * (1.0 / self.sensitivity):
                        self._maybe_jump()
        finally:
            st.stop_stream(); st.close(); pa.terminate()

    def _run_ffmpeg(self):
        import subprocess, shutil
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg not found")
        import sys as _sys
        sources = (
            [("alsa","default"),("pulse","default")] if _sys.platform=="linux"
            else [("avfoundation",":0")] if _sys.platform=="darwin"
            else [("dshow","audio=Microphone")]
        )
        proc = None
        for fmt, dev in sources:
            try:
                cmd = ["ffmpeg","-hide_banner","-loglevel","error",
                       "-f",fmt,"-i",dev,
                       "-ar",str(self.SAMPLE_RATE),"-ac","1",
                       "-f","s16le","-"]
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL,
                                     bufsize=self.CHUNK_SIZE*2)
                chunk = self.CHUNK_SIZE * 2
                test = p.stdout.read(chunk)
                if test and len(test) == chunk:
                    proc = p; break
                p.kill()
            except Exception:
                pass
        if not proc:
            raise RuntimeError("no ffmpeg audio source")
        self.enabled = True
        self._status_msg = "калібрування (ffmpeg)…"
        calib = []
        try:
            while not self._stop.is_set():
                raw = proc.stdout.read(self.CHUNK_SIZE * 2)
                if not raw or len(raw) < self.CHUNK_SIZE * 2:
                    break
                rms = self._rms16(raw)
                if not self._calib_done:
                    calib.append(rms)
                    if len(calib) >= self.CALIB_CHUNKS:
                        self.threshold = max(200.0, (sum(calib)/len(calib)) * 5.0)
                        self._calib_done = True
                        self._status_msg = "🎙 голос активний"
                else:
                    self.volume_level = min(1.0, rms / max(1, self.threshold * 3))
                    if rms > self.threshold * (1.0 / self.sensitivity):
                        self._maybe_jump()
        finally:
            proc.kill()

    @staticmethod
    def _rms16(raw: bytes) -> float:
        n = len(raw) // 2
        if n == 0: return 0.0
        total = sum(struct.unpack_from("<h", raw, i*2)[0]**2 for i in range(n))
        return math.sqrt(total / n)

    def _maybe_jump(self):
        now = time.time()
        if now - self._last_jump >= self.COOLDOWN_S:
            self._last_jump = now
            self.jump_queue.put(1)


def make_sound(freq, ms, waveform="sine", vol=0.28):
    sr = 44100; n = int(sr * ms / 1000); buf = bytearray(n * 2)
    for i in range(n):
        t = i / sr
        v = (math.sin(2*math.pi*freq*t) if waveform=="sine"
             else (1.0 if math.sin(2*math.pi*freq*t)>0 else -1.0) if waveform=="square"
             else random.uniform(-1,1))
        fade = min(1.0, (n-i)/max(1, sr*0.03))
        struct.pack_into("<h", buf, i*2, int(max(-32768, min(32767, v*vol*fade*32767))))
    return pygame.mixer.Sound(buffer=bytes(buf))

SFX = {
    "jump":      make_sound(520,  80, "sine",   0.22),
    "score":     make_sound(880, 100, "sine",   0.28),
    "die":       make_sound(120, 280, "square", 0.35),
    "click":     make_sound(660,  40, "sine",   0.18),
    "open":      make_sound(440,  55, "sine",   0.14),
    "milestone": make_sound(1046, 600, "sine",  0.30),
}
sound_on = True

def play(name):
    if sound_on:
        s = SFX.get(name)
        if s: s.play()


PHASE_FRAMES = 1200
SKYBOX = [
    ("dawn",    ( 30, 15, 60),(180, 90, 40),(255,200,120), 80),
    ("morning", ( 80,160,220),(200,220,255),(255,240,180),  0),
    ("noon",    ( 30,120,210),(135,195,255),(255,255,200),  0),
    ("evening", ( 50, 25, 80),(220,100, 40),(255,140, 60), 40),
    ("dusk",    ( 20, 10, 50),(100, 40, 80),(255,100, 80),120),
    ("night",   (  5,  5, 20),( 10, 20, 40),(220,220,255),255),
]

def lerp_col(a, b, t):
    return tuple(int(a[i]+(b[i]-a[i])*t) for i in range(3))

def get_sky(gf):
    idx = (gf//PHASE_FRAMES) % len(SKYBOX)
    nxt = (idx+1) % len(SKYBOX)
    t   = (gf%PHASE_FRAMES) / PHASE_FRAMES
    a,b = SKYBOX[idx], SKYBOX[nxt]
    return (lerp_col(a[1],b[1],t), lerp_col(a[2],b[2],t),
            lerp_col(a[3],b[3],t), int(a[4]+(b[4]-a[4])*t), a[0])

_bg_cache = {}

def draw_background(surface, gf, moving):
    sky_top,sky_bot,sun_col,stars_alpha,phase = get_sky(gf)
    key = (sky_top, sky_bot)
    if key not in _bg_cache:
        s = pygame.Surface((W, H-40))
        for row in range(H-40):
            pygame.draw.line(s, lerp_col(sky_top,sky_bot,row/(H-40)), (0,row),(W,row))
        _bg_cache.clear(); _bg_cache[key] = s
    surface.blit(_bg_cache[key],(0,0))
    sx,sy = int(W*0.76), int(H*0.17+math.sin(gf*0.0008)*H*0.06)
    is_night = (gf//PHASE_FRAMES)%len(SKYBOX) in (4,5)
    if not is_night:
        for r in (30,21,14):
            g=pygame.Surface((r*2,r*2),pygame.SRCALPHA)
            pygame.draw.circle(g,(*sun_col,max(0,200-(30-r)*18)),(r,r),r)
            surface.blit(g,(sx-r,sy-r))
    else:
        pygame.draw.circle(surface,(220,220,200),(sx,sy),14)
        pygame.draw.circle(surface,(160,160,140),(sx+4,sy-3),10)
    for s in stars:
        if moving:
            s["x"] -= s["speed"]
            if s["x"]<0: s["x"]=W
        if stars_alpha>30:
            tw=max(40,min(255,int(stars_alpha*(0.7+0.3*math.sin(gf*0.04+s["x"])))))
            pygame.draw.circle(surface,(tw,tw,min(255,tw+20)),(int(s["x"]),int(s["y"])),int(s["r"]))
    fc=(max(10,sky_bot[0]//3),max(15,sky_bot[1]//2),max(10,sky_bot[2]//4))
    fb=lerp_col(fc,(fc[0]+20,fc[1]+50,fc[2]+20),0.5)
    for row in range(40):
        pygame.draw.line(surface,lerp_col(fb,fc,row/40),(0,H-40+row),(W,H-40+row))
    lc={"dawn":(255,160,80),"morning":(100,255,180),"noon":(0,255,136),
        "evening":(255,120,60),"dusk":(200,80,180),"night":(80,120,255)}.get(phase,(0,255,136))
    pygame.draw.line(surface,lc,(0,H-40),(W,H-40),2)
    surface.blit(font_tiny.render(phase.upper(),True,lc),(8,8))

stars = [{"x":random.uniform(0,W),"y":random.uniform(0,H-80),
          "r":random.uniform(0.4,1.8),"speed":random.uniform(0.08,0.35)} for _ in range(100)]

def draw_mic_indicator(surface, voice: VoiceController, gf):
    if not voice.enabled: return
    x, y = W-80, H-18
    bar_w = 60; bar_h = 6
    pulse = 0.7 + 0.3*math.sin(gf*0.2)
    mic_col = (0,255,136) if voice._calib_done else (255,200,0)
    pygame.draw.circle(surface, mic_col, (x, y-1), int(4*pulse))
    pygame.draw.rect(surface,(30,30,60),(x+8,y-3,bar_w,bar_h))
    filled = int(voice.volume_level * bar_w)
    if filled > 0:
        bar_col = lerp_col((0,255,100),(255,60,60), min(1.0, voice.volume_level*1.5))
        pygame.draw.rect(surface, bar_col, (x+8,y-3,filled,bar_h))
    # threshold marker at 33%
    pygame.draw.line(surface,(255,255,255),(x+8+bar_w//3,y-4),(x+8+bar_w//3,y+3),1)

def draw_voice_status(surface, voice: VoiceController, gf):
    col = (255,200,0) if not voice._calib_done else (0,220,120)
    t = font_tiny.render(f"mic: {voice.status}", True, col)
    surface.blit(t,(W//2-t.get_width()//2, H-35))


class Bird:
    def __init__(self):
        self.y=H/2; self.vy=0.0; self.wing_tick=0
    def jump(self): self.vy=JUMP_FORCE; play("jump")
    def update(self): self.vy+=GRAVITY; self.y+=self.vy; self.wing_tick+=1
    def get_rect(self):
        m=3; s=BIRD_SIZE-m*2
        return pygame.Rect(BIRD_X-s//2,int(self.y)-s//2,s,s)
    def draw(self,surface):
        cx,cy,r=BIRD_X,int(self.y),BIRD_SIZE//2
        wo=int(math.sin(self.wing_tick*0.28)*5)
        pygame.draw.ellipse(surface,(255,153,0),pygame.Rect(cx-r+2,cy+wo-4,r-2,9))
        pygame.draw.circle(surface,(255,215,0),(cx,cy),r)
        pygame.draw.ellipse(surface,(255,229,92),pygame.Rect(cx-4,cy+1,r-2,r-4))
        pygame.draw.polygon(surface,(255,102,0),[(cx+r-2,cy-3),(cx+r+9,cy),(cx+r-2,cy+3)])
        pygame.draw.circle(surface,(255,255,255),(cx+6,cy-5),5)
        pygame.draw.circle(surface,(0,0,0),(cx+7,cy-5),2)


class Pipe:
    def __init__(self, gap_override=None):
        g=gap_override if gap_override is not None else PIPE_GAP
        self.gap_y=random.randint(80,H-g-80); self.gap=g
        self.x=W+PIPE_W; self.passed=False
    def update(self,speed): self.x-=speed
    def offscreen(self): return self.x+PIPE_W<0
    def get_rects(self):
        e=5
        return (pygame.Rect(self.x-e,0,PIPE_W+e*2,self.gap_y),
                pygame.Rect(self.x-e,self.gap_y+self.gap,PIPE_W+e*2,H-(self.gap_y+self.gap)-40))
    def draw(self,surface,phase):
        tints={"dawn":(180,220,160),"morning":(100,220,80),"noon":(60,200,60),
               "evening":(160,180,80),"dusk":(120,160,120),"night":(40,130,80)}
        base=tints.get(phase,(60,200,60))
        dark=tuple(max(0,c-60) for c in base); shine=tuple(min(255,c+80) for c in base)
        ch,cw,cx2=18,PIPE_W+10,self.x-5
        th=self.gap_y-ch
        if th>0:
            pygame.draw.rect(surface,dark,(self.x,0,PIPE_W,th))
            pygame.draw.rect(surface,shine,(self.x+4,0,6,th))
        pygame.draw.rect(surface,base,(cx2,self.gap_y-ch,cw,ch))
        pygame.draw.rect(surface,shine,(cx2+2,self.gap_y-ch+3,6,ch-6))
        by=self.gap_y+self.gap; bh=H-by-ch-40
        pygame.draw.rect(surface,base,(cx2,by,cw,ch))
        pygame.draw.rect(surface,shine,(cx2+2,by+3,6,ch-6))
        if bh>0:
            pygame.draw.rect(surface,dark,(self.x,by+ch,PIPE_W,bh))
            pygame.draw.rect(surface,shine,(self.x+4,by+ch,6,bh))


def get_difficulty(score, base_speed):
    tier=score//MILESTONE
    return base_speed+min(tier*0.4,4.0), max(110,PIPE_GAP-min(tier*5,40))


class MilestoneEffect:
    DURATION=130
    def __init__(self): self.active=False; self.frame=0; self.value=0
    def trigger(self,score): self.active=True; self.frame=0; self.value=score; play("milestone")
    def update(self):
        if self.active:
            self.frame+=1
            if self.frame>=self.DURATION: self.active=False
    def draw(self,surface):
        if not self.active: return
        t=self.frame/self.DURATION
        alpha=int((t/0.15*255) if t<0.15 else 255 if t<0.7 else ((1-(t-0.7)/0.3)*255))
        alpha=max(0,min(255,alpha))
        bw,bh=300,100; bx,by=W//2-bw//2,H//2-bh//2-40
        box=pygame.Surface((bw,bh),pygame.SRCALPHA); box.fill((5,0,30,min(210,alpha)))
        surface.blit(box,(bx,by))
        pulse=math.sin(self.frame*0.2)*0.5+0.5
        pygame.draw.rect(surface,lerp_col((255,200,0),(200,80,255),pulse),(bx,by,bw,bh),2)
        tier=self.value//MILESTONE
        label={1:"★ МАЙСТЕР ★",2:"★★ ЛЕГЕНДА ★★",3:"★★★ БОГ ★★★"}.get(
            tier,f"{'★'*min(tier,5)} НЕЙМОВІРНО {'★'*min(tier,5)}")
        t1=font_big.render(f"{self.value} ОЧОК!",True,lerp_col((255,200,0),(255,80,200),pulse))
        t1.set_alpha(alpha); surface.blit(t1,(W//2-t1.get_width()//2,by+10))
        t2=font_med.render(label,True,(200,200,255))
        t2.set_alpha(alpha); surface.blit(t2,(W//2-t2.get_width()//2,by+58))


class AdminPanel:
    PW,PH=310,400; ROW_H=44
    def __init__(self):
        self.visible=False; self.sel=0
        self.rows=[
            {"type":"slider","label":"ШВИДКІСТЬ",  "val":3.0,"min":0.5,"max":10.0,"step":0.5},
            {"type":"toggle","label":"ГОЛОС",       "key":"voice"},
            {"type":"slider","label":"ЧУТЛИВІСТЬ",  "val":1.0,"min":0.3,"max":3.0,"step":0.1},
            {"type":"button","label":"+1  ОЧКО",   "action":"pts","amt":1},
            {"type":"button","label":"+5  ОЧОК",   "action":"pts","amt":5},
            {"type":"button","label":"+10 ОЧОК",   "action":"pts","amt":10},
            {"type":"toggle","label":"ЗВУК",        "key":"sound"},
            {"type":"button","label":"ЗАКРИТИ [Q]","action":"close"},
        ]
    @property
    def PX(self): return W//2-self.PW//2
    @property
    def PY(self): return H//2-self.PH//2
    def open(self):   self.visible=True;  play("open")
    def close(self):  self.visible=False; play("click")
    def toggle(self):
        if self.visible: self.close()
        else:            self.open()
    def get_speed(self):       return self.rows[0]["val"]
    def get_sensitivity(self): return self.rows[2]["val"]

    def handle_key(self,key,ctx):
        if not self.visible: return
        if   key==pygame.K_UP:    self.sel=(self.sel-1)%len(self.rows)
        elif key==pygame.K_DOWN:  self.sel=(self.sel+1)%len(self.rows)
        elif key in(pygame.K_RETURN,pygame.K_SPACE): self._activate(self.sel,ctx)
        elif key==pygame.K_LEFT:  self._slide(self.sel,-1,ctx)
        elif key==pygame.K_RIGHT: self._slide(self.sel,+1,ctx)
        elif key in(pygame.K_q,pygame.K_ESCAPE): self.close()

    def handle_click(self,pos,ctx):
        if not self.visible: return
        mx,my=pos
        for i,row in enumerate(self.rows):
            ry=self.PY+68+i*self.ROW_H
            if self.PX<=mx<=self.PX+self.PW and ry<=my<=ry+self.ROW_H-4:
                self.sel=i
                if row["type"]=="slider": self._slide(i,-1 if mx<self.PX+self.PW//2 else+1,ctx)
                else: self._activate(i,ctx)

    def _activate(self,i,ctx):
        row=self.rows[i]; play("click")
        if row["type"]=="button":
            if row["action"]=="close": self.close()
            elif row["action"]=="pts":
                ctx["score"]+=row["amt"]
                if ctx["score"]>ctx["best"]: ctx["best"]=ctx["score"]
        elif row["type"]=="toggle":
            if row["key"]=="sound":
                global sound_on; sound_on=not sound_on
            elif row["key"]=="voice":
                ctx["voice_on"]=not ctx.get("voice_on",True)

    def _slide(self,i,d,ctx):
        row=self.rows[i]
        if row["type"]=="slider":
            row["val"]=round(max(row["min"],min(row["max"],row["val"]+d*row["step"])),2)
            if row["label"]=="ШВИДКІСТЬ": ctx["pipe_speed"]=row["val"]
            elif row["label"]=="ЧУТЛИВІСТЬ":
                vc=ctx.get("voice_ctrl")
                if vc: vc.sensitivity=row["val"]
            play("click")

    def draw(self,surface,ctx):
        if not self.visible: return
        ov=pygame.Surface((W,H),pygame.SRCALPHA); ov.fill((0,0,0,145)); surface.blit(ov,(0,0))
        pan=pygame.Surface((self.PW,self.PH),pygame.SRCALPHA); pan.fill((8,4,24,248))
        surface.blit(pan,(self.PX,self.PY))
        pygame.draw.rect(surface,(255,200,0),(self.PX,self.PY,self.PW,self.PH),2)
        for dx,dy in [(0,0),(self.PW-8,0),(0,self.PH-8),(self.PW-8,self.PH-8)]:
            pygame.draw.rect(surface,(255,230,80),(self.PX+dx,self.PY+dy,8,8),2)
        t=font_med.render("НАЛАШТУВАННЯ",True,(255,200,0))
        surface.blit(t,(self.PX+self.PW//2-t.get_width()//2,self.PY+10))
        pygame.draw.line(surface,(255,200,0),(self.PX+8,self.PY+36),(self.PX+self.PW-8,self.PY+36),1)
        sc=font_small.render(f"РАХУНОК: {ctx['score']}   РЕКОРД: {ctx['best']}",True,(160,160,220))
        surface.blit(sc,(self.PX+self.PW//2-sc.get_width()//2,self.PY+44))
        for i,row in enumerate(self.rows):
            ry=self.PY+68+i*self.ROW_H; rx=self.PX+10; rw=self.PW-20; sel=(i==self.sel)
            rb=pygame.Surface((rw,self.ROW_H-5),pygame.SRCALPHA)
            rb.fill((255,200,0,40) if sel else (255,255,255,8)); surface.blit(rb,(rx,ry))
            if sel: pygame.draw.rect(surface,(255,200,0),(rx,ry,rw,self.ROW_H-5),1)
            lc=(255,200,0) if sel else (200,200,230)
            if row["type"]=="slider":
                surface.blit(font_small.render(row["label"],True,lc),(rx+8,ry+5))
                vt=font_med.render(f"{row['val']:.1f}",True,(0,255,200))
                surface.blit(vt,(rx+rw-vt.get_width()-8,ry+4))
                bx2=rx+8; bw2=rw-16; by2=ry+26; bh2=6
                pygame.draw.rect(surface,(40,40,80),(bx2,by2,bw2,bh2))
                filled=int((row["val"]-row["min"])/(row["max"]-row["min"])*bw2)
                pygame.draw.rect(surface,(0,255,200),(bx2,by2,filled,bh2))
                surface.blit(font_tiny.render("◄  ►",True,(100,100,140) if not sel else (255,200,0)),
                             (rx+rw//2-font_tiny.size("◄  ►")[0]//2,by2-1))
            elif row["type"]=="toggle":
                surface.blit(font_small.render(row["label"],True,lc),(rx+8,ry+12))
                if row["key"]=="sound":   state=sound_on
                elif row["key"]=="voice": state=ctx.get("voice_on",True)
                else: state=False
                surface.blit(font_med.render("ВКЛ" if state else "ВИКЛ",True,
                    (0,255,136) if state else (255,60,80)),(rx+rw-50,ry+10))
                # voice status line
                if row["key"]=="voice":
                    vc=ctx.get("voice_ctrl")
                    if vc:
                        st=font_tiny.render(vc.status,True,(120,120,180))
                        surface.blit(st,(rx+8,ry+28))
            else:
                lbl=font_small.render(row["label"],True,lc)
                surface.blit(lbl,(rx+rw//2-lbl.get_width()//2,ry+12))
        surface.blit(font_tiny.render("↑↓ вибір  ←→ змінити  ENTER підтвердити",True,(80,80,120)),
                     (self.PX+self.PW//2-font_tiny.size("↑↓ вибір  ←→ змінити  ENTER підтвердити")[0]//2,
                      self.PY+self.PH-18))


class MainMenu:
    def __init__(self): self.sel=0; self.items=["ПОЧАТИ ГРУ","ЗВУК: ВКЛ","ВИЙТИ"]
    def refresh(self): self.items[1]=f"ЗВУК: {'ВКЛ' if sound_on else 'ВИКЛ'}"
    def handle_key(self,key):
        if key==pygame.K_UP:    self.sel=(self.sel-1)%len(self.items); play("click")
        elif key==pygame.K_DOWN:self.sel=(self.sel+1)%len(self.items); play("click")
        elif key in(pygame.K_RETURN,pygame.K_SPACE): return self._activate()
        return None
    def _activate(self):
        global sound_on; play("click")
        if self.sel==0: return "start"
        elif self.sel==1: sound_on=not sound_on; self.refresh()
        elif self.sel==2: pygame.quit(); sys.exit()
        return None
    def draw(self,surface,gf,voice):
        self.refresh()
        g=int(180+60*math.sin(gf*0.06)); cx=W//2
        for off,col,sh in [((3,3),(110,0,110),(0,70,90)), ((0,0),(255,g//2,255),(0,g,200))]:
            t1=font_big.render("FLAPPY",True,col if off==(0,0) else sh)
            t2=font_big.render("BIRD",  True,col if off==(0,0) else (0,g,200) if off==(0,0) else sh)
            surface.blit(font_big.render("FLAPPY",True,sh if off==(3,3) else (255,g//2,255)),
                         (cx-font_big.size("FLAPPY")[0]//2+off[0],130+off[1]))
            surface.blit(font_big.render("BIRD",  True,(110,0,110) if off==(3,3) else (0,g,200)),
                         (cx-font_big.size("BIRD")[0]//2+off[0],174+off[1]))
        sub=font_tiny.render("ENTER / SPACE або КРИЧИ!",True,(140,140,180))
        surface.blit(sub,(cx-sub.get_width()//2,226))
        for i,item in enumerate(self.items):
            sel=(i==self.sel); pulse=int(220+35*math.sin(gf*0.08)) if sel else 180
            col=(255,pulse,0) if sel else (160,160,200)
            txt=font_med.render(("▶ " if sel else "  ")+item,True,col)
            y=280+i*46
            if sel:
                bg=pygame.Surface((txt.get_width()+20,32),pygame.SRCALPHA); bg.fill((255,200,0,22))
                surface.blit(bg,(cx-txt.get_width()//2-10,y-2))
            surface.blit(txt,(cx-txt.get_width()//2,y))
        surface.blit(font_tiny.render("Q = Налаштування   ESC = Меню",True,(70,70,100)),
                     (cx-font_tiny.size("Q = Налаштування   ESC = Меню")[0]//2,H-40))
        draw_voice_status(surface,voice,gf)


def draw_score(surface,score,best):
    tx=font_big.render(str(score),True,(255,255,255))
    x=W//2-tx.get_width()//2
    surface.blit(font_big.render(str(score),True,(100,60,160)),(x+2,16)); surface.blit(tx,(x,14))
    surface.blit(font_small.render(f"РЕКОРД: {best}",True,(168,85,247)),(W-font_small.size(f"РЕКОРД: {best}")[0]-10,14))
    tier=score//MILESTONE
    if tier>0:
        badge=font_small.render("★"*min(tier,5)+("+" if tier>5 else ""),True,(255,220,60))
        surface.blit(badge,(W//2-badge.get_width()//2,46))

def draw_gameover(surface,score,best):
    bw,bh=290,155; bx,by=W//2-bw//2,H//2-bh//2
    box=pygame.Surface((bw,bh),pygame.SRCALPHA); box.fill((20,8,45,218)); surface.blit(box,(bx,by))
    pygame.draw.rect(surface,(255,60,80),(bx,by,bw,bh),2)
    surface.blit(font_med.render("ГРА ЗАКІНЧЕНА",True,(255,60,80)),(W//2-font_med.size("ГРА ЗАКІНЧЕНА")[0]//2,by+12))
    for i,line in enumerate([f"РАХУНОК:  {score}",f"РЕКОРД:  {best}"]):
        lt=font_small.render(line,True,(255,255,255)); surface.blit(lt,(W//2-lt.get_width()//2,by+50+i*26))
    ht=font_tiny.render("SPACE/↑/КРИК — знову   ESC — меню",True,(120,120,160))
    surface.blit(ht,(W//2-ht.get_width()//2,by+bh-20))

def draw_idle(surface):
    bw,bh=310,255; bx,by=W//2-bw//2,H//2-bh//2+10
    box=pygame.Surface((bw,bh),pygame.SRCALPHA); box.fill((12,5,35,220)); surface.blit(box,(bx,by))
    pygame.draw.rect(surface,(255,107,255),(bx,by,bw,bh),2)
    for dx,dy in [(0,0),(bw-6,0),(0,bh-6),(bw-6,bh-6)]:
        pygame.draw.rect(surface,(255,107,255),(bx+dx,by+dy,6,6))
    surface.blit(font_med.render("ЯК ГРАТИ",True,(255,107,255)),(W//2-font_med.size("ЯК ГРАТИ")[0]//2,by+12))
    pygame.draw.line(surface,(255,107,255),(bx+10,by+36),(bx+bw-10,by+36),1)
    surface.blit(font_tiny.render("Лети крізь просвіти між стовпами!",True,(200,200,255)),
                 (W//2-font_tiny.size("Лети крізь просвіти між стовпами!")[0]//2,by+44))
    surface.blit(font_tiny.render("— КЕРУВАННЯ —",True,(255,200,0)),(W//2-font_tiny.size("— КЕРУВАННЯ —")[0]//2,by+64))
    for i,(key,desc) in enumerate([("SPACE / ↑","Летіти вгору"),
                                    ("🎙 КРИК",  "Летіти вгору (голос)"),
                                    ("Q",        "Налаштування"),
                                    ("ESC",      "Головне меню")]):
        ky=by+82+i*24
        surface.blit(font_tiny.render(key, True,(255,220,80)),(bx+20,ky))
        surface.blit(font_tiny.render(desc,True,(180,180,220)),(bx+135,ky))
    pygame.draw.line(surface,(80,80,120),(bx+10,by+bh-38),(bx+bw-10,by+bh-38),1)
    surface.blit(font_small.render("Натисни SPACE / ↑  або КРИЧИ!",True,(255,255,255)),
                 (W//2-font_small.size("Натисни SPACE / ↑  або КРИЧИ!")[0]//2,by+bh-28))

def run_menu(menu,gf_ref,voice):
    menu.sel=0
    while True:
        for event in pygame.event.get():
            if event.type==pygame.QUIT: pygame.quit(); sys.exit()
            if event.type==pygame.VIDEORESIZE: on_resize(event.w,event.h)
            if event.type==pygame.KEYDOWN:
                res=menu.handle_key(event.key)
                if res=="start": return gf_ref[0]
        if voice.poll_jump(): return gf_ref[0]
        gf_ref[0]+=1
        draw_background(screen,gf_ref[0],False)
        menu.draw(screen,gf_ref[0],voice)
        pygame.display.flip(); clock.tick(FPS)


def main():
    global sound_on

    voice = VoiceController()
    voice.start()

    best_score=0; first_game=True; gf=[0]
    menu=MainMenu(); admin=AdminPanel(); milestone=MilestoneEffect()

    run_menu(menu,gf,voice)

    while True:
        bird=Bird(); pipes=[]; score=0; prev_score=0; game_frame=0
        state="idle" if first_game else "playing"
        pipe_speed=admin.get_speed()
        ctx={"score":score,"best":best_score,"pipe_speed":pipe_speed,
             "voice_on":True,"voice_ctrl":voice}

        while True:
            need_restart=False; need_menu=False

            for event in pygame.event.get():
                if event.type==pygame.QUIT: voice.stop(); pygame.quit(); sys.exit()
                if event.type==pygame.VIDEORESIZE:
                    on_resize(event.w,event.h)
                    for p in pipes:
                        if p.x>W: p.x=W+PIPE_W
                if event.type==pygame.KEYDOWN:
                    if event.key==pygame.K_q: admin.toggle(); continue
                    if admin.visible:
                        admin.handle_key(event.key,ctx)
                        score=ctx["score"]; best_score=ctx["best"]; pipe_speed=ctx["pipe_speed"]
                        continue
                    if event.key==pygame.K_ESCAPE: need_menu=True; break
                    if event.key in(pygame.K_SPACE,pygame.K_UP):
                        if   state=="idle":    state="playing"
                        elif state=="playing": bird.jump()
                        elif state=="dead":    need_restart=True; break
                if event.type==pygame.MOUSEBUTTONDOWN and admin.visible:
                    admin.handle_click(event.pos,ctx)
                    score=ctx["score"]; best_score=ctx["best"]; pipe_speed=ctx["pipe_speed"]

            if ctx.get("voice_on",True) and voice.poll_jump():
                if   state=="idle":    state="playing"
                elif state=="playing": bird.jump()
                elif state=="dead":    need_restart=True

            if need_restart: first_game=False; break
            if need_menu: run_menu(menu,gf,voice); first_game=False; break

            gf[0]+=1; milestone.update()

            if state=="playing" and not admin.visible:
                game_frame+=1
                eff_speed,eff_gap=get_difficulty(score,ctx["pipe_speed"])
                bird.update()
                if game_frame%PIPE_SPAWN==1: pipes.append(Pipe(gap_override=eff_gap))
                for pipe in pipes:
                    pipe.update(eff_speed)
                    if not pipe.passed and pipe.x+PIPE_W<BIRD_X:
                        pipe.passed=True; prev_score=score; score+=1; ctx["score"]=score
                        if score>best_score: best_score=score; ctx["best"]=best_score
                        play("score")
                        if (score//MILESTONE)>(prev_score//MILESTONE): milestone.trigger(score)
                pipes=[p for p in pipes if not p.offscreen()]
                if bird.y-BIRD_SIZE//2<0 or bird.y+BIRD_SIZE//2>H-40:
                    state="dead"; play("die")
                else:
                    br=bird.get_rect()
                    for pipe in pipes:
                        tr,botr=pipe.get_rects()
                        if br.colliderect(tr) or br.colliderect(botr): state="dead"; play("die"); break

            _,_,_,_,phase=get_sky(gf[0])
            draw_background(screen,gf[0],state=="playing")
            for pipe in pipes: pipe.draw(screen,phase)
            bird.draw(screen)
            draw_score(screen,score,best_score)
            milestone.draw(screen)

            if state=="idle" and first_game: draw_idle(screen)
            elif state=="dead":              draw_gameover(screen,score,best_score)

            if ctx.get("voice_on",True):
                draw_mic_indicator(screen,voice,gf[0])
                if not voice._calib_done or state in("idle","dead"):
                    draw_voice_status(screen,voice,gf[0])

            admin.draw(screen,ctx)

            if not admin.visible:
                tier=score//MILESTONE
                if tier>0:
                    screen.blit(font_tiny.render(f"рівень {tier+1}  +{tier*0.4:.1f}швид",True,(120,80,200)),(8,20))
                screen.blit(font_tiny.render("[Q] налаштування",True,(60,60,95)),(8,H-18))

            pygame.display.flip(); clock.tick(FPS)


if __name__=="__main__":
    main()
