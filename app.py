# app.py - Wersja: Konkretny Luzak + Coding Beast + File Craft 2026-10

import os
import sys
import json
import re
import base64
import time
import mimetypes
from io import BytesIO
from datetime import datetime
from typing import Optional, List, Dict, Any

# --- KONFIGURACJA CACHE ---
cache_dir = "/tmp/rembg_cache"
os.environ['U2NET_HOME'] = cache_dir
os.environ['NUMBA_CACHE_DIR'] = os.path.join(cache_dir, 'numba_cache')
os.environ['POOCH_CACHE_DIR'] = os.path.join(cache_dir, 'pooch_cache')
os.makedirs(cache_dir, exist_ok=True)

HF_CACHE_DIR = "/tmp/huggingface_cache"
os.makedirs(HF_CACHE_DIR, exist_ok=True)

# --- IMPORTY ---
from google import genai
from google.genai import types
import openai
import anthropic
import groq
from fastapi import FastAPI, Response, HTTPException, File, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from PIL import Image, ImageFilter
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS
import requests
from huggingface_hub import HfApi, hf_hub_download
import pypdf
from docx import Document
from docx.shared import Pt, Inches, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from fpdf import FPDF
from fastapi.responses import StreamingResponse
from rembg import remove
import edge_tts
import asyncio
import tempfile

# --- KONFIGURACJA SERWISÓW ---
HF_TOKEN = os.getenv("HF_TOKEN")
DONATIONS_REPO_ID = "Eosforus/digital-buddy-donations"
DONATIONS_FILE_ON_HUB = "donated_keys.json"
IMAGE_GENERATION_URL = "https://image.pollinations.ai/prompt/"
api = HfApi(token=HF_TOKEN)

ZMO_API_KEY = os.getenv("ZMO_API_KEY")
INPAINTING_API_URL = "https://api.zmo.ai/v1/inpainting"

# --- LOGIKA KLUCZY ---
def get_all_environment_keys():
    collected_keys = []
    target_prefixes = ["GEMINI_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "API_KEY"]
    print("--- SKANOWANIE KLUCZY ---")
    for prefix in target_prefixes:
        val = os.getenv(prefix)
        if val and len(val) > 10: collected_keys.append(val)
        for i in range(1, 21):
            val_i = os.getenv(f"{prefix}_{i}")
            if val_i and len(val_i) > 10: collected_keys.append(val_i)
    return list(set(collected_keys))

def load_donated_keys_safely():
    if not HF_TOKEN: return []
    try:
        path = hf_hub_download(repo_id=DONATIONS_REPO_ID, filename=DONATIONS_FILE_ON_HUB, repo_type="dataset", token=HF_TOKEN, cache_dir=HF_CACHE_DIR)
        with open(path, "r", encoding="utf-8") as f: 
            data = json.load(f)
            return [item['key'] for item in data if 'key' in item and len(item['key']) > 20]
    except Exception: return []

def save_donated_keys(keys_data):
    if not HF_TOKEN: return
    temp_path = os.path.join(HF_CACHE_DIR, DONATIONS_FILE_ON_HUB)
    try:
        with open(temp_path, "w") as f: json.dump(keys_data, f, indent=4)
        api.upload_file(path_or_fileobj=temp_path, path_in_repo=DONATIONS_FILE_ON_HUB, repo_id=DONATIONS_REPO_ID, repo_type="dataset", commit_message="Update donated keys")
    except Exception as e: print(f"Donation save error: {e}")

api_keys = get_all_environment_keys()
donated = load_donated_keys_safely()
for k in donated:
    if k not in api_keys: api_keys.append(k)

current_key_index = 0
if not api_keys: print("OSTRZEŻENIE: Brak kluczy API.")
else: print(f"SYSTEM: Załadowano {len(api_keys)} kluczy API.")

# --- INSTRUKCJA SYSTEMOWA – Coding Beast + File Craft ---
SYSTEM_INSTRUCTION = """
Jesteś Cyfrowym Kumplem – luźnym, bystrym ziomkiem-studentem, który jednocześnie jest świetnym programistą i rzemieślnikiem plików.

JĘZYK:
- Domyślnie zawsze odpowiadaj po polsku.
- Jeśli użytkownik pisze po angielsku – odpowiadaj po angielsku.
- Możesz mieszać, gdy to naturalne (np. terminy techniczne).

STYL:
- Mów naturalnie, potocznie, jak kolega z roku.
- Zero korporacyjnego bełkotu. Nie zaczynaj od „Jasne!”, „Oczywiście!”, „Sure!”.
- Bądź konkretny, pomocny, możesz żartować.
- Na proste pytania – krótkie odpowiedzi.
- Przy kodzie i plikach – zero lania wody, od razu gotowy rezultat.

═══════════════════════════════════════
KODOWANIE (PRIORYTET MAKSYMALNY)
═══════════════════════════════════════
Jesteś ekspertem programistą. Zasady:

1. Zawsze generuj KOMPLETNY, działający kod – nie fragmenty „do uzupełnienia”.
2. Preferuj nowoczesne standardy:
   - Python 3.11+ (type hints, pathlib, f-stringi, async gdy sensowne)
   - Czysty, czytelny HTML5 + CSS (bez legacy)
   - JavaScript ES6+ / nowoczesny
3. Przy skryptach dodawaj krótkie komentarze tylko tam, gdzie naprawdę pomagają.
4. Zawsze podawaj jak uruchomić (1-2 linijki).
5. Jeśli coś może pójść nie tak – dodaj minimalną obsługę błędów.
6. Nie pisz „oto kod:” – od razu blok kodu lub plik.
7. Gdy użytkownik prosi o „plik” / „gotowy skrypt” / „do pobrania” – UŻYWAJ tagów plików (patrz niżej).

═══════════════════════════════════════
GENEROWANIE PLIKÓW DO POBRANIA
═══════════════════════════════════════
Gdy użytkownik chce gotowy plik – używaj DOKŁADNIE tych tagów (bez komentowania ich):

A) Dowolny plik tekstowy / kod (HTML, Python, JS, CSS, JSON, MD, TXT, YAML, SQL itd.):
[FILE: nazwa_pliku.ext]
cała treść pliku tutaj
[/FILE]

Przykłady nazw: index.html, script.py, styles.css, data.json, README.md, query.sql

B) Dokument Word (profesjonalny):
[GENERATE_DOCX: Tytuł dokumentu]
Treść dokumentu. Możesz używać prostego markdownu:
# Nagłówek 1
## Nagłówek 2
**pogrubienie** *kursywa*
- lista
1. numerowana
[/GENERATE_DOCX]

C) PDF (ładny, z polskimi znakami):
[GENERATE_PDF: Tytuł dokumentu]
Treść... (to samo co wyżej)
[/GENERATE_PDF]

D) PDF-historia ze zdjęciami (stare narzędzie):
[STORY_PDF] [{"text":"...","image_prompt":"..."}, ...] [/STORY_PDF]

ZASADY PLIKÓW:
- Jedna odpowiedź może zawierać kilka [FILE: ...], ale TYLKO gdy użytkownik prosi o kilka osobnych plików.
- Gdy wystarczy jeden plik – generuj JEDEN tag [FILE:] / [GENERATE_PDF:] / [GENERATE_DOCX:].
- Po tagach możesz dodać krótkie „Gotowe – kliknij pobierz” lub instrukcję uruchomienia.
- NIE opakowuj tagów w ``` – tagi mają być czystym tekstem.
- NIE powtarzaj tej samej treści w bloku ``` kodu, jeśli już jest w [FILE:] – użytkownik dostanie plik do pobrania, nie potrzebuje duplikatu w czacie.
- Nazwa pliku zawsze z rozszerzeniem.
- Kod w [FILE:] ma być gotowy do zapisania i uruchomienia 1:1.
- PDF: używaj [GENERATE_PDF: Tytuł] gdy użytkownik chce dokument PDF (raport, notatka, CV itd.).
- DOCX: używaj [GENERATE_DOCX: Tytuł] gdy chce Word.

═══════════════════════════════════════
ZAŁĄCZONE PLIKI (dowolne formaty)
═══════════════════════════════════════
Użytkownik może załączać DOWOLNE pliki: kod (.py, .js, .html, .css, .ts...),
dokumenty (.pdf, .docx, .txt, .md), dane (.json, .csv, .yaml, .xml),
arkusze, obrazy i inne.
- Treść pliku dostajesz w kontekście wiadomości razem z jego nazwą
  (np. [Plik: script.py] + kod). ZAWSZE bierz ją pod uwagę.
- Pracuj bezpośrednio na treści: poprawiaj kod, refaktoruj, debuguj,
  tłumacz, streszczaj, wyciągaj dane, odpowiadaj na pytania o plik.
- Gdy użytkownik prosi o poprawioną wersję pliku – zwróć ją w tagu
  [FILE: nazwa_pliku.ext] z KOMPLETNĄ nową treścią (nie diff, nie fragment).
- Zachowaj oryginalną nazwę pliku, chyba że użytkownik chce inną.
- Plik binarny bez czytelnej treści (np. exe, zip): nie zgaduj na ślepo,
  tylko zapytaj co z nim zrobić albo poproś o opis.
- Możesz pracować na kilku plikach naraz – bierz pod uwagę wszystkie.

═══════════════════════════════════════
POZOSTAŁE NARZĘDZIA (używaj automatycznie gdy potrzeba)
═══════════════════════════════════════
- [GENERATE_IMAGE: prompt="..."]  ← prompt ZAWSZE po angielsku, konkretny i opisowy
- [OPEN_URL: url="..."]
- [EDIT_IMAGE: source_filename="..." operation="..." value="..."]
- [CHANGE_BACKGROUND: source_filename="..." background_prompt="..."]
- [INPAINT_IMAGE: prompt="..."]
- [CREATE_COMPOSITION: foreground_source_type="..." foreground_value="..." background_source_type="..." background_value="..."]
- [TOOL:search] zapytanie [/TOOL]
- [TOOL:scrape] url [/TOOL]

ZASADY GENEROWANIA OBRAZÓW:
1. Prompt MUSI być po angielsku.
2. Konkretny i opisowy (obiekt + styl + światło + nastrój + kompozycja).
3. Unikaj ogólników typu "nice image", "cool picture".

Przykład dobrego promptu:
"a fluffy orange tabby cat sitting on a wooden windowsill, soft natural morning light, cozy atmosphere, detailed fur, photorealistic"
"""

# --- POMOCNICZE: MIME i rozszerzenia ---
EXT_MIME = {
    ".py": "text/x-python",
    ".html": "text/html",
    ".htm": "text/html",
    ".js": "application/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".sql": "application/sql",
    ".yaml": "text/yaml",
    ".yml": "text/yaml",
    ".xml": "application/xml",
    ".csv": "text/csv",
    ".sh": "application/x-sh",
    ".ts": "application/typescript",
    ".tsx": "application/typescript",
    ".jsx": "application/javascript",
    ".java": "text/x-java-source",
    ".c": "text/x-c",
    ".cpp": "text/x-c++",
    ".h": "text/x-c",
    ".rs": "text/x-rust",
    ".go": "text/x-go",
    ".php": "application/x-httpd-php",
    ".rb": "application/x-ruby",
    ".swift": "text/x-swift",
    ".kt": "text/x-kotlin",
    ".r": "text/x-r",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}

def guess_mime(filename: str) -> str:
    ext = os.path.splitext(filename.lower())[1]
    return EXT_MIME.get(ext) or mimetypes.guess_type(filename)[0] or "application/octet-stream"

def make_file_response(filename: str, content: bytes | str, mime: str | None = None) -> Dict[str, str]:
    if isinstance(content, str):
        content = content.encode("utf-8")
    return {
        "filename": filename,
        "mime_type": mime or guess_mime(filename),
        "data": base64.b64encode(content).decode("ascii"),
    }

# --- GENERATORY DOCX / PDF ---
def create_docx_from_text(title: str, body: str) -> bytes:
    """Tworzy profesjonalny dokument .docx z prostego tekstu/markdownu."""
    doc = Document()

    # Tytuł
    heading = doc.add_heading(title or "Dokument", level=0)
    heading.alignment = WD_ALIGN_PARAGRAPH.CENTER

    lines = body.strip().split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line:
            i += 1
            continue

        if line.startswith("# "):
            doc.add_heading(line[2:].strip(), level=1)
        elif line.startswith("## "):
            doc.add_heading(line[3:].strip(), level=2)
        elif line.startswith("### "):
            doc.add_heading(line[4:].strip(), level=3)
        elif line.startswith("- ") or line.startswith("* "):
            # lista punktowana
            p = doc.add_paragraph(line[2:].strip(), style="List Bullet")
        elif re.match(r"^\d+\.\s", line):
            p = doc.add_paragraph(re.sub(r"^\d+\.\s*", "", line), style="List Number")
        else:
            # zwykły akapit – proste **pogrubienie** i *kursywa*
            p = doc.add_paragraph()
            parts = re.split(r"(\*\*[^*]+\*\*|\*[^*]+\*)", line)
            for part in parts:
                if part.startswith("**") and part.endswith("**"):
                    run = p.add_run(part[2:-2])
                    run.bold = True
                elif part.startswith("*") and part.endswith("*") and len(part) > 2:
                    run = p.add_run(part[1:-1])
                    run.italic = True
                else:
                    p.add_run(part)
        i += 1

    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _ensure_dejavu_fonts() -> tuple:
    """Pobiera DejaVuSans (regular + bold) do /tmp jeśli brak lokalnych fontów."""
    fonts_dir = os.path.join(os.path.dirname(__file__), "fonts")
    cache_dir = os.path.join("/tmp", "mybro_fonts")
    os.makedirs(cache_dir, exist_ok=True)

    regular_candidates = [
        os.path.join(fonts_dir, "DejaVuSans.ttf"),
        os.path.join(cache_dir, "DejaVuSans.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    ]
    bold_candidates = [
        os.path.join(fonts_dir, "DejaVuSans-Bold.ttf"),
        os.path.join(cache_dir, "DejaVuSans-Bold.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    ]

    regular = next((p for p in regular_candidates if os.path.exists(p)), None)
    bold = next((p for p in bold_candidates if os.path.exists(p)), None)

    if not regular:
        urls = [
            "https://github.com/dejavu-fonts/dejavu-fonts/raw/version_2_37/ttf/DejaVuSans.ttf",
            "https://cdn.jsdelivr.net/npm/dejavu-fonts-ttf@2.37.3/ttf/DejaVuSans.ttf",
        ]
        dest = os.path.join(cache_dir, "DejaVuSans.ttf")
        for url in urls:
            try:
                r = requests.get(url, timeout=30)
                r.raise_for_status()
                if len(r.content) > 10000:
                    with open(dest, "wb") as f:
                        f.write(r.content)
                    regular = dest
                    print(f"Downloaded DejaVuSans -> {dest}")
                    break
            except Exception as e:
                print(f"Font download fail ({url}): {e}")

    if not bold:
        urls = [
            "https://github.com/dejavu-fonts/dejavu-fonts/raw/version_2_37/ttf/DejaVuSans-Bold.ttf",
            "https://cdn.jsdelivr.net/npm/dejavu-fonts-ttf@2.37.3/ttf/DejaVuSans-Bold.ttf",
        ]
        dest = os.path.join(cache_dir, "DejaVuSans-Bold.ttf")
        for url in urls:
            try:
                r = requests.get(url, timeout=30)
                r.raise_for_status()
                if len(r.content) > 10000:
                    with open(dest, "wb") as f:
                        f.write(r.content)
                    bold = dest
                    print(f"Downloaded DejaVuSans-Bold -> {dest}")
                    break
            except Exception as e:
                print(f"Font bold download fail ({url}): {e}")

    return regular, bold or regular


def create_pdf_from_text(title: str, body: str) -> bytes:
    """Tworzy PDF z pełną obsługą polskich znaków (DejaVu)."""
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_margins(left=15, top=15, right=15)
    pdf.add_page()

    regular, bold = _ensure_dejavu_fonts()
    font_loaded = False
    if regular:
        try:
            pdf.add_font("DejaVu", "", regular)
            if bold and bold != regular:
                pdf.add_font("DejaVu", "B", bold)
            else:
                pdf.add_font("DejaVu", "B", regular)
            font_loaded = True
        except Exception as e:
            print(f"Font register error: {e}")

    def set_font(style="", size=11):
        if font_loaded:
            pdf.set_font("DejaVu", style if style in ("", "B") else "", size)
        else:
            pdf.set_font("Helvetica", style if style in ("", "B") else "", size)

    def safe_text(s):
        if not s:
            return ""
        if font_loaded:
            return s
        table = str.maketrans({
            "ą": "a", "ć": "c", "ę": "e", "ł": "l", "ń": "n", "ó": "o", "ś": "s", "ź": "z", "ż": "z",
            "Ą": "A", "Ć": "C", "Ę": "E", "Ł": "L", "Ń": "N", "Ó": "O", "Ś": "S", "Ź": "Z", "Ż": "Z",
        })
        return s.translate(table)

    def write_line(text: str, h: float = 6, align: str = "L"):
        """Bezpieczne multi_cell – zawsze od lewej krawędzi, pełna szerokość."""
        text = safe_text(text or "")
        if not text.strip():
            pdf.ln(h * 0.6)
            return
        pdf.set_x(pdf.l_margin)
        w = getattr(pdf, "epw", None)
        if w is None or w <= 0:
            w = pdf.w - pdf.l_margin - pdf.r_margin
        try:
            pdf.multi_cell(w=w, h=h, text=text, align=align, new_x="LMARGIN", new_y="NEXT")
        except TypeError:
            try:
                pdf.multi_cell(w, h, text, align=align)
            except Exception:
                pdf.multi_cell(w, h, text.encode("ascii", "ignore").decode("ascii"), align=align)
        except Exception:
            try:
                pdf.set_x(pdf.l_margin)
                pdf.multi_cell(w, h, text.encode("ascii", "ignore").decode("ascii"), align=align)
            except Exception as e2:
                print(f"PDF write_line fallback fail: {e2}")
                pdf.ln(h)

    set_font("B", 16)
    write_line(title or "Dokument", h=10, align="C")
    pdf.ln(4)
    set_font("", 11)

    for line in (body or "").strip().split("\n"):
        line = line.rstrip()
        if not line:
            pdf.ln(4)
            continue
        if line.startswith("# "):
            set_font("B", 14)
            write_line(line[2:].strip(), h=8)
            set_font("", 11)
            pdf.ln(2)
        elif line.startswith("## "):
            set_font("B", 12)
            write_line(line[3:].strip(), h=7)
            set_font("", 11)
            pdf.ln(1)
        elif line.startswith("### "):
            set_font("B", 11)
            write_line(line[4:].strip(), h=6)
            set_font("", 11)
            pdf.ln(1)
        elif line.startswith("- ") or line.startswith("* "):
            clean = re.sub(r"\*\*([^*]+)\*\*", r"\1", line[2:])
            clean = re.sub(r"\*([^*]+)\*", r"\1", clean)
            write_line("• " + clean.strip(), h=6)
        elif re.match(r"^\d+\.\s", line):
            clean = re.sub(r"^\d+\.\s*", "", line)
            clean = re.sub(r"\*\*([^*]+)\*\*", r"\1", clean)
            clean = re.sub(r"\*([^*]+)\*", r"\1", clean)
            write_line(clean.strip(), h=6)
        else:
            clean = re.sub(r"\*\*([^*]+)\*\*", r"\1", line)
            clean = re.sub(r"\*([^*]+)\*", r"\1", clean)
            write_line(clean, h=6)

    out = pdf.output()
    if isinstance(out, str):
        out = out.encode("latin-1")
    elif isinstance(out, bytearray):
        out = bytes(out)
    return out



# --- NARZĘDZIA (obrazy, search itd.) ---

def find_attachment_in_history(name, history):
    for item in reversed(history):
        if item.attachments:
            for att in item.attachments:
                if hasattr(att, 'name') and att.name == name: return att
    return None

def open_url_and_get_content(url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = requests.get(url, headers=headers, timeout=20, stream=True)
        r.raise_for_status()
        ct = r.headers.get('Content-Type', '').lower()
        name = url.split('/')[-1].split('?')[0] or "file"
        data = base64.b64encode(r.content).decode()
        if 'image' in ct: return {"type": "image", "filename": f"{name}.png", "data": data}
        elif 'pdf' in ct: return {"type": "pdf", "filename": f"{name}.pdf", "data": data}
        else: raise ValueError("Unsupported file type")
    except Exception as e: raise RuntimeError(f"URL Error: {e}")

def _enhance_image_prompt(prompt: str) -> str:
    prompt = (prompt or "").strip()
    if not prompt:
        return prompt
    low = prompt.lower()
    quality_boost = ", highly detailed, sharp focus, natural lighting"
    if "highly detailed" not in low and "8k" not in low and len(prompt) < 120:
        prompt = prompt.rstrip(",. ") + quality_boost
    return prompt

def _generate_pollinations(prompt: str) -> bytes:
    """Próbuje kilka wariantów URL Pollinations (darmowe parametry najpierw)."""
    enhanced = _enhance_image_prompt(prompt)
    encoded = requests.utils.quote(enhanced)
    # kolejne warianty od najbardziej kompatybilnego / darmowego
    variants = [
        f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&nologo=true",
        f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&nologo=true&model=flux",
        f"https://image.pollinations.ai/prompt/{encoded}?width=768&height=768&nologo=true",
        f"https://gen.pollinations.ai/image/{encoded}?width=1024&height=1024&nologo=true",
    ]
    last_err = None
    for url in variants:
        try:
            print(f"ImageGen Pollinations try: {url[:100]}…")
            r = requests.get(url, timeout=90, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            if r.content and len(r.content) >= 1000 and r.headers.get("content-type", "").startswith("image"):
                return r.content
            # czasem zwraca HTML przy limicie
            if r.content and len(r.content) >= 1000 and r.content[:8] != b"<!DOCTYPE":
                return r.content
            last_err = RuntimeError(f"empty/invalid response ({len(r.content or b'')} B)")
        except Exception as e:
            last_err = e
            print(f"ImageGen Pollinations variant fail: {e}")
            continue
    raise RuntimeError(f"Pollinations all variants failed: {last_err}")

def _generate_ai_horde(prompt: str) -> bytes:
    enhanced = _enhance_image_prompt(prompt)
    headers = {
        "apikey": "0000000000",
        "Client-Agent": "MyBro:1.0:eosforus",
        "Content-Type": "application/json"
    }
    # mniejsze wymiary / mniej kroków = mniej odrzuceń na anonimowym kluczu
    payload = {
        "prompt": enhanced,
        "params": {
            "width": 512,
            "height": 512,
            "steps": 20,
            "cfg_scale": 7.0,
            "sampler_name": "k_euler_a",
            "n": 1
        },
        "nsfw": False,
        "censor_nsfw": True,
        "models": ["stable_diffusion"],
        "r2": True
    }
    r = requests.post("https://stablehorde.net/api/v2/generate/async", json=payload, headers=headers, timeout=30)
    r.raise_for_status()
    job_id = r.json().get("id")
    if not job_id:
        raise RuntimeError("AI Horde no job id")
    for _ in range(60):
        time.sleep(3)
        check = requests.get(f"https://stablehorde.net/api/v2/generate/check/{job_id}", headers=headers, timeout=15)
        check.raise_for_status()
        if check.json().get("done"):
            break
        if check.json().get("faulted"):
            raise RuntimeError("AI Horde job faulted")
    else:
        raise RuntimeError("AI Horde timeout")
    result = requests.get(f"https://stablehorde.net/api/v2/generate/status/{job_id}", headers=headers, timeout=30)
    result.raise_for_status()
    gens = result.json().get("generations", [])
    if not gens or not gens[0].get("img"):
        raise RuntimeError("AI Horde no image")
    img_url = gens[0]["img"]
    # R2 URL albo base64
    if img_url.startswith("http"):
        img_resp = requests.get(img_url, timeout=30)
        img_resp.raise_for_status()
        return img_resp.content
    return base64.b64decode(img_url)

def _generate_pollinations_simple(prompt: str) -> bytes:
    """Ostatnia deska ratunku – minimalny endpoint."""
    url = "https://image.pollinations.ai/prompt/" + requests.utils.quote(prompt[:500])
    r = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    if not r.content or len(r.content) < 500:
        raise RuntimeError("Pollinations simple empty")
    return r.content

def generate_image_from_prompt(prompt: str) -> bytes:
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("Empty prompt")
    if len(prompt) > 1500:
        prompt = prompt[:1500]
    errors = []
    for name, fn in (
        ("Pollinations", _generate_pollinations),
        ("AI Horde", _generate_ai_horde),
        ("Pollinations-simple", _generate_pollinations_simple),
    ):
        try:
            print(f"ImageGen: Trying {name}… prompt={prompt[:80]!r}")
            return fn(prompt)
        except Exception as e:
            errors.append(f"{name}: {e}")
            print(f"ImageGen {name} fail: {e}")
    raise RuntimeError(f"All image engines failed: {'; '.join(errors)}")

def composite_images(fg_bytes, bg_bytes):
    fg = Image.open(BytesIO(fg_bytes)).convert("RGBA")
    bg = Image.open(BytesIO(bg_bytes)).convert("RGBA")
    if fg.width > bg.width or fg.height > bg.height: fg.thumbnail(bg.size, Image.Resampling.LANCZOS)
    x = (bg.width - fg.width) // 2; y = (bg.height - fg.height) // 2
    bg.paste(fg, (x, y), fg)
    buf = BytesIO(); bg.save(buf, format="PNG"); return buf.getvalue()

def edit_image(b64, details):
    img = Image.open(BytesIO(base64.b64decode(b64)))
    op_match = re.search(r'operation="([^"]+)"', details)
    val_match = re.search(r'value="([^"]+)"', details)
    op = (op_match.group(1) if op_match else "show").lower()
    val = val_match.group(1) if val_match else "0"
    if op == "remove_background": 
        return remove(base64.b64decode(b64))
    if op == "show": 
        return base64.b64decode(b64)
    if op == "grayscale": 
        out = img.convert('L')
    elif op == "rotate": 
        out = img.rotate(int(val or 90), expand=True)
    elif op == "blur": 
        out = img.filter(ImageFilter.GaussianBlur(radius=int(val or 2)))
    else: 
        out = img
    buf = BytesIO(); out.save(buf, format="PNG"); return buf.getvalue()

def inpaint_image(b64, prompt):
    if not ZMO_API_KEY: raise ValueError("ZMO Key missing")
    files = {'image': ('i.png', base64.b64decode(b64), 'image/png')}
    r = requests.post(INPAINTING_API_URL, headers={'X-Api-Key': ZMO_API_KEY}, files=files, data={'mask_prompt': prompt}, timeout=90)
    r.raise_for_status(); return r.content

def create_story_pdf(struct, hist):
    pdf = FPDF(); pdf.add_page(); pdf.set_auto_page_break(True, 15)
    try:
        font = os.path.join(os.path.dirname(__file__), "fonts", "DejaVuSans.ttf")
        pdf.add_font('DejaVu', '', font, uni=True); pdf.set_font('DejaVu', '', 12)
    except: pdf.set_font('Helvetica', '', 12)
    for s in struct:
        if pdf.get_y()>250: pdf.add_page()
        if s.get("text"): pdf.write(8, s.get("text")); pdf.ln(10)
        if s.get("image_prompt"):
            try:
                img = generate_image_from_prompt(s.get("image_prompt"))
                if pdf.get_y()>200: pdf.add_page()
                pdf.image(BytesIO(img), x=15, w=pdf.w-30, type='PNG'); pdf.ln(10)
            except: pass
    out = pdf.output()
    if isinstance(out, str):
        out = out.encode('latin-1')
    elif isinstance(out, bytearray):
        out = bytes(out)
    return out

def search_web(q):
    try:
        with DDGS() as ddgs: return "\n".join([f"{r['title']}: {r['body']}" for r in ddgs.text(q, max_results=4)])
    except Exception as e: return str(e)

def scrape_web(url):
    try:
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        soup = BeautifulSoup(r.text, 'html.parser')
        for s in soup(["script", "style"]): s.decompose()
        return soup.body.get_text(separator='\n', strip=True)[:8000]
    except Exception as e: return str(e)

def process_att(att):
    """Uniwersalna obsługa DOWOLNYCH załączników: obrazy, PDF, DOCX, kod, tekst, binarki."""
    try:
        raw = base64.b64decode(att.data)
    except Exception:
        return f"[Plik {getattr(att, 'name', 'plik')}: błąd dekodowania base64]"

    mime = (getattr(att, 'mime_type', '') or '').lower()
    name = getattr(att, 'name', '') or 'plik'
    ext = os.path.splitext(name.lower())[1]

    # 1. Obrazy -> obiekt PIL (dla modeli wizyjnych)
    if "image" in mime or ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"):
        try:
            return Image.open(BytesIO(raw))
        except Exception as e:
            return f"[Obraz {name}: nie można otworzyć - {e}]"

    # 2. PDF -> wyciągnij tekst (pypdf)
    if mime == "application/pdf" or ext == ".pdf":
        try:
            reader = pypdf.PdfReader(BytesIO(raw))
            texts = []
            for i, page in enumerate(reader.pages[:25]):  # limit 25 stron
                try:
                    t = page.extract_text() or ""
                    if t.strip():
                        texts.append(f"--- Strona {i+1} ---\n{t[:5000]}")
                except Exception:
                    continue
            content = "\n".join(texts)[:18000]
            return f"[Plik PDF: {name}]\n{content or '(nie udało się wyodrębnić tekstu)'}"
        except Exception as e:
            return f"[PDF {name}: błąd odczytu - {e}]"

    # 3. DOCX -> wyciągnij tekst (python-docx)
    if "wordprocessingml" in mime or ext == ".docx":
        try:
            doc = Document(BytesIO(raw))
            paras = [p.text for p in doc.paragraphs if p.text.strip()]
            # też tabele
            for table in doc.tables:
                for row in table.rows:
                    cells = [c.text.strip() for c in row.cells if c.text.strip()]
                    if cells:
                        paras.append(" | ".join(cells))
            content = "\n".join(paras)[:18000]
            return f"[Plik DOCX: {name}]\n{content or '(pusty dokument)'}"
        except Exception as e:
            return f"[DOCX {name}: błąd odczytu - {e}]"

    # 4. Pliki tekstowe / kod -> dekoduj z nazwą i kolorowaniem
    TEXT_EXTS = {
        ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".htm", ".css",
        ".json", ".md", ".markdown", ".txt", ".csv", ".yaml", ".yml",
        ".xml", ".sql", ".sh", ".java", ".c", ".cpp", ".h", ".hpp",
        ".rs", ".go", ".php", ".rb", ".swift", ".kt", ".r", ".toml",
        ".ini", ".env", ".log", ".tex",
    }
    is_text_mime = (
        mime.startswith("text/")
        or mime in ("application/json", "application/xml", "application/javascript",
                    "application/x-python", "application/sql", "application/yaml")
    )
    if ext in TEXT_EXTS or is_text_mime:
        try:
            text = raw.decode("utf-8", errors="ignore")
            truncated = len(text) > 25000
            if truncated:
                text = text[:25000] + f"\n...[ucięto - pełny plik ma {len(raw)} bajtów]"
            lang = ext.lstrip(".") if ext else ""
            return f"[Plik: {name}]\n```{lang}\n{text}\n```"
        except Exception as e:
            return f"[Plik {name}: błąd dekodowania - {e}]"

    # 5. Próba odczytu jako tekst (pliki bez rozszerzenia itd.)
    try:
        sample = raw[:2000]
        if b"\x00" in sample:
            raise ValueError("binary")
        text = raw.decode("utf-8", errors="strict")
        if len(text.strip()) < 5:
            raise ValueError("too short")
        truncated = len(text) > 25000
        if truncated:
            text = text[:25000] + "...[ucięto]"
        return f"[Plik: {name} ({mime or 'nieznany typ'}, {len(raw)} bajtów)]\n{text}"
    except Exception:
        pass

    # 6. Binarka -> metadane + prośba o kontekst
    size_kb = len(raw) / 1024
    size_str = f"{size_kb:.1f} KB" if size_kb < 1024 else f"{size_kb/1024:.1f} MB"
    return (
        f"[Plik binarny: {name} ({mime or 'nieznany typ'}, {size_str}) - "
        f"nie mogę odczytać treści bezpośrednio. "
        f"Opisz, co mam z tym plikiem zrobić.]"
    )

# --- FASTAPI SETUP ---
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

class Attachment(BaseModel): name: str; mime_type: str; data: str
class HistoryItem(BaseModel): role: str; content: str; attachments: Optional[List[Attachment]] = None; isHidden: Optional[bool] = False
class ChatRequest(BaseModel): history: List[HistoryItem]; apiKey: Optional[str] = Field(None, alias="apiKey")
class DonateKeyRequest(BaseModel): apiKey: str
class ExportRequest(BaseModel): history: List[HistoryItem]

@app.post("/donate-key")
def dp(r: DonateKeyRequest):
    k=r.apiKey.strip(); keys=load_donated_keys_safely(); keys.append({"timestamp": datetime.now().isoformat(), "key": k})
    save_donated_keys(keys); return {"status": "ok"}

@app.post("/export-pdf")
def ep(r: ExportRequest):
    pdf = FPDF(); pdf.add_page(); pdf.set_auto_page_break(True, 15)
    try:
        font = os.path.join(os.path.dirname(__file__), "fonts", "DejaVuSans.ttf")
        pdf.add_font('DejaVu', '', font, uni=True); pdf.set_font('DejaVu', '', 10)
    except: pdf.set_font('Helvetica', '', 10)
    for m in r.history:
        if m.isHidden: continue
        pdf.multi_cell(0, 5, f"{'Ty' if m.role=='user' else 'AI'}: {m.content.replace('[IMG]','')}"); pdf.ln(2)
    out = pdf.output()
    if isinstance(out, str):
        out = out.encode('latin-1')
    elif isinstance(out, bytearray):
        out = bytes(out)
    return StreamingResponse(BytesIO(out), media_type="application/pdf", headers={"Content-Disposition": "attachment; filename=chat.pdf"})


# --- GŁOS (TTS) ---
class TTSRequest(BaseModel):
    text: str
    voice: Optional[str] = "pl-PL-MarekNeural"

@app.post("/tts")
async def tts(r: TTSRequest):
    try:
        communicate = edge_tts.Communicate(r.text, r.voice)
        audio_bytes = b""
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_bytes += chunk["data"]
        return StreamingResponse(
            BytesIO(audio_bytes),
            media_type="audio/mpeg",
            headers={"Content-Disposition": "inline; filename=speech.mp3"}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"TTS error: {str(e)}")


# --- GŁOS (STT) ---
def _transcribe_with_key(key: str, audio_path: str, language: Optional[str] = None) -> str:
    lang = language if language in ("pl", "en", "de", "fr", "es", "it", "pt", "ru", "uk", "cs", "sk") else None
    if key.startswith("gsk_"):
        client = groq.Groq(api_key=key)
        with open(audio_path, "rb") as f:
            kwargs = {
                "file": (os.path.basename(audio_path), f),
                "model": "whisper-large-v3",
                "response_format": "text",
            }
            if lang:
                kwargs["language"] = lang
            result = client.audio.transcriptions.create(**kwargs)
        return result if isinstance(result, str) else getattr(result, "text", str(result))
    if key.startswith("sk-") and not key.startswith("sk-ant-"):
        client = openai.OpenAI(api_key=key)
        with open(audio_path, "rb") as f:
            kwargs = {
                "file": f,
                "model": "whisper-1",
                "response_format": "text",
            }
            if lang:
                kwargs["language"] = lang
            result = client.audio.transcriptions.create(**kwargs)
        return result if isinstance(result, str) else getattr(result, "text", str(result))
    raise ValueError("Key type does not support STT (need Groq gsk_ or OpenAI sk-)")


@app.post("/stt")
async def stt(
    file: UploadFile = File(...),
    language: Optional[str] = Form(None),
    apiKey: Optional[str] = Form(None),
):
    global current_key_index
    raw = await file.read()
    if not raw or len(raw) < 100:
        raise HTTPException(status_code=400, detail="Pusty lub zbyt krótki plik audio.")
    orig_name = file.filename or "audio.webm"
    suffix = "." + orig_name.rsplit(".", 1)[-1].lower() if "." in orig_name else ".webm"
    if suffix not in (".webm", ".mp4", ".m4a", ".wav", ".ogg", ".mp3", ".mpeg", ".mpga", ".oga", ".flac"):
        suffix = ".webm"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(raw)
            tmp_path = tmp.name
        keys = ([apiKey] if apiKey else []) + api_keys[current_key_index:] + api_keys[:current_key_index]
        keys = [k for k in keys if k and (k.startswith("gsk_") or (k.startswith("sk-") and not k.startswith("sk-ant-")))]
        if not keys:
            raise HTTPException(
                status_code=503,
                detail="Brak kluczy API z obsługą STT (Groq lub OpenAI). Podaj własny klucz gsk_/sk-."
            )
        last_err = None
        for idx, k in enumerate(keys):
            if idx > 8:
                break
            try:
                text = _transcribe_with_key(k, tmp_path, language)
                if k in api_keys:
                    current_key_index = api_keys.index(k)
                return {"text": (text or "").strip()}
            except Exception as e:
                print(f"STT key fail ({k[:8]}...): {e}")
                last_err = e
                continue
        raise HTTPException(status_code=502, detail=f"STT failed: {last_err}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


# --- LLM CALL ---
def call_llm(key, msgs, sys_prompt):
    print(f"-> Call: {key[:8]}...")
    
    def fmt(prov):
        out = []
        for m in msgs:
            if m.get('isHidden'): continue
            role = 'assistant' if m['role']=='model' else 'user'
            content = " ".join(str(p) for p in m['parts'] if isinstance(p, str))
            if content: out.append({'role': role, 'content': content})
        if prov=='anthropic' and out and out[0]['role']=='assistant': del out[0]
        return out

    try:
        if key.startswith("sk-ant-"):
            client = anthropic.Anthropic(api_key=key)
            return client.messages.create(
                model="claude-sonnet-5-5",
                max_tokens=8000,
                system=sys_prompt,
                messages=fmt('anthropic')
            ).content[0].text

        elif key.startswith("gsk_"):
            client = groq.Groq(api_key=key)
            models = [
                "openai/gpt-oss-120b",
                "openai/gpt-oss-20b",
                "qwen/qwen3.8-27b",
                "llama-3.1-8b-instant"
            ]
            payload = fmt('groq')
            payload.insert(0, {"role": "system", "content": sys_prompt})
            for m in models:
                try:
                    print(f"  Trying Groq model: {m}")
                    return client.chat.completions.create(
                        model=m, 
                        messages=payload, 
                        max_tokens=8000
                    ).choices[0].message.content
                except Exception as e:
                    print(f"  Groq {m}: {e}")
                    continue
            raise RuntimeError("All Groq models failed")

        elif key.startswith("sk-"):
            client = openai.OpenAI(api_key=key)
            payload = fmt('openai')
            payload.insert(0, {"role": "system", "content": sys_prompt})
            return client.chat.completions.create(
                model="gpt-4o",
                messages=payload,
                max_tokens=8000
            ).choices[0].message.content

        elif key.startswith("AIza"):
            client = genai.Client(api_key=key)
            models_gemini = [
                "gemini-3.8-flash",
                "gemini-2.5-flash",
                "gemini-2.5-pro",
                "gemini-3.5-flash",
                "gemini-3.1-pro-preview"
            ]
            contents = []
            for m in msgs:
                if m.get('isHidden'): continue
                role = "user" if m['role'] == 'user' else "model"
                parts = []
                for p in m.get('parts', []):
                    if isinstance(p, str) and p.strip():
                        parts.append(types.Part.from_text(text=p))
                    elif isinstance(p, Image.Image):
                        # google-genai wymaga Part z danymi bajtowymi, nie surowego PIL
                        try:
                            buf = BytesIO()
                            img = p
                            if img.mode not in ("RGB", "RGBA"):
                                img = img.convert("RGB")
                            img.save(buf, format="PNG")
                            parts.append(types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png"))
                        except Exception as img_err:
                            print(f"  Gemini image part skip: {img_err}")
                            parts.append(types.Part.from_text(text="[obraz – nie udało się dołączyć]"))
                if parts:
                    contents.append(types.Content(role=role, parts=parts))
            for gm in models_gemini:
                try:
                    print(f"  Trying Gemini model: {gm}")
                    response = client.models.generate_content(
                        model=gm,
                        contents=contents,
                        config=types.GenerateContentConfig(
                            system_instruction=sys_prompt,
                            max_output_tokens=8000,
                            temperature=0.7
                        )
                    )
                    return response.text
                except Exception as e:
                    print(f"  Gemini {gm}: {e}")
                    continue
            raise RuntimeError("All Gemini models failed")
        else:
            raise ValueError("Unknown key type")
    except Exception as e:
        raise RuntimeError(f"Provider error: {e}")


def _extract_and_build_files(ai_text: str) -> tuple[str, list]:
    """
    Wyciąga z odpowiedzi AI tagi [FILE:], [GENERATE_DOCX:], [GENERATE_PDF:]
    i buduje listę plików do zwrócenia.
    Zwraca (czysty_tekst_bez_tagów, lista_plików).
    """
    files = []
    clean = ai_text

    # 1. [FILE: name.ext] ... [/FILE]
    file_pattern = re.compile(
        r'\[FILE:\s*([^\]]+?)\]\s*(.*?)\s*\[/FILE\]',
        re.DOTALL | re.IGNORECASE
    )
    for m in file_pattern.finditer(ai_text):
        fname = m.group(1).strip()
        content = m.group(2)
        # usuń ewentualne otaczające ```language
        content = re.sub(r'^```[\w]*\n?', '', content.strip())
        content = re.sub(r'\n?```$', '', content)
        files.append(make_file_response(fname, content))
    clean = file_pattern.sub('', clean)

    # 2. [GENERATE_DOCX: Tytuł] ... [/GENERATE_DOCX]
    docx_pattern = re.compile(
        r'\[GENERATE_DOCX:\s*([^\]]*)\]\s*(.*?)\s*\[/GENERATE_DOCX\]',
        re.DOTALL | re.IGNORECASE
    )
    for m in docx_pattern.finditer(ai_text):
        title = m.group(1).strip() or "Dokument"
        body = m.group(2).strip()
        try:
            docx_bytes = create_docx_from_text(title, body)
            safe_name = re.sub(r'[^\w\s\-ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]', '', title)[:40].strip() or "dokument"
            safe_name = safe_name.replace(' ', '_') + ".docx"
            files.append(make_file_response(safe_name, docx_bytes))
        except Exception as e:
            print(f"DOCX gen error: {e}")
    clean = docx_pattern.sub('', clean)

    # 3. [GENERATE_PDF: Tytuł] ... [/GENERATE_PDF]
    pdf_pattern = re.compile(
        r'\[GENERATE_PDF:\s*([^\]]*)\]\s*(.*?)\s*\[/GENERATE_PDF\]',
        re.DOTALL | re.IGNORECASE
    )
    for m in pdf_pattern.finditer(ai_text):
        title = m.group(1).strip() or "Dokument"
        body = m.group(2).strip()
        try:
            pdf_bytes = create_pdf_from_text(title, body)
            if not pdf_bytes or len(pdf_bytes) < 50:
                raise RuntimeError("PDF output too small / empty")
            safe_name = re.sub(r'[^\w\s\-ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]', '', title)[:40].strip() or "dokument"
            safe_name = safe_name.replace(' ', '_') + ".pdf"
            files.append(make_file_response(safe_name, pdf_bytes, "application/pdf"))
            print(f"PDF generated OK: {safe_name} ({len(pdf_bytes)} bytes)")
        except Exception as e:
            print(f"PDF gen error: {e}")
            # awaryjnie oddaj treść jako .txt, żeby użytkownik coś dostał
            safe_name = re.sub(r'[^\w\s\-ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]', '', title)[:40].strip() or "dokument"
            safe_name = safe_name.replace(' ', '_') + ".txt"
            files.append(make_file_response(safe_name, f"{title}\n\n{body}"))
    clean = pdf_pattern.sub('', clean)

    # posprzątaj wielokrotne puste linie
    clean = re.sub(r'\n{3,}', '\n\n', clean).strip()
    return clean, files


@app.post("/chat")
def chat(r: ChatRequest, resp: Response):
    global current_key_index
    prompt_parts = []
    for i in [x for x in r.history if not x.isHidden]:
        parts = [i.content] if i.content else []
        if i.attachments: 
            for a in i.attachments: parts.append(process_att(a))
        prompt_parts.append({'role': i.role, 'parts': parts})

    loops = 0
    while loops < 3:
        loops += 1
        keys = ([r.apiKey] if r.apiKey else []) + api_keys[current_key_index:] + api_keys[:current_key_index]
        ai_text = None
        for idx, k in enumerate(keys):
            if idx > 15: break
            try:
                ai_text = call_llm(k, prompt_parts, SYSTEM_INSTRUCTION)
                if k in api_keys: current_key_index = api_keys.index(k)
                break
            except Exception as e: 
                print(f"Key fail: {e}")
        
        if not ai_text: 
            resp.status_code = 429
            return {"error": "All keys failed", "all_keys_exhausted": True}

        # --- najpierw narzędzia wymagające pętli (search/scrape) ---
        if "[TOOL:search]" in ai_text:
            q = re.search(r"\[TOOL:search\](.*?)\[/TOOL\]", ai_text, re.DOTALL).group(1)
            prompt_parts.append({'role': 'model', 'parts': [ai_text]})
            prompt_parts.append({'role': 'user', 'parts': [f"WYNIK: {search_web(q)}"]})
            continue
        if "[TOOL:scrape]" in ai_text:
            u = re.search(r"\[TOOL:scrape\](.*?)\[/TOOL\]", ai_text, re.DOTALL).group(1)
            prompt_parts.append({'role': 'model', 'parts': [ai_text]})
            prompt_parts.append({'role': 'user', 'parts': [f"TREŚĆ: {scrape_web(u)}"]})
            continue

        # --- narzędzia obrazkowe / URL / STORY_PDF (zwracają od razu) ---
        user_msg = ai_text.split('[')[0].strip() if '[' in ai_text else ai_text.strip()

        if "[OPEN_URL:" in ai_text:
            try:
                u = re.search(r'url="([^"]*)"', ai_text).group(1)
                d = open_url_and_get_content(u)
                return {f"{d['type']}_file": {"filename":d['filename'], "data":d['data']}, "generated_text": user_msg or "Otwarto."}
            except: return {"generated_text": "Błąd URL."}
        elif "[GENERATE_IMAGE:" in ai_text:
            try:
                p = re.search(r'prompt="([^"]*)"', ai_text).group(1)
                return {"image_file": {"filename":"gen.png", "data": base64.b64encode(generate_image_from_prompt(p)).decode()}, "generated_text": user_msg or "Obraz:"}
            except: return {"generated_text": "Błąd gen."}
        elif "[EDIT_IMAGE:" in ai_text:
            try:
                att = next((a for i in reversed(r.history) if i.attachments for a in i.attachments if "image" in a.mime_type), None)
                if not att: return {"generated_text": "Brak obrazu."}
                return {"image_file": {"filename":"edit.png", "data": base64.b64encode(edit_image(att.data, ai_text)).decode()}, "generated_text": user_msg or "Zrobione."}
            except: return {"generated_text": "Błąd edycji."}
        elif "[CHANGE_BACKGROUND:" in ai_text:
            try:
                att = next((a for i in reversed(r.history) if i.attachments for a in i.attachments if "image" in a.mime_type), None)
                if not att: return {"generated_text": "Brak obrazu."}
                fg = remove(base64.b64decode(att.data))
                bg = generate_image_from_prompt(re.search(r'background_prompt="([^"]*)"', ai_text).group(1))
                return {"image_file": {"filename":"bg.png", "data": base64.b64encode(composite_images(fg, bg)).decode()}, "generated_text": user_msg or "Tło zmienione."}
            except: return {"generated_text": "Błąd tła."}
        elif "[INPAINT_IMAGE:" in ai_text:
            try:
                att = next((a for i in reversed(r.history) if i.attachments for a in i.attachments if "image" in a.mime_type), None)
                return {"image_file": {"filename":"inp.png", "data": base64.b64encode(inpaint_image(att.data, re.search(r'prompt="([^"]*)"', ai_text).group(1))).decode()}, "generated_text": user_msg or "Usunięto."}
            except: return {"generated_text": "Błąd inpaint."}
        elif "[CREATE_COMPOSITION:" in ai_text:
            try:
                m = re.search(r"\[CREATE_COMPOSITION:(.*?)\]", ai_text, re.DOTALL).group(1)
                def gp(k): return re.search(f'{k}="([^"]*)"', m).group(1)
                fg_b = base64.b64decode(find_attachment_in_history(gp("foreground_value"), r.history).data) if gp("foreground_source_type")=="file" else generate_image_from_prompt(gp("foreground_value"))
                bg_b = base64.b64decode(find_attachment_in_history(gp("background_value"), r.history).data) if gp("background_source_type")=="file" else generate_image_from_prompt(gp("background_value"))
                return {"image_file": {"filename":"comp.png", "data": base64.b64encode(composite_images(fg_b, bg_b)).decode()}, "generated_text": user_msg or "Kompozycja."}
            except: return {"generated_text": "Błąd komp."}
        elif "[STORY_PDF]" in ai_text:
            try:
                j = json.loads(re.search(r"\[STORY_PDF\](.*)\[/STORY_PDF\]", ai_text, re.DOTALL).group(1))
                return {"pdf_file": {"filename":"story.pdf", "data": base64.b64encode(create_story_pdf(j, r.history)).decode()}, "generated_text": user_msg or "PDF."}
            except: return {"generated_text": "Błąd PDF."}

        # --- NOWE: pliki kodu / docx / pdf z tagów ---
        clean_text, generated_files = _extract_and_build_files(ai_text)

        result = {"generated_text": clean_text or user_msg}

        if generated_files:
            # kompatybilność wsteczna + nowa lista
            result["files"] = generated_files
            # jeśli jest dokładnie jeden plik obrazu/pdf – zostaw stare klucze
            if len(generated_files) == 1:
                f = generated_files[0]
                if f["mime_type"].startswith("image/"):
                    result["image_file"] = {"filename": f["filename"], "data": f["data"]}
                elif f["mime_type"] == "application/pdf":
                    result["pdf_file"] = {"filename": f["filename"], "data": f["data"]}

        return result
    
    return {"generated_text": "Limit operacji."}
