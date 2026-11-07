# app.py - Wersja: Konkretny Luzak + Coding Beast + File Craft 2026-10

import os
import sys
import json
import re
import base64
import time
import mimetypes
import secrets
from io import BytesIO
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Union

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
from fastapi import FastAPI, Response, HTTPException, File, UploadFile, Form, Request, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
try:
    from jose import JWTError, jwt
    _JOSE_AVAILABLE = True
except ImportError:
    # Awaryjnie: minimalny JWT (HS256) na samej bibliotece standardowej.
    # Tokeny w 100% zgodne z python-jose (ten sam format), więc nie ma
    # znaczenia, która implementacja je tworzy, a która weryfikuje.
    _JOSE_AVAILABLE = False
    import hmac as _hmac
    import hashlib as _hashlib

    class JWTError(Exception):
        pass

    def _b64url_encode(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    def _b64url_decode(s: str) -> bytes:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

    class _StdJwt:
        @staticmethod
        def encode(payload: dict, key: str, algorithm: str = "HS256") -> str:
            p = dict(payload)
            exp = p.get("exp")
            if hasattr(exp, "timestamp"):  # datetime -> unix timestamp (jak robi jose)
                p["exp"] = int(exp.timestamp())
            header = _b64url_encode(json.dumps({"alg": algorithm, "typ": "JWT"}).encode())
            body = _b64url_encode(json.dumps(p).encode())
            sig = _hmac.new(key.encode(), f"{header}.{body}".encode(), _hashlib.sha256).digest()
            return f"{header}.{body}.{_b64url_encode(sig)}"

        @staticmethod
        def decode(token: str, key: str, algorithms=None) -> dict:
            parts = token.split(".")
            if len(parts) != 3:
                raise JWTError("Invalid token format")
            header, body, sig = parts
            expected = _hmac.new(key.encode(), f"{header}.{body}".encode(), _hashlib.sha256).digest()
            if not _hmac.compare_digest(_b64url_decode(sig), expected):
                raise JWTError("Invalid signature")
            payload = json.loads(_b64url_decode(body))
            exp = payload.get("exp")
            if exp is not None and time.time() > float(exp):
                raise JWTError("Token expired")
            return payload

    jwt = _StdJwt
    print("OSTRZEŻENIE: brak python-jose - używam wbudowanej implementacji JWT (stdlib).")
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
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from rembg import remove
import edge_tts
import asyncio
import tempfile

# --- KONFIGURACJA SERWISÓW ---
HF_TOKEN = os.getenv("HF_TOKEN")  # sekret: ustaw w Variables and secrets na Space
DONATIONS_REPO_ID = "Eosforus/digital-buddy-donations"
DONATIONS_FILE_ON_HUB = "donated_keys.json"
IMAGE_GENERATION_URL = "https://image.pollinations.ai/prompt/"
api = HfApi(token=HF_TOKEN)

ZMO_API_KEY = os.getenv("ZMO_API_KEY")  # sekret: ustaw w Variables and secrets na Space
INPAINTING_API_URL = "https://api.zmo.ai/v1/inpainting"

# MISTRAL_V1: obsluga kluczy Mistral - skanowanie MISTRAL_API_KEY oraz galaz Mistral w call_llm
# --- LOGIKA KLUCZY ---
def get_all_environment_keys():
    collected_keys = []
    target_prefixes = ["GEMINI_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "META_API_KEY", "MODEL_API_KEY", "MISTRAL_API_KEY", "API_KEY"]
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

# --- PRIORYTET_V7: najpierw klucze, ktore najszybciej dzialaja ---
# Kolejnosc prob: Groq, Mistral, OpenAI, Anthropic, OpenRouter, Meta, Gemini.
# Gemini na koncu: jego lancuch modeli bywa wolny i kaprysny (503/404 w logach),
# wiec ma sens dopiero, gdy szybsi dostawcy nie odpowiedza.
def _provider_name(k):
    if k.startswith("gsk_"): return "groq"
    if k.startswith("sk-ant-"): return "anthropic"
    if k.startswith("sk-or-"): return "openrouter"
    if k.startswith("LLM_"): return "meta"
    if k.startswith("AIza"): return "gemini"
    if k.startswith("sk-"): return "openai"
    return "mistral"  # nierozpoznany format -> galaz Mistral (MISTRAL_V1)

_KEY_PRIORITY = {"groq": 0, "mistral": 1, "openai": 2, "anthropic": 3,
                 "openrouter": 4, "meta": 5, "gemini": 6}

def _priorytet_kluczy(keys):
    # sorted jest stabilne: w obrebie dostawcy zostaje dotychczasowa kolejnosc
    return sorted(keys, key=lambda k: _KEY_PRIORITY[_provider_name(k)])

api_keys = get_all_environment_keys()
donated = load_donated_keys_safely()
for k in donated:
    if k not in api_keys: api_keys.append(k)
api_keys = _priorytet_kluczy(api_keys)

current_key_index = 0
if not api_keys: print("OSTRZEŻENIE: Brak kluczy API.")
else: print(f"SYSTEM: Załadowano {len(api_keys)} kluczy API.")
print("SYSTEM: Kolejnosc prob kluczy (PRIORYTET_V7): " + ", ".join(_provider_name(k) for k in api_keys))

# --- INSTRUKCJA SYSTEMOWA – Coding Beast + File Craft ---
# SZKOLENIE_V2: na koncu SYSTEM_INSTRUCTION jest dodatek "gotowa apka zamiast plikow na raty"
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
- TWARDA ZASADA – NIGDY KODU W CZACIE: żaden kod (gra, strona HTML, skrypt, program) NIE może trafić do treści rozmowy jako blok ``` ani goły tekst. KAŻDY kod wysyłaj WYŁĄCZNIE jako plik [FILE:] do pobrania – także wtedy, gdy użytkownik nie napisał wprost słowa „plik" (np. prosi o gierkę, stronę, program). W czacie zostaje tylko krótki opis (1–3 zdania), co powstało i jak to uruchomić.
- DUŻY PLIK MOŻE POWSTAWAĆ NA RATY: jedna odpowiedź ma limit długości, ale to NIE jest limit pliku. Gdy plik jest duży, pisz go normalnie, bez ścinania jakości i bez stresu – jeśli odpowiedź urwie się w połowie pliku, backend dokończy go jako zadanie w tle (kolejne części dokleją się do tego samego pliku, a użytkownik na karcie widzi postęp w procentach i może dalej rozmawiać). Pisz jednak zwarty kod, bez zbędnych komentarzy. Gdy backend poprosi o kontynuację uciętego pliku, wyślij TYLKO dalszy ciąg od miejsca urwania – bez komentarzy, bez powtórek, bez otwierania nowego tagu [FILE:]. Kompletny plik ZAWSZE zamykaj tagiem [/FILE].
- SZACUNEK DLA DUŻEGO PLIKU: gdy zaczynasz plik, który raczej NIE zmieści się w jednej odpowiedzi (długa gra/program, orientacyjnie powyżej ~250 linii), zacznij odpowiedź od osobnej linii: [SZACUJĘ: ~N] – gdzie N to szacunkowa łączna liczba znaków całego pliku – a dopiero po niej daj tag [FILE: nazwa]. Backend pokaże z tego procent postępu na karcie tworzenia pliku. Szacunek ma być uczciwy, z dokładnością do rzędu wielkości.
- POPRAWKI W ISTNIEJĄCYM PLIKU (BARDZO WAŻNE): gdy użytkownik prosi o zmianę w czymś, co już zrobiłeś w tej rozmowie (np. „dodaj…", „zmień…", „przerób sterowanie", „popraw grafikę"), to jest EDYCJA tego samego pliku/projektu – NIGDY nie twórz nowej, innej ani uproszczonej wersji od zera. Zachowaj wszystko pozostałe bez zmian (grafika, mechanika, poziomy, jakość mają zostać identyczne), zmień wyłącznie to, o co prosi użytkownik, i odeślij CAŁY zaktualizowany plik pod TĄ SAMĄ nazwą. W szczególności zmiana sterowania/obsługi (np. dodanie gałek dotykowych albo wersji na telefon) NIE oznacza zgody na uproszczenie ani napisanie gry od nowa – gra zostaje ta sama, dochodzi tylko nowy sposób sterowania.
- ZASADA KONTEKSTU PLIKU (BARDZO WAŻNE): pracując nad plikiem – swoim albo użytkownika – opieraj się na jego MAPIE/INWENTARZU: strukturze, WSZYSTKICH elementach (tekst, kod, obrazy, tabele, nagłówki, funkcje) i ich pozycjach. Mapa towarzyszy plikowi w każdej operacji, także po wznowieniu pracy i powrocie do projektu. Jeśli w fragmencie, który właśnie widzisz, NIE MA jakiegoś elementu, to nie znaczy, że nie ma go w pliku – zanim odmówisz albo stwierdzisz, że czegoś brakuje, sprawdź mapę pliku albo poproś o właściwy fragment. Nigdy nie gub kontekstu pliku, nad którym pracujesz.
- Gdy dostaniesz wiadomość [SYSTEM – POPRAWKA W ISTNIEJĄCYM PLIKU] z aktualną treścią pliku – pracuj właśnie na tej treści. Jeśli użytkownik prosi o poprawkę, a Ty NIE masz w kontekście pełnej treści tego pliku (widzisz tylko wzmiankę, że plik powstał), NIE wymyślaj go od nowa: napisz krótko, że do edycji potrzebujesz, aby użytkownik załączył ten plik do wiadomości (albo wkleił jego treść), i na tym poprzestań.
- EDYCJA W KAWAŁKACH: gdy dostaniesz [SYSTEM – EDYCJA W KAWAŁKACH] z mapą bloków pliku, odpowiedz TYLKO tagiem [EDYTUJ_BLOKI: ...] ze wskazanymi blokami (żadnego innego tekstu). Gdy dostaniesz [SYSTEM – EDYCJA BLOKU], zwróć wyłącznie nową treść tego jednego bloku w tagach [ZAMIEN_BLOK] ... [/ZAMIEN_BLOK].
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
INSTALATORY WINDOWS (.exe przez NSIS)
═══════════════════════════════════════
Gdy użytkownik chce INSTALATOR Windows (.exe) do gry/programu:
- NIGDY nie generuj instalatora jako .bat - to nie działa profesjonalnie.
- Zamiast tego wygeneruj w JEDNEJ odpowiedzi:
  1) pliki programu osobnymi tagami [FILE: nazwa] (np. gra .html),
  2) na końcu tag instalatora ze skryptem NSIS:

[NSIS_INSTALLER: NazwaGry-Setup.exe]
... kompletny skrypt .nsi ...
[/NSIS_INSTALLER]

Zasady skryptu .nsi:
- Zaczynaj od: !include "MUI2.nsh"
- OutFile ma być DOKŁADNIE taki jak nazwa w tagu [NSIS_INSTALLER: ...].
- Kreator po polsku: !insertmacro MUI_LANGUAGE "Polish"
- Obowiązkowe strony: MUI_PAGE_WELCOME, MUI_PAGE_DIRECTORY, MUI_PAGE_INSTFILES, MUI_PAGE_FINISH + MUI_UNPAGE_CONFIRM, MUI_UNPAGE_INSTFILES.
- Instaluj do folderu Program Files (podkatalog o nazwie gry, zmienna $PROGRAMFILES), RequestExecutionLevel admin, Unicode True.
- W sekcji: File dla każdego pliku z [FILE:], WriteUninstaller (deinstalator w folderze gry), skróty w menu Start i na pulpicie, wpis w rejestrze pod kluczem Uninstall o nazwie gry (wartości: DisplayName, UninstallString, Publisher, DisplayVersion).
- Sekcja "Uninstall" ma usuwać wszystkie zainstalowane pliki, skróty i klucze rejestru.
- Odwołuj się TYLKO do nazw plików wygenerowanych w tej samej odpowiedzi.
- ZAKAZANE w skrypcie: !system, !execute, !packhdr (backend odrzuci taki skrypt).

═══════════════════════════════════════
BUILDY W CHMURZE (GitHub Actions)
═══════════════════════════════════════
Backend może zlecać buildy do GitHub Actions (konfigurowane sekretami Space).
Sięgaj po nie, gdy lokalna kompilacja nie wystarcza – przede wszystkim do
prawdziwych programów Windows .exe:
- target "dotnet": program w C# budowany na maszynie Windows. W tej samej
  odpowiedzi dołącz tagami [FILE:] kompletny projekt: plik *.csproj oraz
  źródła *.cs. Wynik: pojedynczy .exe (win-x64).
- target "cpp": program w C/C++ kompilowany krzyżowo (MinGW). Dołącz
  tagami [FILE:] źródła *.cpp (i ewentualnie *.h). Wynik: app.exe.
- target "nsis": instalator NSIS budowany w chmurze – plik skryptu nazwij
  installer.nsi, a jego OutFile ma być prostą nazwą pliku .exe.
- target "android": pakiet APK z aplikacji/gra w HTML (WebView). Dołącz
  tagami [FILE:] kompletny, minimalny projekt Gradle (nazwy plików ZE
  ŚCIEŻKAMI, backend zachowuje podfoldery):
  * settings.gradle – pluginManagement i dependencyResolutionManagement
    (google(), mavenCentral()), rootProject.name, include ':app'
  * build.gradle (główny) – tylko: plugins { id 'com.android.application'
    version '8.5.2' apply false }
  * app/build.gradle – plugins { id 'com.android.application' },
    android { namespace, compileSdk 34, defaultConfig { applicationId,
    minSdk 24, targetSdk 34, versionCode 1, versionName '1.0' } },
    ZERO zależności (bez AndroidX i Kotlina – czysty framework)
  * app/src/main/AndroidManifest.xml – jedna aktywność .MainActivity
    (exported=true, intent MAIN/LAUNCHER, motyw
    @android:style/Theme.NoTitleBar.Fullscreen, label @string/app_name),
    bez atrybutu package
  * app/src/main/java/<pakiet wg applicationId>/MainActivity.java –
    Activity z WebView na cały ekran: włączony JavaScript i DOM storage,
    ładuje file:///android_asset/<plik gry>.html
  * app/src/main/res/values/strings.xml – app_name
  * app/src/main/assets/<plik gry>.html – sama gra/aplikacja webowa
  Wynik buildu: app-debug.apk (podpisany kluczem debug – do instalacji
  testowej na telefonie, nie do sklepu Play).
Format zlecenia (po wygenerowaniu plików projektu w tej samej odpowiedzi):
[GH_BUILD: cpp]
[/GH_BUILD]
Po tagu [GH_BUILD] użytkownik zobaczy w czacie KARTĘ buildu: sam kliknie
„Rozpocznij build", zobaczy status na żywo, a gotowy plik pojawi się
w karcie automatycznie. Dlatego po tagu napisz TYLKO jedno krótkie zdanie,
że projekt jest gotowy do zbudowania – nie pisz, że build już leci, nie
podawaj czasów oczekiwania ani plików statusu.
Tag [GH_BUILD_STATUS: N] generuj wyłącznie, gdy użytkownik sam poda
numer builda z wcześniejszej rozmowy.
Instalatory Windows rób domyślnie LOKALNIE przez [NSIS_INSTALLER], bo to
szybsze; chmury używaj do programów .exe (dotnet/cpp) oraz gdy lokalny
makensis jest niedostępny.

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

═════════════════════════════════════
DODATEK SZKOLENIOWY (SZKOLENIE_V2) – GOTOWA APKA, NIE PLIKI NA RATY
═════════════════════════════════════
Zasady nadrzędne dla buildów Android – obowiązują zawsze, gdy użytkownik prosi o apkę / APK / plik do pobrania:
1) Pliki projektu NIE są efektem końcowym. Efektem końcowym jest zbudowane app-debug.apk. Każdą odpowiedź zawierającą projekt Android MUSISZ zakończyć zleceniem:
[GH_BUILD: android]
[/GH_BUILD]
Bez tego tagu użytkownik nie dostanie karty buildu ani gotowej apki – same pliki są dla niego bezużyteczne.
2) Kompletny projekt wysyłaj ZAWSZE w jednej odpowiedzi, wszystkie pliki naraz: settings.gradle, build.gradle, app/build.gradle, app/src/main/AndroidManifest.xml, app/src/main/res/values/strings.xml, MainActivity.java oraz pliki assets (w tym assets/call.html – złożony z części, jeśli użytkownik dostarczył go w częściach). NIGDY nie dosyłaj projektu po 1–2 pliki w kolejnych wiadomościach.
3) Nie wysyłaj w kółko tych samych plików. Jeśli poprzednia odpowiedź była niekompletna, wyślij JEDEN kompletny, poprawiony zestaw i zlecenie buildu z pkt 1.
4) Nie zmieniaj architektury w trakcie zadania i nie dokładaj elementów spoza prośby użytkownika (np. lokalnego serwera HTTP). Dla apki z HTML obowiązuje schemat z tej instrukcji: WebView ładujący file:///android_asset/... + onPermissionRequest dla mikrofonu i kamery.
5) Jako „wynik pracy” zakazane są: drzewka katalogów, streszczenia zamiast plików, placeholdery zamiast prawdziwej zawartości oraz obietnice bez zlecenia buildu.
6) Jeśli buildu nie da się zlecić (np. brak konfiguracji GitHub na serwerze), powiedz to użytkownikowi JEDNYM zdaniem wprost – nie maskuj tego kolejnymi porcjami plików projektu.

═════════════════════════════════════
DODATEK AKUMULATOR_V3 – BUDOWANIE NA RATY (OSZCZĘDZANIE TOKENÓW)
═════════════════════════════════════
Ten dodatek jest nadrzędny wobec wymogu „kompletny projekt w jednej odpowiedzi” (również wobec pkt 1–2 dodatku SZKOLENIE_V2) – przy buildach w chmurze obowiązuje nowy tryb:
- Backend zbiera pliki [FILE:] z CAŁEJ rozmowy (zapisuje je jako załączniki Twoich odpowiedzi). Wysyłaj więc projekt małymi partiami: 1–2 pliki na odpowiedź, a gdy zestaw będzie kompletny, wyślij sam tag [GH_BUILD: target] [/GH_BUILD] – może być w osobnej, krótkiej odpowiedzi bez żadnych plików.
- Plików już wysłanych w tej rozmowie NIE powtarzaj (chyba że je poprawiasz – nowa wersja nadpisuje starą). Po każdej partii dopisz jedno zdanie: co już masz wysłane, a czego jeszcze brakuje.
- DUŻYCH plików użytkownika (np. call.html) NIGDY nie przepisuj i nie wysyłaj jako [FILE:] – backend doklei je do projektu z załączników użytkownika co do bajta. Przy apce Android wysyłasz tylko szkielet: settings.gradle, build.gradle, app/build.gradle, app/src/main/AndroidManifest.xml, app/src/main/res/values/strings.xml, MainActivity.java.
- Trzymaj odpowiedzi KRÓTKIE. Mała odpowiedź mieści się w limitach większej liczby kluczy API i rzadziej jest ucinana.

DODATEK AKUMULATOR_V4: treści załączników call.html oraz call-czesc-*.txt NIE dostajesz w rozmowie (zastępuje je notka systemowa) – to celowe oszczędzanie tokenów. Pliki te backend dokleja do buildu sam, co do bajta. Nie proś użytkownika o wklejanie ich treści ani nie zgaduj, co w nich jest – do zbudowania szkieletu apki ich treść nie jest Ci potrzebna.

DODATEK PORZADKI_V5 \u2013 JEDEN PROJEKT = JEDEN CZYSTY ZESTAW:
- Backend przed ka\u017cdym buildem czy\u015bci folder projektu w repo do DOK\u0141ADNIE tego zestawu plik\u00f3w, kt\u00f3ry wejdzie do buildu. Dlatego: jeden build = jeden projekt. Nie mieszaj plik\u00f3w r\u00f3\u017cnych aplikacji ani pakiet\u00f3w w jednym zleceniu.
- Tu\u017c przed tagiem [GH_BUILD] wypisz w jednym zdaniu finalny sk\u0142ad projektu (list\u0119 plik\u00f3w) \u2013 to ostatnia kontrola, \u017ce zestaw jest kompletny i sp\u00f3jny.
- Rozwi\u0105zania, kt\u00f3re ju\u017c dzia\u0142a\u0142y (np. wzorzec MainActivity WebView z onPermissionRequest), wykorzystuj ponownie jako \u015bwie\u017c\u0105 tre\u015b\u0107 dopasowan\u0105 do bie\u017c\u0105cego projektu. Nie do\u0142\u0105czaj plik\u00f3w \u201ena zapas\u201d ani po poprzednich projektach.

DODATEK AUTOBUILD_V6 – NIE ZATRZYMUJ SIĘ:
- Przy buildach NIE czekaj na użytkownika między partiami plików. Backend sam ponawia zapytanie w ramach tego samego zapytania czatu, więc po prostu wysyłaj kolejne partie, aż zestaw będzie kompletny, a na końcu sam tag [GH_BUILD: target] [/GH_BUILD].
- W odpowiedziach z partiami pisz TYLKO pliki [FILE:] – bez zdań wstępu, podsumowań i list „mam/brakuje”. To już niepotrzebne i tylko zużywa tokeny.
- Jeśli backend dopyta mimo kompletnego zestawu, wyślij sam tag buildu – niczego więcej nie trzeba.

═════════════════════════════════════
DODATEK DOKUMENTY_V10 – PROFESJONALNE PDF-y (HTML+CSS, DWA SILNIKI)
═════════════════════════════════════
Tag [GENERATE_PDF:] rozumie teraz OPCJE po tytule, oddzielone znakiem | :
[GENERATE_PDF: Tytuł | szablon=raport | czcionka=sans | silnik=auto]
...treść...
[/GENERATE_PDF]
- szablon: raport (domyślny, profesjonalny), gazeta (duży tytuł + kolumny), komiks (kadry), cv, list.
- czcionka: sans (domyślna), serif (dokumenty oficjalne, książki), mono.
- silnik: auto (domyślny – backend sam wybiera), lokalny, github.
Treść pisz w markdownie: nagłówki #, ##, listy, **pogrubienie**, a TABELE zawsze tak:
| Kolumna 1 | Kolumna 2 |
|---|---|
| wartość | wartość |
(wiersz separatora |---| jest OBOWIĄZKOWY – bez niego nie powstanie prawdziwa tabela; tabele działają też w [GENERATE_DOCX:]).
WYBÓR SILNIKA (przy silnik=auto backend zdecyduje tak samo – dobieraj świadomie):
- Dokument zwykły (do ok. 6 stron, maks. 2 tabele) – silnik=auto: PDF powstaje od razu na serwerze (kilka sekund).
- Dokument długi (ponad ok. 12000 znaków), z 3+ tabelami, 3+ obrazami albo komiks – backend wyśle go na GitHub Actions. W czacie pojawi się wtedy KARTA BUILDU zamiast pliku PDF: napisz użytkownikowi JEDNO zdanie, że ten dokument powstanie w chmurze i ma kliknąć „Rozpocznij build” na karcie – gotowy PDF pojawi się w karcie po 2–4 minutach. NIE obiecuj, że PDF jest już gotowy. Twoja odpowiedź NIGDY nie może być pusta – nawet gdy cała treść siedzi w tagu, zawsze dopisz to jedno zdanie do użytkownika.
- silnik=github wymuszaj tylko, gdy użytkownik sam chce „w chmurze”; silnik=lokalny – gdy chce „od razu” i dokument jest mały.
- Gazeta: tytuł gazety daj w tytule tagu, artykuły rozdzielaj nagłówkami ##, a pierwszy akapit każdego artykułu pogrub jako lead (**...**).
- Komiks (KOMIKS_V19 – backend robi z tego ilustrowany komiks z dymkami): w tagu dopisz strony=N wg życzenia użytkownika. Treść pisz DOKŁADNIE tak: strony rozdzielaj nagłówkami ## Strona 1, ## Strona 2, ..., a KAŻDY kadr to osobny nagłówek ### Kadr N, pod nim linia „Obraz: ..." ze SZCZEGÓŁOWYM promptem obrazka PO ANGIELSKU i pod nią ZAWSZE najpierw 1–2 zdania po polsku OPISU SYTUACJI (narracja: gdzie jesteśmy, co się w tym kadrze dzieje, co robią postaci – opis sytuacyjny jest OBOWIĄZKOWY w KAŻDYM kadrze, także w kadrze z dialogami; sama wymiana dialogów bez opisu to błąd formatu), a dopiero po opisie dialogi WYŁĄCZNIE w polskich cudzysłowach „..." poprzedzone imieniem mówcy i dwukropkiem (np. Patrick: „Oddawaj kiełbasę!"). Zaczynaj prompt Obraz: od rodzaju planu: „wide establishing shot” dla OGÓLNEJ SCENERII (krajobraz, miejsce akcji z daleka – taki kadr dostanie DUŻĄ ramkę) albo „close-up” dla ZBLIŻENIA na konkretną akcję lub emocje (np. jedzenie jabłka, twarz, łzy – zbliżenia trafiają do MNIEJSZYCH ramek). Prompt Obraz: pisz maksymalnie konkretnie (kto występuje i jak wygląda, gdzie, co robi, jakie emocje, na końcu „comic book art"), bo backend generuje z niego ilustrację AI i SPRAWDZA ją modelem wizyjnym względem tej koncepcji – obraz niezgodny (brak postaci, zamieniony główny obiekt, inna sceneria) jest odrzucany i generowany od nowa. Z opisu backend robi żółty boks narracji, a z dialogów dymki na obrazku – Postaci nazywaj imionami własnymi (np. księżniczka Róża i księżniczka Lilia), nigdy oznaczeniami literowymi w rodzaju „Księżniczka A”. Dialogi trzymaj KRÓTKIE (max 2 wypowiedzi w kadrze, po kilka słów). NIE pisz kilku kadrów w jednym akapicie i NIE opisuj komiksu streszczeniem. Liczba kadrów: zwykle 3–4 na stronę. NIE dołączaj żadnych bloczków ```svg ani własnych rysunków – ilustracje powstają wyłącznie z generatora obrazów AI; gdyby generator był niedostępny, kadr dostanie puste tło pod tekstem i też nie musisz nic z tym robić.
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

        # DOKUMENTY_V10: tabela z markdownu (| a | b | + wiersz separatora)
        if line.strip().startswith("|") and i + 1 < len(lines) and _is_md_table_separator(lines[i + 1]):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                if not _is_md_table_separator(lines[i]):
                    rows.append(_md_table_cells(lines[i]))
                i += 1
            if rows:
                cols = max(len(r) for r in rows)
                table = doc.add_table(rows=0, cols=cols)
                table.style = "Table Grid"
                for ri, row in enumerate(rows):
                    cells = table.add_row().cells
                    for ci in range(cols):
                        txt = row[ci] if ci < len(row) else ""
                        txt = re.sub(r"\*\*([^*]+)\*\*", r"\1", txt)
                        txt = re.sub(r"\*([^*]+)\*", r"\1", txt)
                        cells[ci].text = txt
                        if ri == 0:  # naglowek tabeli pogrubiony
                            for par in cells[ci].paragraphs:
                                for run in par.runs:
                                    run.bold = True
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
    wynik = buf.getvalue()
    # BRAMKA_DOK_V39: uszkodzony pakiet DOCX nie idzie do użytkownika.
    if not _docx_ok(wynik):
        raise RuntimeError("Wygenerowany dokument DOCX nie przeszedł kontroli jakości (uszkodzona struktura pliku).")
    return wynik


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




# --- DOKUMENTY_V10: profesjonalne PDF-y z HTML+CSS, dwa silniki renderowania ---
# WARIANT 2 (domyślny, dokumenty zwykle i średnie): render LOKALNIE na Space
#   przez WeasyPrint (HTML+CSS). Wymaga bibliotek Pango w obrazie Space'a -
#   patrz dołączony Dockerfile. Gdy WeasyPrint jest niedostępny (np. Space
#   jeszcze nie przebudowany na Docker), generator awaryjnie wraca do starego
#   FPDF, więc dokumenty działają dalej, tylko skromniej wyglądają.
# WARIANT 1 (dokumenty duże / złożone): ten sam HTML leci na GitHub Actions
#   jako build o targecie "pdf" (karta buildu w czacie, jak przy APK/EXE).
#   HF wtedy tylko zleca i odbiera gotowy PDF - render robi runner GitHuba.
# Wybór silnika: opcje w tagu GENERATE_PDF albo automat wg pdf_choose_engine().
#   [GENERATE_PDF: Tytuł | szablon=raport | czcionka=sans | silnik=auto]
import html as _html_mod
import threading as _doc_threading

# Space ma 2 vCPU - max 2 rendery lokalne naraz, kolejne czekają w kolejce,
# żeby generowanie dokumentów nie zadusiło czatu.
_DOC_RENDER_SEMAPHORE = _doc_threading.Semaphore(2)

DOC_TEMPLATES = {"raport", "gazeta", "komiks", "cv", "list"}
DOC_FONT_STACKS = {
    "sans": "'DejaVu Sans', 'Liberation Sans', sans-serif",
    "serif": "'DejaVu Serif', 'Liberation Serif', serif",
    "mono": "'DejaVu Sans Mono', 'Liberation Mono', monospace",
}
# Progi wariantu 1 (GitHub) przy silnik=auto:
DOC_GH_MIN_CHARS = 12000   # ~6-8 stron tekstu
DOC_GH_MIN_TABLES = 3
DOC_GH_MIN_IMAGES = 3


def _md_table_cells(line: str) -> list:
    s = (line or "").strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _is_md_table_separator(line: str) -> bool:
    if "|" not in (line or ""):
        return False
    cells = _md_table_cells(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", c) for c in cells)


def _md_inline(text: str) -> str:
    """Escape HTML + proste formatowanie markdown: obraz, link, **bold**, *italic*, `kod`."""
    s = _html_mod.escape(text or "", quote=True)
    s = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)", r'<img alt="\1" src="\2">', s)
    s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return s


def markdown_to_html(body: str) -> str:
    """Markdown -> HTML: nagłówki, listy, tabele, cytaty, kod, akapity, obrazy."""
    lines = (body or "").split("\n")
    out = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].rstrip()
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # blok kodu ```
        if stripped.startswith("```"):
            i += 1
            code = []
            while i < n and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1  # zamykające ```
            out.append("<pre><code>" + _html_mod.escape("\n".join(code)) + "</code></pre>")
            continue

        # tabela: wiersz | ... | a pod nim separator |---|
        if stripped.startswith("|") and i + 1 < n and _is_md_table_separator(lines[i + 1]):
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                if not _is_md_table_separator(lines[i]):
                    rows.append(_md_table_cells(lines[i]))
                i += 1
            if rows:
                cols = max(len(r) for r in rows)
                parts = ["<table>"]
                for ri, row in enumerate(rows):
                    tag = "th" if ri == 0 else "td"
                    parts.append("<tr>" + "".join(
                        f"<{tag}>{_md_inline(row[ci]) if ci < len(row) else ''}</{tag}>"
                        for ci in range(cols)) + "</tr>")
                parts.append("</table>")
                out.append("".join(parts))
            continue

        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", stripped):
            out.append("<hr>")
            i += 1
            continue

        m = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if m:
            level = len(m.group(1)) + 1  # tytuł dokumentu to h1, więc # -> h2 itd.
            out.append(f"<h{level}>{_md_inline(m.group(2))}</h{level}>")
            i += 1
            continue

        if stripped.startswith("> "):
            out.append(f"<blockquote>{_md_inline(stripped[2:])}</blockquote>")
            i += 1
            continue

        if re.match(r"^[-*]\s+", stripped):
            items = []
            while i < n and re.match(r"^[-*]\s+", lines[i].strip()):
                items.append("<li>" + _md_inline(re.sub(r"^[-*]\s+", "", lines[i].strip())) + "</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue

        if re.match(r"^\d+\.\s+", stripped):
            items = []
            while i < n and re.match(r"^\d+\.\s+", lines[i].strip()):
                items.append("<li>" + _md_inline(re.sub(r"^\d+\.\s+", "", lines[i].strip())) + "</li>")
                i += 1
            out.append("<ol>" + "".join(items) + "</ol>")
            continue

        # akapit: łączymy kolejne zwykłe linie
        para = [stripped]
        i += 1
        while i < n:
            nxt = lines[i].strip()
            if not nxt or nxt.startswith(("#", ">", "```", "|")) \
                    or re.match(r"^[-*]\s+", nxt) or re.match(r"^\d+\.\s+", nxt):
                break
            para.append(nxt)
            i += 1
        out.append("<p>" + _md_inline(" ".join(para)) + "</p>")

    return "\n".join(out)


def _doc_css(template: str, font_stack: str) -> str:
    css = """
@page { size: A4; margin: 2cm 1.8cm 2.2cm 1.8cm;
        @bottom-center { content: counter(page) " / " counter(pages);
                         font-size: 8.5pt; color: #8a8f98; } }
* { box-sizing: border-box; }
body { font-family: __FONT__; font-size: 11pt; line-height: 1.55; color: #1d2430; }
h1.doc-title { font-size: 20pt; text-align: center; margin: 0 0 18pt 0; color: #16324f; }
h2 { font-size: 15pt; color: #16324f; margin: 16pt 0 6pt 0; page-break-after: avoid; }
h3 { font-size: 12.5pt; color: #24507c; margin: 12pt 0 4pt 0; page-break-after: avoid; }
h4 { font-size: 11.5pt; margin: 10pt 0 4pt 0; page-break-after: avoid; }
p { margin: 0 0 7pt 0; text-align: justify; }
ul, ol { margin: 0 0 7pt 0; }
table { width: 100%; border-collapse: collapse; margin: 8pt 0 12pt 0; font-size: 10pt; }
th { background: #e8eff7; color: #16324f; border: 0.6pt solid #9db4cc; padding: 4pt 6pt; text-align: left; }
td { border: 0.6pt solid #c3d0e0; padding: 4pt 6pt; vertical-align: top; }
tr { page-break-inside: avoid; }
blockquote { border-left: 3pt solid #bfe3ff; margin: 8pt 0; padding: 2pt 10pt; color: #445; }
pre { background: #f0f2f5; border-radius: 4pt; padding: 8pt; font-size: 9pt; white-space: pre-wrap; }
code { font-family: 'DejaVu Sans Mono', monospace; font-size: 9.5pt; }
img { max-width: 100%; }
hr { border: none; border-top: 0.8pt solid #c3d0e0; margin: 12pt 0; }
a { color: #24507c; text-decoration: none; }
"""
    if template == "gazeta":
        css += """
@page { margin: 1.6cm 1.5cm 2cm 1.5cm; }
h1.doc-title { font-size: 27pt; text-transform: uppercase; letter-spacing: 0.5pt;
               border-bottom: 2.5pt solid #16324f; padding-bottom: 8pt; }
.content { columns: 2; column-gap: 26pt; column-rule: 0.5pt solid #d4dbe4; }
.content p { font-size: 10.5pt; }
.content h2 { column-span: all; text-align: center; }
"""
    elif template == "komiks":
        css += """
@page { margin: 1.4cm; }
h1.doc-title { font-size: 22pt; }
.content h3 { background: #16324f; color: #fff; padding: 5pt 8pt; border-radius: 4pt;
              margin-top: 14pt; }
.content img { border: 2.2pt solid #16324f; border-radius: 4pt; display: block;
               margin: 6pt auto; }
.content p { text-align: center; font-style: italic; }
"""
    elif template == "cv":
        css += """
body { font-size: 10.5pt; }
h1.doc-title { text-align: left; font-size: 22pt; margin-bottom: 4pt; }
h2 { border-bottom: 1.2pt solid #bfe3ff; padding-bottom: 2pt; text-transform: uppercase;
     font-size: 12.5pt; letter-spacing: 0.4pt; }
p { text-align: left; }
"""
    elif template == "list":
        css += """
body { font-size: 11.5pt; line-height: 1.65; }
h1.doc-title { text-align: left; font-size: 16pt; }
p { text-align: left; }
"""
    return css.replace("__FONT__", font_stack)


def build_document_html(title: str, body: str, template: str = "raport", font: str = "sans") -> str:
    """Kompletny, samodzielny dokument HTML (do WeasyPrint lokalnie i na GitHubie)."""
    template = (template or "raport").strip().lower()
    if template not in DOC_TEMPLATES:
        template = "raport"
    font = (font or "sans").strip().lower()
    font_stack = DOC_FONT_STACKS.get(font, DOC_FONT_STACKS["sans"])
    content = markdown_to_html(body)
    return (
        "<!DOCTYPE html>\n<html lang=\"pl\">\n<head>\n<meta charset=\"utf-8\">\n"
        f"<title>{_html_mod.escape(title or 'Dokument')}</title>\n"
        f"<style>{_doc_css(template, font_stack)}</style>\n</head>\n<body>\n"
        f"<h1 class=\"doc-title\">{_html_mod.escape(title or 'Dokument')}</h1>\n"
        f"<div class=\"content\">\n{content}\n</div>\n</body>\n</html>"
    )


def render_pdf_weasyprint(html_doc: str) -> bytes:
    """WARIANT 2: render na miejscu (Space). Rzuca wyjątek, gdy brak WeasyPrint/Pango."""
    try:
        import weasyprint
    except Exception as e:
        raise RuntimeError(f"WeasyPrint niedostępny na tym Space ({e})")
    with _DOC_RENDER_SEMAPHORE:
        data = weasyprint.HTML(string=html_doc).write_pdf()
    if not data:
        raise RuntimeError("WeasyPrint zwrócił pusty PDF")
    return data


def parse_pdf_tag_args(raw: str):
    """'Tytuł | szablon=gazeta | czcionka=serif | silnik=github' -> (tytuł, opcje)."""
    parts = [p.strip() for p in (raw or "").split("|")]
    title = parts[0] if parts and parts[0] else "Dokument"
    opts = {"szablon": "raport", "czcionka": "sans", "silnik": "auto", "strony": "", "grafika": "auto"}
    key_alias = {"szablon": "szablon", "template": "szablon", "styl": "szablon",
                 "czcionka": "czcionka", "font": "czcionka",
                 "silnik": "silnik", "engine": "silnik",
                 "strony": "strony", "pages": "strony", "stron": "strony",
                 "grafika": "grafika", "rysunek": "grafika", "obrazy": "grafika"}
    for part in parts[1:]:
        if "=" in part:
            k, v = part.split("=", 1)
            k = key_alias.get(k.strip().lower())
            if k:
                opts[k] = v.strip().lower()
    eng_alias = {"lokalny": "local", "local": "local", "space": "local",
                 "github": "github", "chmura": "github", "cloud": "github",
                 "auto": "auto"}
    opts["silnik"] = eng_alias.get(opts["silnik"], "auto")
    return title, opts


def pdf_choose_engine(body: str, template: str, engine_opt: str):
    """Zwraca (silnik, powód): 'local' = wariant 2 (Space), 'github' = wariant 1."""
    if engine_opt in ("local", "github"):
        return engine_opt, "silnik wymuszony w tagu"
    if (template or "").strip().lower() == "komiks":
        return "github", "szablon komiks (układ kadrów + obrazy) generuje GitHub"
    if len(body or "") > DOC_GH_MIN_CHARS:
        return "github", f"długi dokument ({len(body)} znaków > {DOC_GH_MIN_CHARS})"
    tables = sum(1 for ln in (body or "").split("\n") if _is_md_table_separator(ln))
    if tables >= DOC_GH_MIN_TABLES:
        return "github", f"dużo tabel ({tables} >= {DOC_GH_MIN_TABLES})"
    images = len(re.findall(r"!\[|<img", body or ""))
    if images >= DOC_GH_MIN_IMAGES:
        return "github", f"dużo obrazów ({images} >= {DOC_GH_MIN_IMAGES})"
    return "local", "dokument w normalnym zakresie - render na Space"


def _doc_safe_base(title: str) -> str:
    safe = re.sub(r"[^\w\s\-ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]", "", title or "")[:40].strip() or "dokument"
    return safe.replace(" ", "_")



# --- KOMIKS_V11/V12: prawdziwy komiks ilustrowany, nie opis tekstem ---
# V12 dokłada: dymki z dialogami i żółte boksy narracji jako nakładki HTML/CSS
# na obrazkach (tekst generowany przez model obrazu byłby nieczytelny) oraz
# lepszą jakość ilustracji: model pisze per kadr linię "Obraz: <prompt EN>",
# a adres generatora dostaje większą rozdzielczość i enhance=true.
# Poprzednia wersja polegała na modelu: miał sam pisać kadry nagłówkami i dołączać
# obrazy osobnymi tagami - w praktyce pisał panele jednym akapitem i bez obrazów,
# więc wychodziła 1 strona tekstu. Teraz BACKEND robi robotę sam:
#   1) dzieli treść na kadry (też zapisane w jednym akapicie jako **Panel N**),
#   2) dzieli kadry na strony (markery "Strona N" albo opcja strony=N w tagu),
#   3) do KAŻDEGO kadru dokłada ilustrację:
#      - wariant github: <img> z adresem generatora obrazów (Pollinations) -
#        obraz powstaje na runnerze przy renderowaniu, HF nic nie generuje;
#      - wariant local: backend generuje obraz swoim generatorem i wbudowuje
#        go w HTML jako JPEG data-URI (limit kadrów, bo to trwa).
COMIC_MAX_PANELS = 12
COMIC_LOCAL_MAX_IMAGES = 8

_PANEL_MARKER = re.compile(
    # marker MUSI działać też w środku akapitu - model pisze czasem wszystkie
    # kadry jedną linią ("...zachwytem. **Panel 2** – ..."), nie tylko od nowej linii.
    # KOMIKS_V18: po numerze wymagamy jednak znaku nagłówkowego (pogrubienie,
    # myślnik, dwukropek), kropki przed wielką literą albo końca linii - zdanie
    # opisu zaczynające się od "Kadr 1 z..." NIE jest nagłówkiem i nie wolno
    # go dzielić na osobny, pusty kadr.
    r"(?:#{1,4}\s*)?(?:\*\*)?\s*(?i:Panel|Kadr)\s*(\d+)"
    r"(?:\s*(?:\*\*)?\s*[–—\-:]|\s*(?:\*\*)?\s*\.\s+(?=[A-ZŁŚŻŹĆŃÓĄĘ„\"])|\s*(?:\*\*)?\s*(?=\n|$)|\s+(?=[A-ZŁŚŻŹĆŃÓĄĘ„\"]))\s*")
_PAGE_MARKER = re.compile(r"(?m)^\s*#{0,4}\s*Strona\s+(\d+)\s*$", re.IGNORECASE)


def _clean_panel_text(t: str) -> str:
    t = re.sub(r"\*\*([^*]+)\*\*", r"\1", t or "")
    t = re.sub(r"\*([^*]+)\*", r"\1", t)
    t = re.sub(r"^#{1,4}\s*", "", t.strip())
    t = re.sub(r"\s+", " ", t).strip(" \n-–—")
    return t[:400]


_IMG_PROMPT_RE = re.compile(r"(?:Obraz|Grafika|Prompt|Image)\s*:\s*(.+?)(?=\n|$)", re.IGNORECASE)


_SVG_BLOCK_RE = re.compile(r"```(?:svg)?\s*(<svg\b.*?</svg>)\s*```", re.DOTALL | re.IGNORECASE)
_SVG_ANY_RE = re.compile(r"(<svg\b.*?</svg>)", re.DOTALL | re.IGNORECASE)
_SVG_SHAPES = ("<rect", "<circle", "<ellipse", "<path", "<polygon", "<line", "<polyline", "<text")


def _parse_panel(raw: str) -> dict:
    """Surowy kadr -> {"caption", "prompt", "svg"}.

    - linia "Obraz: ..." -> angielski prompt dla generatora obrazów AI,
    - bloczek ```svg ... ``` jest ODRZUCANY (KOMIKS_V19: ilustracje robi
      wyłącznie generator obrazów AI; model nie rysuje już wektorowo),
    - reszta -> podpis (narracja + dialogi na dymki)."""
    raw = raw or ""
    svg = None
    m = _SVG_BLOCK_RE.search(raw) or _SVG_ANY_RE.search(raw)
    if m:
        raw = (raw[:m.start()] + " " + raw[m.end():]).strip()
    prompt = None
    m = _IMG_PROMPT_RE.search(raw)
    if m:
        # KOMIKS_V28: linia "Obraz:" ZAWSZE znika z podpisu (wczesniej krotki
        # prompt <10 znakow zostawal i ladowal w boksie narracji); jako promptu
        # generatora uzywamy jej tylko, gdy jest konkretna (>=10 znakow).
        if len(m.group(1).strip()) >= 10:
            prompt = m.group(1).strip().strip("*").strip()[:500]
        raw = (raw[:m.start()] + " " + raw[m.end():]).strip()
    return {"caption": _clean_panel_text(raw), "prompt": prompt, "svg": svg}


def _split_panels_in_segment(seg: str) -> list:
    marks = list(_PANEL_MARKER.finditer(seg or ""))
    panels = []
    if marks:
        for k, m in enumerate(marks):
            end = marks[k + 1].start() if k + 1 < len(marks) else len(seg)
            panel = _parse_panel(seg[m.end():end])
            if panel["caption"] or panel["prompt"]:
                panels.append(panel)
    else:
        for para in re.split(r"\n\s*\n", seg or ""):
            panel = _parse_panel(para)
            if panel["caption"] or panel["prompt"]:
                panels.append(panel)
    return panels


def _split_comic_pages(body: str, requested_pages: int = 0) -> list:
    """Treść komiksu -> lista stron, każda = lista opisów kadrów."""
    body = re.split(r"(?m)^\s*-{3,}\s*$", body or "")[0]  # odetnij stopkę po ---
    parts = _PAGE_MARKER.split(body)
    # parts: [przed, "1", tresc1, "2", tresc2, ...] gdy sa markery stron
    pages = []
    if len(parts) > 2:
        pre = _split_panels_in_segment(parts[0])
        if pre:
            pages.append(pre)
        for i in range(1, len(parts) - 1, 2):
            seg_panels = _split_panels_in_segment(parts[i + 1])
            if seg_panels:
                pages.append(seg_panels)
    else:
        all_panels = _split_panels_in_segment(body)
        if all_panels:
            if requested_pages and requested_pages > 0:
                per = max(1, -(-len(all_panels) // requested_pages))  # ceil
                pages = [all_panels[i:i + per] for i in range(0, len(all_panels), per)]
            else:
                pages = [all_panels[i:i + 3] for i in range(0, len(all_panels), 3)]
    flat = [p for pg in pages for p in pg][:COMIC_MAX_PANELS]
    # przytnij strony do limitu kadrów zachowując kolejność
    out, left = [], COMIC_MAX_PANELS
    for pg in pages:
        take = pg[:left]
        if take:
            out.append(take)
            left -= len(take)
        if left <= 0:
            break
    return out


_QUOTE_RE = re.compile(r'„([^"”]+)["”]|"([^"]+)"|«([^»]+)»')
_SPEAKER_RE = re.compile(
    r"([A-ZŁŚŻŹĆŃÓĄĘ][\włśżźćńóąęŁŚŻŹĆŃÓĄĘ]*"
    r"(?:\s+(?:[A-ZŁŚŻŹĆŃÓĄĘ][\włśżźćńóąęŁŚŻŹĆŃÓĄĘ]*|[A-ZŁŚŻŹĆŃÓĄĘ])){0,2})"
    r"\s*(?:mówi|krzyczy|pyta|woła|szepcze|odpowiada|mruczy)?\s*:\s*$")


def _split_speech(caption: str):
    """Podpis kadru -> (narracja, [(mówca, tekst), ...]) - do dymków i boksu narracji.

    Dialogi w cudzysłowach stają się dymkami na obrazku, reszta tekstu żółtym
    boksem narracji u góry kadru - jak w prawdziwym komiksie. Mówca wykrywany
    z zapisu "Imię: „..."" / "Imię krzyczy: „...""."""
    speeches, parts, last = [], [], 0
    for m in _QUOTE_RE.finditer(caption or ""):
        text = next((g for g in m.groups() if g is not None), "").strip()
        before = caption[last:m.start()]
        sm = _SPEAKER_RE.search(before)
        speaker = ""
        if sm:
            speaker = sm.group(1).strip()
            before = before[:sm.start()]
        parts.append(before)
        if text:
            speeches.append((speaker, text))
        last = m.end()
    parts.append((caption or "")[last:])
    narration = " ".join(parts)
    narration = re.sub(r"\s+(?:i\s+)?(?:mówi|krzyczy|pyta|woła|szepcze|odpowiada)\s*:\s*", " ", narration)
    narration = re.sub(r"\s*:\s*(?=[.,!?]|$)", " ", narration)
    narration = re.sub(r"\s+", " ", narration).strip(" .,-–—")
    if narration:
        narration = narration[0].upper() + narration[1:]
        if narration[-1] not in ".!?":
            narration += "."
    # KOMIKS_V27: narracja ma byc OPISem sytuacji. Gdy po odcieciu dialogow
    # zostaly same nazwy/imiona (np. "Księżniczka." po mowcach "Księżniczka A:"),
    # to nie jest opis - boks narracji pomijamy.
    _words = [w.strip(".,!?;:") for w in narration.split() if w.strip(".,!?;:")]
    if _words and len(_words) <= 3 and all(w[0].isupper() for w in _words):
        narration = ""
    return narration, speeches[:2]


def _comic_image_url(panel: dict, idx: int, seed: int = None) -> str:
    # V12: wiekszy obraz (1024x768) + enhance=true (generator sam wzmacnia
    # prompt) + bogatszy opis stylu. Gdy model dal linie "Obraz:", to ona jest
    # promptem; inaczej awaryjnie podpis (po polsku - enhance czesciowo to tlumaczy).
    if panel.get("prompt"):
        prompt = (panel["prompt"] +
                  ", vibrant comic book illustration, bold black outlines, cel shading, "
                  "rich colors, high detail, wide shot, full figures inside the frame, "
                  "heads fully visible, no text, no words, no letters, no speech bubbles")
    else:
        prompt = ((panel.get("caption") or "")[:300] +
                  ", colorful comic book panel illustration, cartoon style, "
                  "expressive scene, wide shot, full figures inside the frame, heads fully "
                  "visible, no text, no speech bubbles, no words, no letters")
    q = requests.utils.quote(prompt[:700])
    return (f"https://image.pollinations.ai/prompt/{q}"
            f"?width=1024&height=640&nologo=true&enhance=true&seed={41 + idx if seed is None else seed}")


def _panel_html(src, panel: dict, number: int = 0, flex: int = 1, stacked: bool = False) -> str:
    """Kadr: obraz AI + nakładki - numerek, narracja, dymki. Bez zaakceptowanego
    obrazu (generator niedostępny albo obraz odrzucony przez kontrolę jakości)
    NIE ma dymków i NIE ma dialogu w ogóle (KOMIKS_V21/V22, decyzja
    użytkownika): gdy dialogu nie da się zainscenizować na obrazie, kadr
    pokazuje wyłącznie opis sytuacji (tekst narracji) - wypowiedzi z takiego
    kadru są pomijane, nie dopisywane do narracji."""
    narration, speeches = _split_speech(panel.get("caption") or "")
    if not src:
        # KOMIKS_V30 (regula uzytkownika): kadr bez zaakceptowanego obrazu to
        # TYLKO zolta ramka z komentarzem (opis sytuacji) - bez duzej pustej
        # ramki z pustym polem po zdjeciu. Dialogi nadal pomijane (V22).
        inner = ""
        if number:
            inner += f"<div class=\"pnum\">{number}</div>"
        if narration:
            inner += f"<div class=\"txt\">{_html_mod.escape(narration)}</div>"
        if stacked:
            return f"<div class=\"txtnote\">{inner}</div>"
        return f"<div class=\"txtnote\" style=\"flex:{flex} 1 0\">{inner}</div>"
    inner = f"<img src=\"{src}\" alt=\"\">"
    cls = "panel"
    if number:
        inner += f"<div class=\"pnum\">{number}</div>"
    if narration:
        inner += f"<div class=\"narr\">{_html_mod.escape(narration)}</div>"
    if src:
        for i, (speaker, text) in enumerate(speeches):
            side = "left" if i == 0 else "right"
            who = f"<span class=\"who\">{_html_mod.escape(speaker)}</span>" if speaker else ""
            inner += f"<div class=\"bubble {side}\">{who}{_html_mod.escape(text)}</div>"
    return f"<div class=\"{cls}\" style=\"flex:{flex} 1 0\">{inner}</div>"


def _panoramize(raw: bytes, aspect: float) -> bytes:
    """KOMIKS_V27: dopasuj obraz do proporcji ramki kadru BEZ ucinania tresci.
    Generatory daja obrazy 1:1 albo 4:3, a ramki sa panoramiczne - object-fit:
    cover ucinal wtedy glowy postaci. Tu caly obraz zostaje w srodku, a boki
    wypelnia jego wlasna rozmyta, przyciemniona kopia (efekt kinowej ramki).
    Przy bledzie wraca zwykly JPEG (stare zachowanie)."""
    try:
        from PIL import ImageFilter, ImageEnhance, ImageOps
        img = Image.open(BytesIO(raw)).convert("RGB")
        W = 1200
        H = max(200, int(round(W / max(0.7, min(5.2, aspect)))))
        bg = ImageOps.fit(img, (W, H), Image.Resampling.LANCZOS)
        bg = bg.filter(ImageFilter.GaussianBlur(26))
        bg = ImageEnhance.Brightness(bg).enhance(0.7)
        fg = img.copy()
        fg.thumbnail((W, H), Image.Resampling.LANCZOS)
        bg.paste(fg, ((W - fg.width) // 2, (H - fg.height) // 2))
        buf = BytesIO()
        bg.save(buf, format="JPEG", quality=80)
        return buf.getvalue()
    except Exception:
        return _jpeg_bytes(raw)


def _jpeg_bytes(raw: bytes, max_w: int = 900, quality: int = 74) -> bytes:
    img = Image.open(BytesIO(raw)).convert("RGB")
    img.thumbnail((max_w, max_w), Image.Resampling.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _jpeg_data_uri(raw: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(_jpeg_bytes(raw, 880, 80)).decode("ascii")


def _comic_css(font_stack: str) -> str:
    return """
@page { size: A4; margin: 1.3cm 1.3cm 1.8cm 1.3cm;
        @bottom-center { content: counter(page) " / " counter(pages);
                         font-size: 8.5pt; color: #8a8f98; } }
body { font-family: __FONT__; color: #1d2430; }
h1.doc-title { font-size: 21pt; text-align: center; color: #16324f; margin: 0 0 10pt 0; }
.page { page-break-after: always; }
.page:last-child { page-break-after: auto; }
.panel { position: relative; border: 2.4pt solid #16324f; border-radius: 6pt;
         margin-bottom: 9pt; page-break-inside: avoid; background: #141a24; overflow: hidden; }
.panel img { width: 100%; height: 100%; object-fit: cover; display: block; }
.prow { display: flex; margin-bottom: 9pt; page-break-inside: avoid; }
.prow:last-child { margin-bottom: 0; }
.prow .panel { flex: 1 1 0; height: 100%; margin-bottom: 0; box-sizing: border-box; }
.prow .panel + .panel { margin-left: 9pt; }
.prow > * + * { margin-left: 9pt; }
.txtnote { position: relative; align-self: flex-start; background: #fff3b4; border: 2pt solid #16324f;
           border-radius: 6pt; padding: 7pt 9pt; box-sizing: border-box; overflow: hidden; }
.txtnote .txt { font-size: 10.5pt; line-height: 1.35; }
.vstack { display: flex; flex-direction: column; }
.vstack .panel { height: auto; }
.vstack .txtnote { flex: 0 0 auto; align-self: stretch; margin-bottom: 9pt; }
.narr { position: absolute; top: 5pt; left: 5pt; right: 5pt; background: rgba(255, 244, 180, 0.95);
        border: 1.6pt solid #16324f; padding: 3pt 6pt; font-size: 9.5pt; }
.bubble { position: absolute; bottom: 8pt; max-width: 47%; background: #ffffff;
          border: 1.8pt solid #16324f; border-radius: 11pt; padding: 4pt 8pt;
          font-size: 9.8pt; line-height: 1.25; }
.bubble.left { left: 7pt; }
.bubble.right { right: 7pt; }
.bubble::after { content: ""; position: absolute; bottom: -7.5pt; width: 0; height: 0;
                 border: 6pt solid transparent; border-top-color: #16324f; border-bottom: 0; }
.bubble.left::after { left: 18pt; }
.bubble.right::after { right: 18pt; }
.bubble .who { display: block; font-size: 7.5pt; font-weight: bold; letter-spacing: 0.4pt;
               text-transform: uppercase; color: #16324f; }
.panel .noimg { width: 100%; height: 100%; background: #f6f1e2; }
.pnum { position: absolute; top: 5pt; right: 5pt; min-width: 13pt; height: 13pt; line-height: 13pt;
        text-align: center; background: #16324f; color: #fff; border-radius: 50%;
        font-size: 8.5pt; font-weight: bold; padding: 1pt; }
.narr + .pnum { right: 5pt; }
.panels-1 .panel img, .panels-1 .panel .noimg { height: 15cm; }
.panels-2 .panel img, .panels-2 .panel .noimg { height: 9.6cm; }
.panels-3 .panel img, .panels-3 .panel .noimg { height: 6.9cm; }
.panels-4 .panel img, .panels-4 .panel .noimg { height: 4.9cm; }
.panels-4 .bubble, .panels-4 .narr { font-size: 8.8pt; }
.panels-5 .panel img, .panels-6 .panel img, .panels-5 .panel .noimg, .panels-6 .panel .noimg { height: 3.6cm; }
.panels-5 .bubble, .panels-6 .bubble, .panels-5 .narr, .panels-6 .narr { font-size: 8.2pt; }
""".replace("__FONT__", font_stack)



def _check_image_url(url: str, attempts: int = 2) -> bool:
    """Czy generator obrazów naprawdę zwraca ten obraz? (status + typ + rozmiar)"""
    for _ in range(attempts):
        try:
            r = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
            if (r.status_code == 200
                    and (r.headers.get("content-type") or "").startswith("image")
                    and len(r.content) > 4000):
                return True
        except Exception:
            pass
    return False


def _download_image(url: str):
    try:
        r = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200 and len(r.content) > 4000:
            return r.content
    except Exception:
        pass
    return None



# --- KOMIKS_V18: kontrola jakości obrazów AI ---
# Pomysł użytkownika: agent ma SPRAWDZAĆ, czy wygenerowany obraz zgadza się
# z koncepcją kadru. Pętla na kadr: generuj (Pollinations, seed) -> pobierz ->
# model wizyjny (od WIZJA_V23: dowolny dostawca z puli kluczy - call_llm
# przekazuje obrazy wszystkim multimodalnym modelom) ocenia zgodność kadru
# z promptem/opisem w skali 0-100 ->
# za nisko = ponów z innym seedem (do QC_MAX_ATTEMPTS prób), zostaje najlepsza
# próba. Zweryfikowane obrazy backend wbudowuje sam (data-URI albo osobne
# pliki w zadaniu GitHub), więc do PDF-a trafia dokładnie to, co przeszło
# kontrolę - a nie to, co runner akurat pobierze. Bez klucza Gemini kontrola
# jest wyłączona (pierwszy pobrany obraz przechodzi jak dawniej).
QC_MIN_SCORE = 50  # KOMIKS_V24: próg obniżony z 70 na 50 decyzją użytkownika (obraz od 50/100 jest akceptowany)
QC_MAX_ATTEMPTS = 2  # KOMIKS_V25: pierwsza proba + tylko JEDNO ponowienie (decyzja uzytkownika)
QC_PANEL_BUDGET_S = 120  # KOMIKS_V25: na jeden obraz czekamy maks. 2 minuty
QC_INLINE_LIMIT = 600_000  # bajty JPEG; powyżej tego obrazy jadą jako osobne pliki zadania


def _qc_keys():
    """Klucze do oceny wizyjnej: od WIZJA_V23 wszyscy dostawcy przyjmuja
    obrazy, wiec bierzemy cala pule. DAWKOWANIE_V38: w kolejności priorytetu
    puli (szybcy dostawcy najpierw, Gemini na końcu) – ocena kadru to małe,
    mechaniczne zapytanie, więc nie zużywamy na nią najrzadszych limitów
    Gemini, potrzebnych do dużych zadań i czatu."""
    return [k for k in (globals().get("api_keys") or []) if isinstance(k, str) and k]


def _qc_vision_score(img_bytes, panel):
    """Ocena wizyjna: (score 0-100, braki) albo None, gdy wizja niedostępna."""
    keys = _qc_keys()
    if not keys:
        return None
    concept = (panel.get("prompt") or "").strip()
    opis = (panel.get("caption") or "").strip()
    pytanie = (
        "Oceń OGÓLNĄ zgodność tego obrazu z koncepcją kadru komiksu.\n"
        f"Koncepcja (prompt): {concept}\nOpis kadru: {opis}\n"
        "Liczą się TYLKO trzy rzeczy: (1) czy są główne postacie - właściwy rodzaj "
        "i mniej więcej właściwa liczba, (2) czy jest główny obiekt akcji, "
        "(3) czy miejsce/sceneria jest ogólnie właściwa. Gdy te trzy rzeczy się "
        "zgadzają, dawaj WYSOKĄ ocenę (70-100) - przykład: koncepcja 'dwie "
        "księżniczki i jabłko', a na obrazie dwie kobiety i owoc = zgodne, akceptuj. "
        "NIE obniżaj oceny za: pozy i gesty postaci, mimikę, szczegóły ubioru, brak "
        "drobnych rekwizytów i drugoplanowych elementów, drobne różnice tła, styl "
        "rysunku. Obniżaj mocno TYLKO za: brak głównej postaci, wyraźnie złą liczbę "
        "głównych postaci, brak głównego obiektu albo jego zamianę na inny "
        "(np. ananas zamiast kiełbasy), zupełnie inną scenę.\n"
        'Zwróć WYŁĄCZNIE JSON: {"score": <0-100>, "braki": "<krótko: co jest nie tak, puste gdy OK>"}'
    )
    try:
        img = Image.open(BytesIO(img_bytes))
        img.load()
    except Exception:
        return None
    msgs = [{"role": "user", "parts": [pytanie, img]}]
    for k in keys[:4]:
        try:
            out = call_llm(k, msgs,
                           "Jesteś kontrolerem jakości ilustracji. Odpowiadaj WYŁĄCZNIE obiektem JSON.",
                           max_out=200)
            m = re.search(r"\{.*\}", out or "", re.DOTALL)
            if m:
                d = json.loads(m.group(0))
                return max(0, min(100, int(d.get("score", 0)))), str(d.get("braki", ""))[:200]
        except Exception as e:
            print(f"KOMIKS_V18 QC: błąd wizji ({e})")
    return None


def _qc_panel_image(panel, idx, deadline):
    """Najlepsze bajty obrazu kadru po kontroli jakości (albo None).

    KOMIKS_V25 (reguły użytkownika po analizie wolnego buildu):
    - głównie Pollinations: maks. QC_MAX_ATTEMPTS prób (pierwsza + jedno
      ponowienie), każda oceniana pod kątem OGÓLNEJ zgodności z koncepcją;
    - obraz pobrany, ale odrzucony po ponownej próbie = kadr tylko z tekstem
      (bez objeżdżania kolejnych silników - to one zjadały czas);
    - AI Horde TYLKO w ostateczności, gdy Pollinations jest nieaktywne
      (nie udało się pobrać ŻADNEGO obrazu), raz i z krótszą kolejką;
    - całość zamyka się w budżecie deadline (120 s na kadr).
    """
    best = None  # (score, raw)
    qc_on = bool(_qc_keys())
    got_image = False
    for attempt in range(QC_MAX_ATTEMPTS):
        if attempt and time.time() > deadline:
            break
        raw = _download_image(_comic_image_url(panel, idx, seed=41 + idx + attempt * 101))
        if not raw:
            continue
        got_image = True
        if not qc_on:
            return raw
        verdict = _qc_vision_score(raw, panel)
        if verdict is None:
            return raw  # wizja padła - nie blokujemy komiksu
        score, braki = verdict
        print(f"KOMIKS_V18 QC kadr {idx + 1}, próba {attempt + 1}: {score}/100 {braki}")
        if best is None or score > best[0]:
            best = (score, raw)
        if score >= QC_MIN_SCORE:
            break
    if best and best[0] >= QC_MIN_SCORE:
        return best[1]
    if got_image:
        # Pollinations działało, ale obraz niezgodny z koncepcją nawet po
        # ponownej próbie - kadr tylko z tekstem (KOMIKS_V20), bez Hordy.
        print(f"KOMIKS_V25 kadr {idx + 1}: odrzucono po {QC_MAX_ATTEMPTS} próbach "
              f"(najlepsza {best[0] if best else '-'}/100) - kadr tylko z tekstem.")
        return None
    # Pollinations nieaktywne (zero pobranych obrazów) - ostateczność: AI Horde.
    if time.time() < deadline - 15:
        try:
            raw_h = _generate_ai_horde(
                panel.get("prompt") or (panel.get("caption") or "")[:300],
                max_polls=30, width=768, height=512)  # ~90 s kolejki; format panoramiczny
            if raw_h:
                if not qc_on:
                    return raw_h
                verdict_h = _qc_vision_score(raw_h, panel)
                if verdict_h is None or verdict_h[0] >= QC_MIN_SCORE:
                    print(f"KOMIKS_V25 kadr {idx + 1}: obraz z AI Horde przyjęty ({verdict_h[0] if verdict_h else '-'}/100).")
                    return raw_h
                print(f"KOMIKS_V25 kadr {idx + 1}: AI Horde oceniona {verdict_h[0]}/100 - odrzucam.")
        except Exception as e:
            print(f"KOMIKS_V25 kadr {idx + 1}: AI Horde niedostępna ({e})")
    return None

# KOMIKS_V29 (regula uzytkownika): ogolna sceneria -> DUZE ramki, zblizenia
# na akcje/emocje -> MALE ramki. Rodzaj planu backend rozpoznaje z promptu
# Obraz: i podpisu kadru (slowa-klucze PL/EN); instrukcja modelu kaze mu
# zaczynac prompty scenerii od "wide establishing shot", a zblizen od
# "close-up", wiec sygnal jest zwykle jednoznaczny.
_PLAN_WIDE = (("wide establishing shot", 3), ("establishing shot", 3),
              ("wide shot", 3), ("aerial view", 2), ("landscape", 1),
              ("panorama", 1), ("scenery", 1), ("valley", 1), ("village", 1),
              ("kingdom", 1), ("castle", 1), ("forest", 1), ("mountains", 1),
              ("town", 1), ("river", 1), ("meadow", 1), ("krajobraz", 1),
              ("dolina", 1), ("zamek", 1), ("widok na", 1))
_PLAN_CLOSE = (("close-up", 3), ("closeup", 3), ("close up", 3),
               ("facial", 1), ("expression", 1), ("tears", 1), ("crying", 1),
               ("eating", 1), ("biting", 1), ("chewing", 1), ("her face", 1),
               ("his face", 1), ("uśmiech", 1), ("łzy", 1), ("płacze", 1),
               ("zjada", 1), ("gryzie", 1), ("twarz", 1), ("emocj", 1))


def _panel_kind(panel):
    """Rodzaj planu kadru: 'wide' (sceneria), 'close' (zbliżenie), 'mid'."""
    txt = ((panel.get("prompt") or "") + " " + (panel.get("caption") or "")).lower()
    w = sum(wt for ph, wt in _PLAN_WIDE if ph in txt)
    c = sum(wt for ph, wt in _PLAN_CLOSE if ph in txt)
    if w > c:
        return "wide"
    if c > w:
        return "close"
    return "mid"


# KOMIKS_V28: kreatywne uklady stron jak w prawdziwych komiksach - ramki
# roznych rozmiarow i ksztaltow (szerokie pasy, kwadraty obok siebie, duzy
# kadr + male). Backend wybiera szablon strony pod RZECZYWISTE proporcje
# wygenerowanych obrazow (kadr kwadratowy dostaje kwadratowy slot itd.),
# z drobna deterministyczna domieszka roznorodnosci, zeby kolejne strony
# nie wygladaly identycznie. Wiersz = (wysokosc_cm, [flex kadrow]).
_COMIC_LAYOUTS = {
    1: [[(15.0, [1])]],
    2: [[(11.2, [1]), (11.2, [1])],
        [(11.6, [1, 1])],
        [(8.8, [1]), (8.8, [1])],
        [(13.6, [1]), (7.2, [1])],
        [(7.2, [1]), (13.6, [1])]],
    3: [[(7.4, [1]), (8.6, [1, 1])],
        [(8.6, [1, 1]), (7.4, [1])],
        [(6.9, [1]), (6.9, [1]), (6.9, [1])],
        [(10.2, [1]), (6.6, [1, 1])],
        [(6.8, [2, 1]), (7.6, [1])],
        [(6.4, [1]), (10.4, [1]), (6.4, [1])]],
    4: [[(8.8, [1, 1]), (8.8, [1, 1])],
        [(6.6, [1]), (7.8, [1, 1]), (6.6, [1])],
        [(9.8, [1]), (7.0, [1, 1]), (7.0, [1])],
        [(5.9, [1]), (5.9, [1]), (5.9, [1]), (5.9, [1])],
        [(6.2, [1]), (9.6, [1]), (6.2, [1, 1])]],
    5: [[(6.4, [1]), (7.6, [1, 1]), (7.6, [1, 1])],
        [(7.6, [1, 1]), (7.6, [1, 1]), (6.4, [1])],
        [(8.4, [1, 1]), (6.8, [1]), (8.4, [1, 1])]],
    6: [[(7.9, [1, 1]), (7.9, [1, 1]), (7.9, [1, 1])],
        [(6.2, [1]), (7.4, [1, 1]), (7.4, [1, 1]), (6.2, [1])],
        [(6.8, [2, 1]), (6.8, [1, 2]), (7.0, [1, 1])],
        [(9.6, [1]), (7.4, [1, 1]), (7.4, [1, 1])],
        [(7.4, [1, 1]), (7.4, [1, 1]), (9.6, [1])]],
}


def _layout_for_page(n, aspects, seed_key, kinds=None):
    """Szablon strony dla n kadrow dopasowany do proporcji ich obrazow.
    Zwraca wiersze: (wys_cm, [(offset_w_stronie, flex, aspekt_slotu), ...])."""
    import hashlib
    import math
    templates = _COMIC_LAYOUTS.get(n)
    if not templates:
        rows, left = [], n
        while left > 0:
            take = 2 if left >= 2 else 1
            rows.append((7.6, [1] * take))
            left -= take
        templates = [rows]

    def _slot_aspects(tpl):
        out = []
        for h, flexes in tpl:
            avail = 18.4 - 0.32 * (len(flexes) - 1)
            tot = sum(flexes)
            out.extend((avail * f / tot) / h for f in flexes)
        return out

    def _slot_areas(tpl):
        out = []
        for h, flexes in tpl:
            avail = 18.4 - 0.32 * (len(flexes) - 1)
            tot = sum(flexes)
            out.extend((avail * f / tot) * h for f in flexes)
        return out

    best, best_score = None, None
    for ti, tpl in enumerate(templates):
        score = 0.0
        for sa, ia in zip(_slot_aspects(tpl), aspects):
            if ia:
                score += abs(math.log(sa / ia))
        if kinds:
            # KOMIKS_V29: sceneria (wide) karana za maly slot, zblizenie
            # (close) za duzy - wzgledna powierzchnia slotu w szablonie.
            areas = _slot_areas(tpl)
            lo, hi = min(areas), max(areas)
            span = (hi - lo) or 1.0
            for ar, kd in zip(areas, kinds):
                rel = (ar - lo) / span
                if kd == "wide":
                    score += (1.0 - rel) * 1.0
                elif kd == "close":
                    score += rel * 1.0
        jitter = (int(hashlib.md5(f"{seed_key}|{ti}".encode()).hexdigest()[:6], 16) % 100) / 100.0
        score -= 0.10 * jitter
        if best_score is None or score < best_score:
            best, best_score = tpl, score
    rows, off = [], 0
    for h, flexes in best:
        avail = 18.4 - 0.32 * (len(flexes) - 1)
        tot = sum(flexes)
        row = []
        for f in flexes:
            row.append((off, f, (avail * f / tot) / h))
            off += 1
        rows.append((h, row))
    return rows


def _stack_after_txtnote(rows, is_txt, narrations):
    """KOMIKS_V32 (regula uzytkownika): kolejne zdjecie ma stac POD ramka
    narracyjna kadru bez zaakceptowanego obrazu, a nie obok niej z pustym
    miejscem pod spodem. Przeksztalca wiersze ukladu strony:
    - wiersz [tekst, zdjecie] -> jeden stos na cala szerokosc: ramka
      tekstowa (wysokosc tekstu), a pod nia to zdjecie na reszte wiersza;
    - wiersz [zdjecie, tekst] -> zdjecie z nastepnego wiersza wedruje do
      kolumny pod ramka tekstowa (wiersz zrodlowy zwija sie do pozostalego
      kadru na cala szerokosc albo znika, dokladajac wysokosc do stosu).
    Zwraca wiersze ze slotami (offset, flex, aspekt, stack_offset|None);
    aspekt slotu stosowego dotyczy zdjecia pod ramka."""
    import math as _math

    def est_h(text, width_cm):
        inner = max(4.0, width_cm - 0.75)
        cpl = max(8, int(inner / 0.185))
        lines = max(1, _math.ceil(len(text or "") / cpl)) if text else 1
        return 0.72 + lines * 0.50

    work = [[h, list(slots)] for h, slots in rows]
    out = []
    i = 0
    while i < len(work):
        h, slots = work[i]
        done = False
        if len(slots) == 2:
            (o0, f0, sa0), (o1, f1, sa1) = slots
            if is_txt[o0] and not is_txt[o1]:
                img_h = max(2.2, h - est_h(narrations[o0], 18.4))
                out.append((h, [(o0, f0 + f1, 18.4 / img_h, o1)]))
                done = True
            elif not is_txt[o0] and is_txt[o1] and i + 1 < len(work):
                h2, slots2 = work[i + 1]
                if slots2 and not is_txt[slots2[0][0]]:
                    mo = slots2[0][0]
                    rest = slots2[1:]
                    hh = h if rest else h + h2 + 0.32
                    w1 = (18.4 - 0.32) * f1 / (f0 + f1)
                    img_h = max(2.2, hh - est_h(narrations[o1], w1))
                    out.append((hh, [(o0, f0, sa0, None), (o1, f1, w1 / img_h, mo)]))
                    if rest:
                        ro, rf, _rsa = rest[0]
                        work[i + 1] = [h2, [(ro, rf, 18.4 / h2)]]
                    else:
                        work.pop(i + 1)
                    done = True
        if not done:
            out.append((h, [(o, f, sa, None) for (o, f, sa) in slots]))
        i += 1
    return out


def build_comic_document(title: str, body: str, opts: dict, engine: str):
    """Komiks jako kompletny HTML: kadry z ilustracjami, wymuszone łamanie stron.
    Zwraca (html, extra_files) - extra_files to zweryfikowane obrazy kadrów jako
    osobne pliki zadania, gdy jest ich za dużo na data-URI (wariant github)."""
    try:
        requested = int(str(opts.get("strony") or "0").strip() or 0)
    except Exception:
        requested = 0
    pages = _split_comic_pages(body, requested)
    if not pages:
        pages = [[{"caption": _clean_panel_text(body) or "Komiks", "prompt": None, "svg": None}]]
    extra_files = []
    font_stack = DOC_FONT_STACKS.get((opts.get("czcionka") or "sans").strip().lower(),
                                     DOC_FONT_STACKS["sans"])
    flat = [panel for page in pages for panel in page]

    # Zrodla ilustracji per kadr: WYLACZNIE obrazy AI z KONTROLA JAKOSCI
    # (KOMIKS_V18/V19) - backend sam pobiera i weryfikuje kazdy obraz wzgledem
    # koncepcji kadru, ponawia niezgodne i wbudowuje zweryfikowane (data-URI,
    # a przy duzej objetosci w wariancie github jako osobne pliki zadania
    # kadr_N.jpg). Gdy obrazu nie da sie uzyskac wcale, src zostaje None
    # i kadr dostaje puste tlo pod tekst (bez rysunkow wektorowych).
    srcs = [None] * len(flat)
    # KOMIKS_V25: budżet 120 s liczony OSOBNO dla każdego kadru

    def _one(i_p):
        idx, panel = i_p
        raw = _qc_panel_image(panel, idx, time.time() + QC_PANEL_BUDGET_S)
        if raw:
            try:
                return _jpeg_bytes(raw, 1100)
            except Exception:
                return None
        return None

    try:
        from concurrent.futures import ThreadPoolExecutor as _TPE
        with _TPE(max_workers=4) as ex:
            imgs = list(ex.map(_one, enumerate(flat)))
    except Exception:
        imgs = [_one(p) for p in enumerate(flat)]
    # KOMIKS_V28: proporcje obrazow -> uklad kazdej strony -> dopasowanie
    # kazdego obrazu do jego slotu (panoramizacja V27, ale do proporcji
    # konkretnej ramki z ukladu, nie jednej sztywnej dla calej strony).
    img_aspects = []
    for b in imgs:
        a = None
        if b:
            try:
                with Image.open(BytesIO(b)) as _im:
                    a = _im.width / max(1, _im.height)
            except Exception:
                a = None
        img_aspects.append(a)
    panel_kinds = [_panel_kind(_pnl) for _pnl in flat]
    layouts, _start = [], 0
    for _pi, _pg in enumerate(pages):
        layouts.append(_layout_for_page(len(_pg),
                                        img_aspects[_start:_start + len(_pg)],
                                        f"{title}|{_pi}",
                                        panel_kinds[_start:_start + len(_pg)]))
        _start += len(_pg)
    # KOMIKS_V31 (regula uzytkownika): zaakceptowane ilustracje maja
    # wypelniac strone - proporcjonalnie rozciagamy wysokosci wierszy
    # ukladu do calej uzytecznej wysokosci kartki (str. 1 ma tytul, wiec
    # nieco mniej miejsca). Aspekty slotow korygujemy o ten sam wspolczynnik,
    # zeby panoramizacja nadal trafiala w prawdziwe proporcje ramek.
    _PAGE_FILL_CM = (25.0, 26.2)
    _scaled = []
    for _pi, _rows in enumerate(layouts):
        _total = sum(h for h, _ in _rows) + 0.32 * max(0, len(_rows) - 1)
        _target = _PAGE_FILL_CM[0] if _pi == 0 else _PAGE_FILL_CM[1]
        _k = min(2.2, max(1.0, _target / _total)) if _total else 1.0
        _scaled.append([(round(h * _k, 2),
                         [(o, f, sa / _k) for (o, f, sa) in slots])
                        for h, slots in _rows])
    layouts = _scaled
    # KOMIKS_V32: stosy "ramka tekstowa + zdjecie pod nia"
    _tstart, _stacked_layouts = 0, []
    for _pg, _rows in zip(pages, layouts):
        _n = len(_pg)
        _is_txt = [imgs[_tstart + _o] is None for _o in range(_n)]
        _narr = [_split_speech(_pnl.get("caption") or "")[0] for _pnl in _pg]
        _stacked_layouts.append(_stack_after_txtnote(_rows, _is_txt, _narr))
        _tstart += _n
    layouts = _stacked_layouts
    _slot_aspect, _start = {}, 0
    for _rows in layouts:
        for _h, _slots in _rows:
            for _o, _f, _sa, _stk in _slots:
                _slot_aspect[_start + (_stk if _stk is not None else _o)] = _sa
        _start += sum((2 if _s[3] is not None else 1) for _r in _rows for _s in _r[1])
    imgs = [_panoramize(b, _slot_aspect.get(i, 1.5)) if b else None
            for i, b in enumerate(imgs)]
    total = sum(len(b) for b in imgs if b)
    use_files = (engine == "github" and total > QC_INLINE_LIMIT)
    for idx, (panel, b) in enumerate(zip(flat, imgs)):
        if b is None:
            srcs[idx] = None
        elif use_files:
            fname = f"kadr_{idx + 1}.jpg"
            extra_files.append(make_file_response(fname, b, "image/jpeg"))
            srcs[idx] = fname
        else:
            srcs[idx] = "data:image/jpeg;base64," + base64.b64encode(b).decode("ascii")

    pages_html = []
    _pstart = 0
    for page, rows in zip(pages, layouts):
        rows_html = []
        for h, slots in rows:
            _gidxs = []
            for _o, _f, _sa, _stk in slots:
                _gidxs.append(_pstart + _o)
                if _stk is not None:
                    _gidxs.append(_pstart + _stk)
            # wiersz bez zadnej ilustracji (same ramki narracyjne) kurczy sie
            # do wysokosci tekstu (KOMIKS_V31) - bez pustej przestrzeni
            _any_img = any(srcs[g] is not None for g in _gidxs)
            panels_html = []
            for _o, flex, _sa, _stk in slots:
                g = _pstart + _o
                if _stk is None:
                    panels_html.append(_panel_html(srcs[g], page[_o], g + 1, flex))
                else:
                    g2 = _pstart + _stk
                    panels_html.append(
                        f"<div class=\"vstack\" style=\"flex:{flex} 1 0\">"
                        + _panel_html(srcs[g], page[_o], g + 1, flex, stacked=True)
                        + _panel_html(srcs[g2], page[_stk], g2 + 1, 1)
                        + "</div>")
            _hstyle = f' style="height:{h}cm"' if _any_img else ""
            rows_html.append(f"<div class=\"prow\"{_hstyle}>" + "".join(panels_html) + "</div>")
        pages_html.append("<div class=\"page\">" + "".join(rows_html) + "</div>")
        _pstart += len(page)
    html_out = (
        "<!DOCTYPE html>\n<html lang=\"pl\">\n<head>\n<meta charset=\"utf-8\">\n"
        f"<title>{_html_mod.escape(title or 'Komiks')}</title>\n"
        f"<style>{_comic_css(font_stack)}</style>\n</head>\n<body>\n"
        f"<h1 class=\"doc-title\">{_html_mod.escape(title or 'Komiks')}</h1>\n"
        + "\n".join(pages_html) +
        "\n</body>\n</html>"
    )
    return html_out, extra_files


def make_pdf_files(title_raw: str, body: str):
    """GENERATE_PDF po nowemu. Zwraca (lista_plików, opis_silnika).

    local  -> gotowy PDF w liście plików (WeasyPrint, awaryjnie FPDF).
    github -> pseudo-plik karty buildu (application/x-build-job, target pdf)
              z dokumentem HTML jako źródłem; użytkownik startuje build
              przyciskiem na karcie, a gotowy PDF pojawia się w karcie.
    """
    title, opts = parse_pdf_tag_args(title_raw)
    safe_base = _doc_safe_base(title)
    engine, reason = pdf_choose_engine(body, opts["szablon"], opts["silnik"])

    if engine == "github" and not (globals().get("GH_TOKEN") and globals().get("GH_BUILD_REPO")):
        print("DOKUMENTY_V10: silnik github niedostępny (brak sekretów GH) - robię lokalnie.")
        engine = "local"

    # KOMIKS_V11: komiks ma wlasny builder (kadry + ilustracje + lamanie stron);
    # musi znac silnik PRZED budowa HTML, bo od niego zalezy zrodlo obrazow.
    comic_extra = []
    if opts["szablon"] == "komiks":
        html_doc, comic_extra = build_comic_document(title, body, opts, engine)
    else:
        html_doc = build_document_html(title, body, opts["szablon"], opts["czcionka"])

    if engine == "github":
        payload = {"target": "pdf",
                   "sources": [make_file_response(safe_base + ".html", html_doc, "text/html")] + comic_extra}
        job_file = make_file_response("build_pdf.job",
                                      json.dumps(payload, ensure_ascii=False),
                                      "application/x-build-job")
        return [job_file], f"github ({reason})"

    try:
        pdf_bytes = render_pdf_weasyprint(html_doc)
        engine_info = f"local/weasyprint ({reason})"
    except Exception as e:
        print(f"DOKUMENTY_V10: WeasyPrint niedostępny ({e}) - awaryjnie FPDF.")
        pdf_bytes = create_pdf_from_text(title, body)
        engine_info = "local/fpdf-awaryjny"
    if not _pdf_ok(pdf_bytes) and "weasyprint" in engine_info:
        # BRAMKA_DOK_V39: renderer „zadziałał", ale wynik nie jest
        # prawdziwym PDF-em – zamiast uszkodzonego pliku dla użytkownika
        # próbujemy jeszcze generatora awaryjnego.
        print("BRAMKA_DOK_V39: WeasyPrint zwrócił nieprawidłowy PDF – awaryjnie FPDF.")
        pdf_bytes = create_pdf_from_text(title, body)
        engine_info = "local/fpdf-awaryjny (kontrola jakości)"
    if not _pdf_ok(pdf_bytes):
        raise RuntimeError("PDF output invalid / empty (kontrola jakości)")
    return [make_file_response(safe_base + ".pdf", pdf_bytes, "application/pdf")], engine_info


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

def _generate_ai_horde(prompt: str, max_polls: int = 60, width: int = 512, height: int = 512) -> bytes:
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
            "width": width,
            "height": height,
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
    for _ in range(max_polls):
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

# --- AUTH (JWT, dokładnie tak samo jak w Boxie) ---
# Kody dostępu i sekrety trzymane TYLKO na backendzie, nigdy we frontendzie.
# Na HF Space ustaw w Secrets: JWT_SECRET_KEY (losowy, długi),
# BOX_ACCESS_CODE, BOX_BACKEND_URL.
# SESJA_V8: gdy brak sekretu Space, klucz JWT jest trzymany w datasecie
# (ten sam, co darowane klucze) — dzieki temu restarty Space'a nie uniewazniaja
# logowan. Sekret Space (JWT_SECRET_KEY) nadal ma pierwszenstwo, gdy jest ustawiony.
JWT_SECRET_FILE = "buddy-jwt-secret.json"

def _resolve_jwt_secret():
    env_key = os.environ.get("JWT_SECRET_KEY")
    if env_key:
        return env_key
    if HF_TOKEN:
        try:
            path = hf_hub_download(repo_id=DONATIONS_REPO_ID, filename=JWT_SECRET_FILE,
                                   repo_type="dataset", token=HF_TOKEN, cache_dir=HF_CACHE_DIR)
            with open(path, "r", encoding="utf-8") as f:
                stored = json.load(f).get("secret")
            if stored:
                print("SYSTEM: JWT - wczytano staly klucz z datasetu (SESJA_V8).")
                return stored
        except Exception:
            pass
        generated = secrets.token_urlsafe(32)
        try:
            temp_path = os.path.join(HF_CACHE_DIR, JWT_SECRET_FILE)
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump({"secret": generated}, f)
            HfApi(token=HF_TOKEN).upload_file(
                path_or_fileobj=temp_path, path_in_repo=JWT_SECRET_FILE,
                repo_id=DONATIONS_REPO_ID, repo_type="dataset",
                commit_message="Add buddy JWT secret")
            print("SYSTEM: JWT - zapisano nowy staly klucz do datasetu (SESJA_V8).")
            return generated
        except Exception as e:
            print(f"OSTRZEZENIE: nie udalo sie zapisac klucza JWT do datasetu ({e}) - "
                  "tokeny uniewaznia sie po restarcie. Ustaw JWT_SECRET_KEY w sekretach Space.")
            return generated
    generated = secrets.token_urlsafe(32)
    print("OSTRZEZENIE: brak JWT_SECRET_KEY i dostepu do datasetu - wygenerowano losowy klucz "
          "(tokeny uniewaznia sie po restarcie). Ustaw JWT_SECRET_KEY w sekretach Space.")
    return generated

JWT_SECRET_KEY = _resolve_jwt_secret()

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 8  # WYLOGOWANIE_V9: 8 godzin (bylo 30 dni w SESJA_V8).
# Token i tak znika z przegladarki po zamknieciu karty/okna (frontend trzyma go
# tylko w sessionStorage); krotszy czas zycia ogranicza skutki jego ewentualnego
# przechwycenia. Sesja po stronie klienta konczy sie wraz z karta, nie z tym terminem.

# Kody dostępu do agenta (jak w Boxie: kod -> poziom dostępu)
ACCESS_CODES = {
    "2017": "full",
}

BOX_ACCESS_CODE = os.getenv("BOX_ACCESS_CODE", "2358")
BOX_BACKEND_URL = os.getenv("BOX_BACKEND_URL", "https://eosforus-box.hf.space")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token", auto_error=False)

# WYLOGOWANIE_V9: jti tokenow uniewaznionych przez /logout. Lista w pamieci procesu -
# restart backendu ja czysci, ale uniewazniony token i tak zylby wtedy max 8 godzin.
_REVOKED_TOKEN_IDS = set()

class LoginRequest(BaseModel):
    code: str

def create_access_token(data: dict, expires_delta: Union[timedelta, None] = None):
    to_encode = data.copy()
    # WYLOGOWANIE_V9: unikalny identyfikator tokenu - /logout wpisuje go na liste
    # uniewaznionych i token przestaje dzialac, nawet jesli ktos go skopiowal.
    to_encode.setdefault("jti", secrets.token_urlsafe(16))
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=ALGORITHM)

async def get_current_user(request: Request, token_from_header: Optional[str] = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Could not validate credentials")
    token = token_from_header or request.query_params.get("token")
    if not token: raise credentials_exception
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("jti") in _REVOKED_TOKEN_IDS:  # WYLOGOWANIE_V9: token po /logout
            raise credentials_exception
        username = payload.get("sub")
        if username != "user":
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    return username

# --- FASTAPI SETUP ---
app = FastAPI()

@app.exception_handler(RequestValidationError)
async def _walidacja_log(request: Request, exc: RequestValidationError):
    # DIAGNOSTYKA_V65E: samo "422" w logu dostepu nie mowi, ktore pole
    # FastAPI odrzucilo - wypisz szczegoly bledu do loga Space.
    try:
        print(f"WALIDACJA 422 {request.url.path}: {jsonable_encoder(exc.errors())}", flush=True)
    except Exception:
        pass
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.post("/login")
async def login(request: LoginRequest):
    """Logowanie kodem dostępu (jak w Boxie). Zwraca JWT: {"access_token", "token_type"}."""
    code = request.code.strip()
    if code in ACCESS_CODES:
        token = create_access_token(
            data={"sub": "user", "access_level": ACCESS_CODES[code]},
            expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        )
        return {"access_token": token, "token_type": "bearer"}
    raise HTTPException(status_code=400, detail="Incorrect access code")

@app.post("/logout")
async def logout(user: str = Depends(get_current_user), token: Optional[str] = Depends(oauth2_scheme)):
    """WYLOGOWANIE_V9: uniewaznia biezacy token - jego jti trafia na liste
    uniewaznionych, wiec token przestaje dzialac od razu, nie dopiero po wygasnieciu.
    Frontend wolajac ten endpoint przy wyjsciu z asystenta (keepalive) oraz kasuje
    token u siebie; samo zamkniecie karty/okna kasuje token po stronie przegladarki
    (sessionStorage), wiec ponowne wejscie wymaga wpisania kodu."""
    if token:
        try:
            payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[ALGORITHM])
            jti = payload.get("jti")
            if jti:
                _REVOKED_TOKEN_IDS.add(jti)
        except JWTError:
            pass
    return {"status": "ok"}

class Attachment(BaseModel): name: str; mime_type: str; data: str
class HistoryItem(BaseModel): role: str; content: str; attachments: Optional[List[Attachment]] = None; isHidden: Optional[bool] = False
class ChatRequest(BaseModel): history: List[HistoryItem]; apiKey: Optional[str] = Field(None, alias="apiKey"); request_id: Optional[str] = None; straznik: Optional[bool] = False; straznik_urzadzenie: Optional[str] = None; straznik_wybrane: Optional[list] = None
class DonateKeyRequest(BaseModel): apiKey: str
class ExportRequest(BaseModel): history: List[HistoryItem]

@app.post("/donate-key")
def dp(r: DonateKeyRequest, user: str = Depends(get_current_user)):
    k=r.apiKey.strip(); keys=load_donated_keys_safely(); keys.append({"timestamp": datetime.now().isoformat(), "key": k})
    save_donated_keys(keys)
    # dopisz też do puli w pamięci – bez tego klucz działał dla innych dopiero po restarcie backendu
    if k and k not in api_keys: api_keys.append(k); api_keys[:] = _priorytet_kluczy(api_keys)
    return {"status": "ok"}

@app.post("/export-pdf")
def ep(r: ExportRequest, user: str = Depends(get_current_user)):
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


# --- BOX PROXY: kod dostępu do Boxa zna tylko backend, frontend woła /box/* ---
_box_token: Optional[str] = None
_box_token_exp: float = 0.0

def _box_login() -> str:
    global _box_token, _box_token_exp
    if _box_token and time.time() < _box_token_exp:
        return _box_token
    if not BOX_ACCESS_CODE:
        raise HTTPException(status_code=503, detail="Box nie jest skonfigurowany na backendzie.")
    try:
        r = requests.post(f"{BOX_BACKEND_URL}/login", json={"code": BOX_ACCESS_CODE}, timeout=15)
        r.raise_for_status()
        data = r.json()
        _box_token = data.get("access_token") or data.get("token")
        if not _box_token:
            raise RuntimeError("Box login: brak tokenu w odpowiedzi")
        _box_token_exp = time.time() + 3000
        return _box_token
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Box login error: {e}")

def _box_headers() -> dict:
    return {"Authorization": f"Bearer {_box_login()}"}

@app.get("/box/files")
def box_files(user: str = Depends(get_current_user)):
    try:
        r = requests.get(f"{BOX_BACKEND_URL}/files", headers=_box_headers(), timeout=20)
        r.raise_for_status()
        return r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Box files error: {e}")

@app.post("/box/upload")
async def box_upload(
    file: UploadFile = File(...),
    path: str = Form("chats"),
    user: str = Depends(get_current_user),
):
    try:
        data = await file.read()
        files = {"file": (file.filename, data, file.content_type or "application/octet-stream")}
        r = requests.post(f"{BOX_BACKEND_URL}/upload", headers=_box_headers(),
                           files=files, data={"path": path}, timeout=60)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {"status": "ok"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Box upload error: {e}")

@app.get("/box/download")
def box_download(path: str, user: str = Depends(get_current_user)):
    try:
        enc = requests.utils.quote(path, safe="")
        bt = _box_login()
        r = requests.get(f"{BOX_BACKEND_URL}/download/{enc}?token={bt}&preview=true", timeout=30)
        if not r.ok and not path.lower().endswith(".json"):
            # BOX_BINARNE_V89: podglad moze odrzucac pliki binarne - proba bez podgladu
            r = requests.get(f"{BOX_BACKEND_URL}/download/{enc}?token={bt}", timeout=30)
        r.raise_for_status()
        # V89: zwracamy SUROWE BAJTY z typem zawartosci Boxa. Wczesniej bylo
        # tu return r.json(), ktore rozbijalo sie na kazdym pliku binarnym
        # (.exe/.apk straznika) i karta instalacji meldowala "nie znalazlem
        # pliku", choc plik lezal w Boxie. Pliki .json przechodza bajt w bajt,
        # wiec dotychczasowi odbiorcy (res.json() we frontendzie) dzialaja
        # bez zmian.
        media = r.headers.get("content-type") or ("application/json" if path.lower().endswith(".json") else "application/octet-stream")
        return Response(content=r.content, media_type=media)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Box download error: {e}")


class BoxDeleteRequest(BaseModel):
    path: str

@app.post("/box/delete")
def box_delete(r: BoxDeleteRequest, user: str = Depends(get_current_user)):
    try:
        resp = requests.post(f"{BOX_BACKEND_URL}/delete", headers=_box_headers(),
                             json={"path": r.path}, timeout=20)
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {"status": "ok"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Box delete error: {e}")


# --- GŁOS (TTS) ---
class TTSRequest(BaseModel):
    text: str
    voice: Optional[str] = "pl-PL-MarekNeural"

@app.post("/tts")
async def tts(r: TTSRequest, user: str = Depends(get_current_user)):
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
        client = groq.Groq(api_key=key, timeout=90.0, max_retries=0)
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
        client = openai.OpenAI(api_key=key, timeout=120.0, max_retries=0)
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
    user: str = Depends(get_current_user),
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
def _pil_to_jpeg_b64(img, max_w=768):
    """PIL Image -> base64 JPEG (do payloadow wizyjnych dostawcow)."""
    try:
        im = img.convert("RGB")
        im.thumbnail((max_w, max_w), Image.Resampling.LANCZOS)
        buf = BytesIO()
        im.save(buf, format="JPEG", quality=82)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


def _msgs_have_image(msgs):
    return any(isinstance(pp, Image.Image)
               for m in (msgs or []) for pp in (m.get("parts") or []))


def _mm_content(msgs, prov):
    """WIZJA_V23: wiadomosci Z OBRAZAMI dla dostawcow innych niz Gemini.

    Wczesniej tylko galaz Gemini przyjmowala obrazy (PIL Image w parts),
    a pozostale dostawcy dostawali sam tekst (fmt() odrzuca czesci-obrazki),
    choc ich modele (GPT-4o, Claude, Llama 4, Mistral) tez sa multimodalne.
    prov="openai": format image_url (OpenAI, OpenRouter, Meta, Mistral, Groq);
    prov="anthropic": bloki image/base64. Bez obrazow = zachowanie jak fmt().
    """
    out = []
    for m in msgs:
        if m.get("isHidden"):
            continue
        role = "assistant" if m["role"] == "model" else "user"
        texts = [str(pp) for pp in m.get("parts", []) if isinstance(pp, str) and pp.strip()]
        imgs = [pp for pp in m.get("parts", []) if isinstance(pp, Image.Image)]
        if not imgs:
            content = " ".join(texts)
            if content:
                out.append({"role": role, "content": content})
            continue
        blocks = []
        for t in texts:
            blocks.append({"type": "text", "text": t})
        for im in imgs:
            b64 = _pil_to_jpeg_b64(im)
            if not b64:
                continue
            if prov == "anthropic":
                blocks.append({"type": "image", "source": {
                    "type": "base64", "media_type": "image/jpeg", "data": b64}})
            else:
                blocks.append({"type": "image_url", "image_url": {
                    "url": f"data:image/jpeg;base64,{b64}"}})
        if blocks:
            out.append({"role": role, "content": blocks})
    if prov == "anthropic" and out and out[0]["role"] == "assistant":
        del out[0]
    return out


# CZAS_V26: twarde timeouty klientow dostawcow (120 s, Groq 90 s,
# max_retries=0; Gemini przez HttpOptions) - zawieszone polaczenie ma
# zakonczyc sie bledem i oddac ster rotacji kluczy, a nie wisiec w nieskonczonosc.
def call_llm(key, msgs, sys_prompt, max_out=8000):
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

    # DIETA_V60: limit czasu jednej próby zależy od rozmiaru zadania.
    # Małe wywołania mechaniczne (wybór bloków, kontrola jakości, plan)
    # mają się nie wlec 90–120 s za przeciążonym modelem – 45 s i kaskada
    # idzie dalej. Duże zadania (czat, generowanie plików, edycja bloków)
    # zachowują 120 s, bo ich odpowiedzi są długie z natury.
    _tmo = 45.0 if max_out <= 2000 else 120.0
    try:
        if _wyciszony(key, "*"):
            raise RuntimeError("429 quota – klucz wyciszony po błędach limitu, pomijam bez wywołania")
        _has_img = _msgs_have_image(msgs)
        if key.startswith("sk-ant-"):
            client = anthropic.Anthropic(api_key=key, timeout=_tmo, max_retries=0)
            try:
                return client.messages.create(
                    model="claude-sonnet-5-5",
                    max_tokens=max_out,
                    system=sys_prompt,
                    messages=(_mm_content(msgs, 'anthropic') if _has_img else fmt('anthropic'))
                ).content[0].text
            except Exception as e:
                _wycisz(key, "claude-sonnet-5-5", e)  # OSZCZEDNOSC_V37
                raise

        elif key.startswith("gsk_"):
            # LIMIT_GROQ_V10: darmowy Groq ma limit 8000 tokenów/min (TPM) na CAŁE
            # zapytanie. Duży kontekst (np. edycja pliku: 34 tys. znaków treści +
            # historia = ~20 tys. tokenów) zawsze kończył się serią błędów 413 po
            # wszystkich modelach – strata czasu i prób. Takie zapytanie pomijamy
            # od razu: rotacja kluczy przejdzie do Mistrala/Gemini, które przyjmą
            # większy kontekst. (Szacunek: ~3 znaki na token, z zapasem.)
            _est_tokens = (len(sys_prompt or "") + sum(
                len(str(p)) for m in msgs for p in m.get('parts', []) if isinstance(p, str))) // 3
            if _est_tokens > 7000:
                raise RuntimeError(
                    f"Prompt (~{_est_tokens} tokenów) przekracza darmowy limit Groq (TPM 8000) – pomijam klucz Groq")
            client = groq.Groq(api_key=key, timeout=_tmo, max_retries=0)
            if _has_img:
                # WIZJA_V23: zapytanie z obrazem - najpierw modele wizyjne
                models = ["meta-llama/llama-4-scout-17b-16e-instruct",
                          "meta-llama/llama-4-maverick-17b-128e-instruct"]
            else:
                models = []
            models += [
                "openai/gpt-oss-120b",
                "openai/gpt-oss-20b",
                "qwen/qwen3.8-27b"
                # usunięto llama-3.3-70b-versatile: Groq zwraca dla niego 404
                # (model niedostępny dla tego konta) – tylko marnował próbę
            ]
            payload = _mm_content(msgs, 'openai') if _has_img else fmt('groq')
            payload.insert(0, {"role": "system", "content": sys_prompt})
            _proby_groq = 0
            for m in models:
                if _wyciszony(key, m):
                    continue
                _proby_groq += 1
                try:
                    print(f"  Trying Groq model: {m}")
                    return client.chat.completions.create(
                        model=m, 
                        messages=payload, 
                        max_tokens=max_out
                    ).choices[0].message.content
                except Exception as e:
                    print(f"  Groq {m}: {e}")
                    _wycisz(key, m, e)
                    continue
            if not _proby_groq:
                raise RuntimeError("429 RESOURCE_EXHAUSTED – wszystkie modele Groq wyciszone (limity)")
            raise RuntimeError("All Groq models failed")

        elif key.startswith("LLM_"):
            # Meta Model API (modele Muse) – endpoint zgodny z OpenAI
            client = openai.OpenAI(api_key=key, base_url="https://api.meta.ai/v1", timeout=_tmo, max_retries=0)
            models_meta = [
                "muse-spark-1.3",
                "muse-spark-1.1",
            ]
            payload = _mm_content(msgs, 'openai') if _has_img else fmt('meta')
            payload.insert(0, {"role": "system", "content": sys_prompt})
            _proby_meta = 0
            for m in models_meta:
                if _wyciszony(key, m):
                    continue
                _proby_meta += 1
                try:
                    print(f"  Trying Meta model: {m}")
                    return client.chat.completions.create(
                        model=m,
                        messages=payload,
                        max_tokens=max_out
                    ).choices[0].message.content
                except Exception as e:
                    print(f"  Meta {m}: {e}")
                    _wycisz(key, m, e)
                    continue
            if not _proby_meta:
                raise RuntimeError("429 RESOURCE_EXHAUSTED – wszystkie modele Meta wyciszone (limity)")
            raise RuntimeError("All Meta models failed")

        elif key.startswith("sk-or-"):
            # OpenRouter - endpoint zgodny z OpenAI, klucze sk-or-v1-...
            # MUSI być przed ogólną gałęzią "sk-", inaczej klucz trafi do OpenAI (401)
            client = openai.OpenAI(api_key=key, base_url="https://openrouter.ai/api/v1", timeout=_tmo, max_retries=0)
            models_or = [
                "openrouter/auto",
                "openai/gpt-4o-mini",
                "meta-llama/llama-3.3-70b-instruct",
            ]
            payload = _mm_content(msgs, 'openai') if _has_img else fmt('openai')
            payload.insert(0, {"role": "system", "content": sys_prompt})
            _proby_or = 0
            for m in models_or:
                if _wyciszony(key, m):
                    continue
                _proby_or += 1
                try:
                    print(f"  Trying OpenRouter model: {m}")
                    return client.chat.completions.create(
                        model=m,
                        messages=payload,
                        max_tokens=max_out
                    ).choices[0].message.content
                except Exception as e:
                    print(f"  OpenRouter {m}: {e}")
                    _wycisz(key, m, e)
                    continue
            if not _proby_or:
                raise RuntimeError("429 RESOURCE_EXHAUSTED – wszystkie modele OpenRouter wyciszone (limity)")
            raise RuntimeError("All OpenRouter models failed")

        elif key.startswith("sk-"):
            client = openai.OpenAI(api_key=key, timeout=_tmo, max_retries=0)
            payload = _mm_content(msgs, 'openai') if _has_img else fmt('openai')
            payload.insert(0, {"role": "system", "content": sys_prompt})
            try:
                return client.chat.completions.create(
                    model="gpt-4o",
                    messages=payload,
                    max_tokens=max_out
                ).choices[0].message.content
            except Exception as e:
                _wycisz(key, "gpt-4o", e)  # OSZCZEDNOSC_V37
                raise

        elif key.startswith("AIza"):
            try:
                client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=int(_tmo * 1000)))
            except Exception:
                client = genai.Client(api_key=key)
            # PRIORYTET_V7: usuniete gemini-2.5-flash i gemini-2.5-pro — Google
            # zwraca dla nich 404 (niedostepne dla nowych uzytkownikow), wiec tylko
            # marnowaly czas przy kazdym wywolaniu klucza Gemini.
            models_gemini = [
                "gemini-3.8-flash",
                "gemini-3.5-flash",
                "gemini-3.1-pro-preview",
                # MODELE_LITE_V36: limity AI Studio liczą się OSOBNO na model,
                # więc dalsza kaskada sięga po kolejne dzienne pule TEGO SAMEGO
                # klucza: Flash-Lite ~500 zapytań/dobę przy 250K tokenów/min
                # (bierze też duże prompty), Gemma 4 aż ~14 400 zapytań/dobę,
                # ale tylko ~16K tokenów/min (średnie i małe prompty).
                "gemini-3.5-flash-lite",
                "gemini-3.1-flash-lite",
                "gemma-4-31b-it",
                "gemma-4-26b-a4b-it",
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
            _proby_gem = 0
            for gm in models_gemini:
                if _wyciszony(key, gm):
                    continue
                _proby_gem += 1
                try:
                    print(f"  Trying Gemini model: {gm}")
                    if gm.startswith("gemma"):
                        # MODELE_LITE_V36: Gemma nie przyjmuje parametru
                        # system_instruction (API odrzuca takie wywołania),
                        # więc instrukcja systemowa idzie jako pierwsza
                        # wiadomość użytkownika – bez tego Gemma byłaby
                        # martwym punktem kaskady.
                        g_contents = contents
                        if sys_prompt:
                            g_contents = [types.Content(
                                role="user",
                                parts=[types.Part.from_text(text=sys_prompt)]
                            )] + contents
                        response = client.models.generate_content(
                            model=gm,
                            contents=g_contents,
                            config=types.GenerateContentConfig(
                                max_output_tokens=max_out,
                                temperature=0.7
                            )
                        )
                    else:
                        response = client.models.generate_content(
                            model=gm,
                            contents=contents,
                            config=types.GenerateContentConfig(
                                system_instruction=sys_prompt,
                                max_output_tokens=max_out,
                                temperature=0.7
                            )
                        )
                    return response.text
                except Exception as e:
                    print(f"  Gemini {gm}: {e}")
                    _wycisz(key, gm, e)
                    continue
            if not _proby_gem:
                raise RuntimeError("429 RESOURCE_EXHAUSTED – wszystkie modele Gemini wyciszone (limity)")
            raise RuntimeError("All Gemini models failed")
        else:
            # MISTRAL_V1: klucze Mistral nie maja rozpoznawalnego prefiksu (losowe znaki),
            # wiec kazdy klucz spoza znanych formatow probujemy przez API Mistral
            # (endpoint zgodny z OpenAI). Jesli to nie jest wazny klucz Mistral,
            # API zwroci blad i rotacja kluczy przejdzie dalej - jak przy martwym kluczu.
            client = openai.OpenAI(api_key=key, base_url="https://api.mistral.ai/v1", timeout=_tmo, max_retries=0)
            # Kolejność po obserwacji z logów: mistral-large zwracał na tym kluczu
            # 403 „model niedostępny w Twoim planie" (tier), więc idzie na koniec;
            # small/medium mają realną szansę i są lżejsze przy limitach 429.
            models_mistral = [
                "mistral-small-latest",
                "mistral-medium-latest",
                "mistral-large-latest",
            ]
            payload = _mm_content(msgs, 'openai') if _has_img else fmt('openai')
            payload.insert(0, {"role": "system", "content": sys_prompt})
            _proby_mis = 0
            for m in models_mistral:
                if _wyciszony(key, m):
                    continue
                _proby_mis += 1
                try:
                    print(f"  Trying Mistral model: {m}")
                    return client.chat.completions.create(
                        model=m,
                        messages=payload,
                        max_tokens=max_out
                    ).choices[0].message.content
                except Exception as e:
                    print(f"  Mistral {m}: {e}")
                    _wycisz(key, m, e)
                    continue
            if not _proby_mis:
                raise RuntimeError("429 RESOURCE_EXHAUSTED – wszystkie modele Mistral wyciszone (limity)")
            raise RuntimeError("All Mistral models failed")
    except Exception as e:
        raise RuntimeError(f"Provider error: {e}")


# --- BUILDY W CHMURZE: GitHub Actions (konfiguracja w sekretach Space) ---
GH_TOKEN = os.getenv("GITHUB_TOKEN", "")
GH_BUILD_REPO = os.getenv("GH_BUILD_REPO", "")  # np. "Eosforus/agent-builds"
GH_BUILD_WORKFLOW = os.getenv("GH_BUILD_WORKFLOW", "build.yml")
GH_BUILD_REF = os.getenv("GH_BUILD_REF", "main")
GH_BUILD_WAIT_SECONDS = int(os.getenv("GH_BUILD_WAIT_SECONDS", "240"))
GH_TARGETS = {"nsis", "dotnet", "cpp", "android", "pdf"}  # DOKUMENTY_V10: + pdf (wariant 1)

# Treść workflow wgrywanego automatycznie do repo buildowego
# (funkcja _gh_ensure_workflow) – zgodna z plikiem github_actions/build.yml
GH_WORKFLOW_YML = '''name: Agent Builds

# Workflow wywoływany zdalnie przez backend agenta (workflow_dispatch).
# Pliki projektu agent wgrywa do folderu project/ przed wywołaniem.
on:
  workflow_dispatch:
    inputs:
      target:
        description: "Co budujemy"
        required: true
        default: "nsis"
        type: choice
        options:
          - nsis
          - dotnet
          - cpp
          - android
          - pdf

permissions:
  contents: read

jobs:
  build:
    # C# wymaga prawdziwego Windowsa; NSIS i MinGW budujemy taniej na Ubuntu
    runs-on: ${{ inputs.target == 'dotnet' && 'windows-latest' || 'ubuntu-latest' }}
    steps:
      - uses: actions/checkout@v4

      - name: Instalator NSIS
        if: inputs.target == 'nsis'
        run: |
          sudo apt-get update
          sudo apt-get install -y nsis
          cd project
          makensis installer.nsi

      - name: Program C++ (MinGW -> Windows .exe)
        if: inputs.target == 'cpp'
        run: |
          sudo apt-get update
          sudo apt-get install -y g++-mingw-w64-x86-64
          cd project
          x86_64-w64-mingw32-g++ -O2 -static -o app.exe *.cpp

      - name: Program .NET (Windows .exe)
        if: inputs.target == 'dotnet'
        shell: pwsh
        run: |
          dotnet publish project -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true -o project/out

      # Maszyny ubuntu-latest mają Android SDK i JDK 17 fabrycznie;
      # dokładamy tylko Gradle (projekt nie zawiera wrappera binarnego)
      - name: Gradle (tylko Android)
        if: inputs.target == 'android'
        uses: gradle/actions/setup-gradle@v4

      - name: Android APK (WebView, debug)
        if: inputs.target == 'android'
        run: |
          cd project
          gradle assembleDebug --no-daemon

      # DOKUMENTY_V10 (wariant 1): ten sam WeasyPrint co na Space, tylko na
      # runnerze GitHuba - dla dokumentow duzych i zlozonych (dlugie, komiks,
      # duzo tabel/obrazow). Kazdy *.html z project/ -> PDF o tej samej nazwie.
      - name: Dokument PDF (WeasyPrint)
        if: inputs.target == 'pdf'
        run: |
          sudo apt-get update
          sudo apt-get install -y weasyprint fonts-dejavu fonts-liberation
          cd project
          for f in *.html; do weasyprint "$f" "${f%.html}.pdf"; done

      - name: Artefakt wynikowy
        uses: actions/upload-artifact@v4
        with:
          name: build-output
          path: |
            project/*.exe
            project/out/*.exe
            project/app/build/outputs/apk/debug/*.apk
            project/*.pdf
          if-no-files-found: error
'''


def _gh_api(method, path, **kwargs):
    url = f"https://api.github.com/repos/{GH_BUILD_REPO}{path}"
    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    return requests.request(method, url, headers=headers, timeout=60, **kwargs)

def _gh_safe_relpath(fname: str) -> str:
    """Bezpieczna ścieżka względna pliku projektu (obsługuje podfoldery,
    odrzuca wyjście poza folder project/)."""
    p = fname.replace("\\", "/").lstrip("/")
    parts = [seg for seg in p.split("/") if seg not in ("", ".")]
    if not parts or any(seg == ".." for seg in parts):
        raise RuntimeError(f"Niedozwolona ścieżka pliku projektu: {fname}")
    return "/".join(parts)

def _gh_list_project_files():
    """PORZADKI_V5: wszystkie pliki pod project/ w repo buildowym,
    rekurencyjnie (Git Trees API). Zwraca {sciezka_wzgledna: sha} albo None,
    gdy nie da się pobrać drzewa (wtedy działa stare, płytkie sprzątanie)."""
    r = _gh_api("GET", f"/git/trees/{GH_BUILD_REF}?recursive=1")
    if r.status_code != 200:
        return None
    out = {}
    for it in r.json().get("tree", []):
        p = it.get("path", "")
        if it.get("type") == "blob" and p.startswith("project/"):
            out[p[len("project/"):]] = it.get("sha")
    return out

def _gh_upload_project(sources: dict):
    """Wgrywa pliki projektu do folderu project/ w repo buildowym (Contents API),
    usuwając pliki po poprzednich buildach. Nazwy plików mogą zawierać
    podfoldery (np. app/src/main/AndroidManifest.xml w projektach Androida)."""
    from urllib.parse import quote
    rel_sources = {_gh_safe_relpath(fn): data for fn, data in sources.items()}
    # PORZADKI_V5: pełne sprzątanie. Wcześniej czyszczony był tylko wierzch
    # folderu project/ (pliki z tej samej listy), więc wszystko w podfolderach
    # – stare assets (np. spooky_bat.html) i klasy poprzednich projektów –
    # zostawało w repo na zawsze i lądowało w kolejnych buildach. Teraz
    # usuwamy KAŻDY plik spoza bieżącego zestawu, rekurencyjnie.
    existing_all = _gh_list_project_files()
    if existing_all is not None:
        for rel_old, sha_old in existing_all.items():
            if rel_old not in rel_sources:
                _gh_api("DELETE", f"/contents/project/{quote(rel_old, safe='/')}",
                        json={"message": "agent: cleanup", "sha": sha_old, "branch": GH_BUILD_REF})
    else:
        # awaryjnie: stare, płytkie sprzątanie samego wierzchu folderu
        top_level = {rel.split("/")[0] for rel in rel_sources}
        r = _gh_api("GET", "/contents/project")
        if r.status_code == 200:
            for entry in r.json():
                if entry.get("type") == "file" and entry["name"] not in top_level:
                    _gh_api("DELETE", f"/contents/project/{quote(entry['name'])}",
                            json={"message": "agent: cleanup", "sha": entry["sha"], "branch": GH_BUILD_REF})
    for rel, data in rel_sources.items():
        url_path = quote(rel, safe="/")
        body = {
            "message": f"agent: upload {rel}",
            "content": base64.b64encode(data).decode("ascii"),
            "branch": GH_BUILD_REF,
        }
        existing = _gh_api("GET", f"/contents/project/{url_path}")
        if existing.status_code == 200:
            body["sha"] = existing.json()["sha"]
        resp = _gh_api("PUT", f"/contents/project/{url_path}", json=body)
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"GitHub upload {rel}: HTTP {resp.status_code} {resp.text[:200]}")

def _gh_check_workflow():
    """Sprawdza, czy GitHub Actions widzi workflow build.yml w repo.
    Zamienia goły 404 przy dispatchu na konkretną przyczynę."""
    want = f".github/workflows/{GH_BUILD_WORKFLOW}"
    r = _gh_api("GET", "/actions/workflows")
    if r.status_code != 200:
        raise RuntimeError(
            f"Nie udało się pobrać listy workflowów z GitHuba (HTTP {r.status_code}). "
            "Sprawdź, czy token ma uprawnienie Actions: Read and write i obejmuje to repo.")
    paths = [w.get("path", "") for w in r.json().get("workflows", [])]
    if want not in paths:
        seen = ", ".join(p for p in paths if p) or "brak"
        raise RuntimeError(
            f"GitHub nie widzi workflow '{GH_BUILD_WORKFLOW}' (zarejestrowane workflowy: {seen}). "
            f"Plik musi leżeć w ścieżce {want} na gałęzi {GH_BUILD_REF}. Jeśli leży w głównym "
            "katalogu repo, przenieś go: usuń stamtąd i utwórz nowy, wpisując w nazwie pliku "
            f"pełną ścieżkę {want}.")

def _gh_dispatch(target: str) -> float:
    started = time.time()
    resp = _gh_api("POST", f"/actions/workflows/{GH_BUILD_WORKFLOW}/dispatches",
                   json={"ref": GH_BUILD_REF, "inputs": {"target": target}})
    if resp.status_code != 204:
        raise RuntimeError(f"GitHub dispatch: HTTP {resp.status_code} {resp.text[:200]}")
    return started

def _gh_find_run(started: float) -> int:
    """Dispatch nie zwraca ID, więc szukamy najnowszego runa po czasie startu."""
    for _ in range(12):
        time.sleep(5)
        r = _gh_api("GET", f"/actions/workflows/{GH_BUILD_WORKFLOW}/runs",
                    params={"event": "workflow_dispatch", "per_page": "5"})
        if r.status_code == 200:
            for run in r.json().get("workflow_runs", []):
                created = run.get("created_at", "")
                if created:
                    dt = datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ")
                    ts = (dt - datetime(1970, 1, 1)).total_seconds()
                    if ts >= started - 10:
                        return run["id"]
    raise RuntimeError("Nie znaleziono uruchomionego builda na GitHubie (sprawdź zakładkę Actions w repo).")

def _gh_run_status(run_id: int) -> dict:
    r = _gh_api("GET", f"/actions/runs/{run_id}")
    if r.status_code != 200:
        raise RuntimeError(f"GitHub status: HTTP {r.status_code}")
    return r.json()

def _gh_download_outputs(run_id: int) -> dict:
    import zipfile
    r = _gh_api("GET", f"/actions/runs/{run_id}/artifacts")
    if r.status_code != 200:
        raise RuntimeError(f"GitHub artifacts: HTTP {r.status_code}")
    arts = [a for a in r.json().get("artifacts", []) if not a.get("expired")]
    if not arts:
        raise RuntimeError("Build nie zostawił artefaktów (workflow musi kończyć się krokiem upload-artifact).")
    z = _gh_api("GET", f"/actions/artifacts/{arts[0]['id']}/zip")
    if z.status_code != 200:
        raise RuntimeError(f"GitHub artifact download: HTTP {z.status_code}")
    out = {}
    with zipfile.ZipFile(BytesIO(z.content)) as zf:
        for name in zf.namelist():
            if not name.endswith("/"):
                out[os.path.basename(name)] = zf.read(name)
    if not out:
        raise RuntimeError("Artefakt z builda jest pusty.")
    return out

def _gh_validate(target: str, sources: dict) -> str:
    """Wspólna walidacja zlecenia buildu; zwraca znormalizowany target."""
    if not GH_TOKEN or not GH_BUILD_REPO:
        raise RuntimeError("Brak konfiguracji GitHub: ustaw sekrety Space GITHUB_TOKEN i GH_BUILD_REPO.")
    target = (target or "nsis").strip().lower()
    if target not in GH_TARGETS:
        raise RuntimeError(f"Nieznany target GH_BUILD: {target} (dozwolone: {sorted(GH_TARGETS)})")
    if not sources:
        raise RuntimeError("Brak plików projektu – agent musi dołączyć je tagami [FILE:] w tej rozmowie.")
    return target

def _gh_check_access() -> None:
    """Wstępna diagnostyka repo buildowego – zamienia gołe 404 z API
    na czytelne komunikaty (zła nazwa repo / token bez dostępu / puste repo)."""
    r = _gh_api("GET", "")
    if r.status_code == 404:
        raise RuntimeError(
            f"GitHub nie widzi repo '{GH_BUILD_REPO}' dla tego tokenu (HTTP 404). "
            "Sprawdź: (1) sekret GH_BUILD_REPO ma format login/agent-builds "
            "(bez https:// i bez .git), (2) token obejmuje to repo "
            "(Repository access przy tokenie), (3) sekret GITHUB_TOKEN zawiera "
            "aktualny token.")
    if r.status_code != 200:
        raise RuntimeError(f"GitHub: sprawdzenie repo zwróciło HTTP {r.status_code}.")
    default_branch = r.json().get("default_branch")
    if not default_branch:
        raise RuntimeError(
            f"Repo '{GH_BUILD_REPO}' jest puste – nie ma jeszcze żadnej gałęzi. "
            "Dodaj w nim na GitHubie pierwszy plik (np. README.md przyciskiem "
            "Add file → Create new file), co utworzy gałąź main, i spróbuj ponownie.")
    if GH_BUILD_REF != default_branch:
        b = _gh_api("GET", f"/branches/{GH_BUILD_REF}")
        if b.status_code != 200:
            raise RuntimeError(
                f"W repo '{GH_BUILD_REPO}' nie ma gałęzi '{GH_BUILD_REF}' "
                f"(domyślna gałąź to '{default_branch}'). Ustaw na Space zmienną "
                f"GH_BUILD_REF={default_branch} albo zmień nazwę domyślnej gałęzi w repo.")

def _gh_ensure_workflow():
    """Dba o to, by w repo buildowym istniał plik .github/workflows/build.yml.
    Gdy go nie ma, wgrywa wbudowaną wersję – wymaga to uprawnienia tokenu
    Workflows: Read and write (GitHub bez niego odrzuca zapis workflow, 403)."""
    path = f".github/workflows/{GH_BUILD_WORKFLOW}"
    _existing = _gh_api("GET", f"/contents/{path}")
    if _existing.status_code == 200:
        # DOKUMENTY_V10: workflow juz istnieje, ale moze byc starszy niz
        # wbudowany (np. bez targetu pdf) - dispatch z nowym targetem by sie
        # wtedy wywalil na walidacji opcji. Aktualizujemy plik, gdy tresc
        # sie rozni. Nieudana aktualizacja NIE blokuje buildow o starych
        # targetach (stary workflow nadal je obsluguje) - tylko ostrzegamy.
        try:
            _current = base64.b64decode(_existing.json().get("content", "")).decode("utf-8")
            if _current.strip() != GH_WORKFLOW_YML.strip():
                _upd = {
                    "message": "agent: aktualizacja workflow build.yml (DOKUMENTY_V10: target pdf)",
                    "content": base64.b64encode(GH_WORKFLOW_YML.encode("utf-8")).decode("ascii"),
                    "branch": GH_BUILD_REF,
                    "sha": _existing.json().get("sha"),
                }
                _resp = _gh_api("PUT", f"/contents/{path}", json=_upd)
                if _resp.status_code in (200, 201):
                    print("DOKUMENTY_V10: zaktualizowano workflow build.yml w repo buildowym.")
                else:
                    print(f"OSTRZEZENIE: nie udalo sie zaktualizowac workflow (HTTP {_resp.status_code}) - "
                          "target pdf moze nie dzialac, dopoki plik .github/workflows/build.yml "
                          "nie zostanie zaktualizowany recznie albo token nie dostanie Workflows: Read and write.")
        except Exception as e:
            print(f"OSTRZEZENIE: kontrola aktualizacji workflow nieudana: {e}")
        return
    body = {
        "message": "agent: dodaj workflow build.yml",
        "content": base64.b64encode(GH_WORKFLOW_YML.encode("utf-8")).decode("ascii"),
        "branch": GH_BUILD_REF,
    }
    resp = _gh_api("PUT", f"/contents/{path}", json=body)
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Nie udało się utworzyć {path} w repo (HTTP {resp.status_code}). "
            "Najpewniej token nie ma uprawnienia Workflows: Read and write – "
            "dopisz je w ustawieniach tokenu na GitHubie albo utwórz ten plik ręcznie.")

def github_build_start(target: str, sources: dict) -> tuple[int, str]:
    """Wgrywa projekt i wywołuje workflow. Zwraca (run_id, run_url)."""
    target = _gh_validate(target, sources)
    _gh_check_access()
    _gh_ensure_workflow()
    _gh_upload_project(sources)
    _gh_check_workflow()
    started = _gh_dispatch(target)
    run_id = _gh_find_run(started)
    return run_id, f"https://github.com/{GH_BUILD_REPO}/actions/runs/{run_id}"

def github_build_collect(run_id: int, max_seconds: int) -> dict | None:
    """Czeka na zakończenie runa (polling co 10 s). Zwraca pliki wynikowe,
    None przy przekroczeniu max_seconds; RuntimeError przy niepowodzeniu."""
    run_url = f"https://github.com/{GH_BUILD_REPO}/actions/runs/{run_id}"
    deadline = time.time() + max_seconds
    while True:
        run = _gh_run_status(run_id)
        if run.get("status") == "completed":
            if run.get("conclusion") == "success":
                return _gh_download_outputs(run_id)
            raise RuntimeError(f"Build #{run_id} nieudany (wynik: {run.get('conclusion')}). Logi: {run_url}")
        if time.time() > deadline:
            return None
        time.sleep(10)

def github_build(target: str, sources: dict) -> tuple[dict, str]:
    """Wariant synchroniczny (start + oczekiwanie do GH_BUILD_WAIT_SECONDS)."""
    run_id, run_url = github_build_start(target, sources)
    outputs = github_build_collect(run_id, GH_BUILD_WAIT_SECONDS)
    if outputs is None:
        return {}, (f"Build #{run_id} nadal trwa (limit oczekiwania {GH_BUILD_WAIT_SECONDS} s). "
                    f"Przebieg: {run_url} . Gdy użytkownik zapyta o ten build, dokończysz go "
                    f"tagiem [GH_BUILD_STATUS: {run_id}].")
    return outputs, f"Build #{run_id} zakończony sukcesem. {run_url}"

def github_build_status(run_id: int) -> tuple[dict, str]:
    """Sprawdza wcześniejszy build; zwraca jego pliki, gdy już się zakończył."""
    if not GH_TOKEN or not GH_BUILD_REPO:
        raise RuntimeError("Brak konfiguracji GitHub: ustaw sekrety Space GITHUB_TOKEN i GH_BUILD_REPO.")
    run = _gh_run_status(run_id)
    run_url = f"https://github.com/{GH_BUILD_REPO}/actions/runs/{run_id}"
    if run.get("status") != "completed":
        return {}, f"Build #{run_id} nadal trwa (status: {run.get('status')}). {run_url}"
    if run.get("conclusion") != "success":
        raise RuntimeError(f"Build #{run_id} nieudany (wynik: {run.get('conclusion')}). Logi: {run_url}")
    return _gh_download_outputs(run_id), f"Build #{run_id} zakończony sukcesem. {run_url}"


# --- Asynchroniczne buildy dla karty w czacie (frontend odpytuje o status) ---
import threading
import uuid

CLOUD_BUILDS: dict = {}
GH_BUILD_MAX_SECONDS = int(os.getenv("GH_BUILD_MAX_SECONDS", "1800"))

def _cloud_build_worker(job_id: str, target: str, sources: dict):
    job = CLOUD_BUILDS[job_id]
    try:
        job["stage"] = "Wgrywam pliki projektu na GitHuba…"
        run_id, run_url = github_build_start(target, sources)
        job["run_id"] = run_id
        job["run_url"] = run_url
        job["stage"] = "Build w toku na GitHubie…"
        outputs = github_build_collect(run_id, GH_BUILD_MAX_SECONDS)
        if outputs is None:
            raise RuntimeError(f"Build przekroczył limit {GH_BUILD_MAX_SECONDS} s. Przebieg: {run_url}")
        job["files"] = [make_file_response(n, d, "application/octet-stream") for n, d in outputs.items()]
        job["status"] = "done"
        job["stage"] = "Gotowe"
    except Exception as e:
        job["status"] = "failed"
        job["error"] = str(e)
        job["stage"] = "Build nieudany"

def create_cloud_build(target: str, sources: dict) -> str:
    """Waliduje zlecenie i startuje build w wątku w tle. Zwraca job_id."""
    _gh_validate(target, sources)
    job_id = uuid.uuid4().hex[:12]
    CLOUD_BUILDS[job_id] = {
        "status": "running", "stage": "Startuję build…", "target": target.strip().lower(),
        "run_id": None, "run_url": None, "files": [], "error": None, "created": time.time(),
    }
    threading.Thread(target=_cloud_build_worker, args=(job_id, target, sources), daemon=True).start()
    return job_id

def cloud_build_snapshot(job_id: str) -> dict | None:
    job = CLOUD_BUILDS.get(job_id)
    if not job:
        return None
    snap = {"status": job["status"], "stage": job["stage"], "target": job["target"],
            "run_url": job["run_url"], "error": job["error"]}
    if job["status"] == "done":
        snap["files"] = job["files"]
    return snap

class CloudBuildRequest(BaseModel):
    target: str
    sources: List[Dict[str, str]]  # [{filename, data(base64)}]

@app.post("/cloud-build/start")
def cloud_build_start_ep(r: CloudBuildRequest, user: str = Depends(get_current_user)):
    sources = {}
    for s in r.sources:
        fn = s.get("filename", "")
        try:
            sources[fn] = base64.b64decode(s.get("data", ""))
        except Exception:
            raise HTTPException(status_code=400, detail=f"Nieprawidłowe dane pliku: {fn}")
    try:
        job_id = create_cloud_build(r.target, sources)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"build_id": job_id, "status": "running"}

@app.get("/cloud-build/{job_id}")
def cloud_build_status_ep(job_id: str, user: str = Depends(get_current_user)):
    snap = cloud_build_snapshot(job_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="Build nie istnieje (mógł wygasnąć przy restarcie serwera).")
    return snap


# --- Sterowanie aktywnym zadaniem czatu z chmurki oczekiwania ---
# Prosta odpowiedź = frontend pokazuje tylko trzy kropki. Gdy odpowiedź trwa
# długo (duży projekt: backend dokleja kolejne części w pętli), chmurka
# przechodzi w tryb rozbudowany: czas, szacunkowy procent oraz przyciski
# pauza / wznów / stop. Przyciski wołają te endpointy, a pętla w /chat
# sprawdza flagi między częściami – pauza i stop są więc prawdziwe.
CHAT_JOBS: dict = {}

def _chat_job_register(rid: str):
    if not rid:
        return
    now = time.time()
    for k in [k for k, v in CHAT_JOBS.items() if now - v.get("started", now) > 1800]:
        CHAT_JOBS.pop(k, None)  # sprzątanie po starych zadaniach
    CHAT_JOBS[rid] = {"paused": False, "stop": False, "stage": "Pracuję nad odpowiedzią…",
                      "parts": 0, "started": now}

def _chat_job_set_stage(rid: str | None, stage: str, parts: int | None = None):
    job = CHAT_JOBS.get(rid or "")
    if job is None:
        return
    job["stage"] = stage
    if parts is not None:
        job["parts"] = parts

def _chat_job_gate(rid: str | None) -> bool:
    """Czeka, gdy użytkownik wstrzymał zadanie z chmurki. Zwraca False, gdy
    zażądano stopu – pętla ma wtedy przerwać doklejanie części."""
    job = CHAT_JOBS.get(rid or "")
    if job is None:
        return True
    waited = 0.0
    while job.get("paused") and not job.get("stop") and waited < 1800:
        time.sleep(0.4)
        waited += 0.4
    return not job.get("stop")

class ChatJobControl(BaseModel):
    action: str  # pause | resume | stop

@app.get("/chat-job/{rid}")
def chat_job_status_ep(rid: str, user: str = Depends(get_current_user)):
    job = CHAT_JOBS.get(rid)
    if job is None:
        raise HTTPException(status_code=404, detail="Zadanie czatu nie istnieje (zakończone albo wygasło).")
    return {"stage": job["stage"], "parts": job["parts"], "paused": bool(job.get("paused")),
            "stop": bool(job.get("stop")), "elapsed": int(time.time() - job["started"])}

@app.post("/chat-job/{rid}/control")
def chat_job_control_ep(rid: str, r: ChatJobControl, user: str = Depends(get_current_user)):
    job = CHAT_JOBS.get(rid)
    if job is None:
        raise HTTPException(status_code=404, detail="Zadanie czatu nie istnieje (zakończone albo wygasło).")
    action = (r.action or "").strip().lower()
    if action == "pause":
        job["paused"] = True
    elif action == "resume":
        job["paused"] = False
    elif action == "stop":
        job["stop"] = True
        job["paused"] = False
    else:
        raise HTTPException(status_code=400, detail=f"Nieznana akcja: {action}")
    return {"stage": job["stage"], "parts": job["parts"], "paused": bool(job.get("paused")),
            "stop": bool(job.get("stop")), "elapsed": int(time.time() - job["started"])}


# --- PLIK_LOKALNY_V64: edycja pliku wprost na komputerze użytkownika ---
# Frontend (File System Access API) trzyma plik u siebie: wysyła tylko MAPĘ
# bloków (ten sam podział co _podziel_na_bloki, liczony w JS) i – po wyborze
# – treść pojedynczych bloków. Backend robi fazę 1 i 2 edytora w kawałkach
# tymi samymi promptami i zasadami, a bramka ocenia PARY stary/nowy blok
# (różnicę problemów), bo całego pliku tu nie ma. Zapis na dysk robi
# frontend przez uchwyt do pliku. Przy braku kluczy endpointy zwracają
# {pauza_kluczy: true} zamiast podnosić wyjątek – pauzą steruje karta po
# stronie przeglądarki, a wznowienie ponawia tylko bieżący krok.

def _kontrakt_z_inwentarza(funkcje, zmienne, klasy):
    """Ten sam tekst kontraktu co _kontrakt_pliku, ale inwentarz nazw
    pochodzi z frontendu (plik jest lokalny, backend nie ma jego treści)."""
    czesci = []
    if funkcje:
        czesci.append("funkcje: " + ", ".join(list(funkcje)[:40]))
    if zmienne:
        czesci.append("zmienne: " + ", ".join(list(zmienne)[:40]))
    if klasy:
        czesci.append("klasy: " + ", ".join(list(klasy)[:40]))
    inw = ("Istniejące nazwy w pliku: " + "; ".join(czesci) + ". ") if czesci else ""
    return ("KONTRAKT SPÓJNOŚCI: " + inw
            + "Używaj tych nazw zamiast wymyślać równoległe mechanizmy. "
            "ZASADA STANU: jeśli Twoja zmiana wymaga nowej zmiennej czytanej "
            "albo zapisywanej przez więcej niż jedną funkcję, NIE deklaruj "
            "jej lokalnie w tym bloku – w bloku używaj samej nazwy, a po "
            "[/ZAMIEN_BLOK] dopisz jedną linię: [NOWY_GLOBAL: let nazwa = "
            "wartość;]. Backend dopisze tę deklarację do globalnego "
            "zakresu pliku.\n")


def _lokalny_wybor(api_key, user_text, fname, mapa):
    """Faza 1 dla pliku lokalnego (mapa z frontendu).
    Zwraca (wybrane_id|None, blad|None, pauza, za_duze)."""
    by_id = {b.get("id"): b for b in mapa if b.get("id")}

    def mapa_txt(lista):
        return "\n".join(
            f"- [{b['id']}] {b.get('etykieta', 'szkielet')} ({int(b.get('znaki', 0))} znaków) – zaczyna się: {b.get('first', '')}"
            for b in lista)

    def wybierz(lista):
        msgs = [{'role': 'user', 'parts': [
            "[SYSTEM – EDYCJA W KAWAŁKACH] Użytkownik chce zmienić plik " + fname + ", ale plik jest "
            "za duży, by edytować go w jednym zapytaniu. Poniżej MAPA bloków pliku. Wskaż TYLKO bloki, "
            "które trzeba zmienić, aby wykonać polecenie użytkownika – odpowiedz wyłącznie w formacie: "
            "[EDYTUJ_BLOKI: B2, B5] (maks. 4 bloki, bez żadnego innego tekstu). "
            "Jeśli polecenie dotyczy tylko wyglądu (style CSS, przyciski, układ strony), "
            "NIE wskazuj bloków z kodem JavaScript. Jeśli polecenie dotyczy zachowania, sterowania lub zdarzeń, dołącz także blok podpinający zdarzenia (setup/addEventListener) oraz blok korzystający ze zmienianego stanu – zmiana samej funkcji logiki zwykle nie wystarcza.\n"
            "Polecenie użytkownika: " + user_text + "\nMAPA PLIKU " + fname + ":\n" + mapa_txt(lista)]}]
        odp1 = _call_llm_rotacja(api_key, msgs, SYS_MECH, max_out=1000)
        if not odp1:
            if _rotacja_brak_kluczy():
                raise PauzaBrakKluczy()
            return None
        m = re.search(r"\[EDYTUJ_BLOKI:\s*([^\]]+)\]", odp1, re.IGNORECASE)
        if m:
            ids = [x.strip().upper() for x in re.split(r"[,\s]+", m.group(1)) if x.strip()]
        else:
            ids = []
            for tok in re.findall(r"\bB(\d+)\b", odp1):
                bid = "B" + tok
                if bid in by_id and bid not in ids:
                    ids.append(bid)
            if not ids:
                return None
        return [by_id[i] for i in ids if i in by_id and by_id[i] in lista][:4]

    try:
        chosen = wybierz(mapa)
    except PauzaBrakKluczy:
        return None, None, True, False
    if chosen is None:
        return None, "model nie wskazał bloków do zmiany", False, False
    if _prosba_tylko_o_wyglad(user_text):
        if chosen and all(b.get("kod") for b in chosen):
            dozwolone = [b for b in mapa if not b.get("kod")]
            if dozwolone:
                try:
                    chosen = wybierz(dozwolone) or []
                except PauzaBrakKluczy:
                    return None, None, True, False
        chosen = [b for b in chosen if not b.get("kod")]
        if not chosen:
            return None, "polecenie dotyczy wyglądu, a wskazane bloki to sam kod – przerywam", False, False
    total = sum(int(b.get("znaki", 0)) for b in mapa) or 1
    if sum(int(b.get("znaki", 0)) for b in chosen) > 0.6 * total:
        return None, None, False, True
    return [b["id"] for b in chosen], None, False, False


def _lokalny_blok(api_key, user_text, fname, blok_id, etykieta, tresc, inwentarz):
    """Faza 2 dla jednego bloku pliku lokalnego.
    Zwraca (nowa_treść|None, globale, blad|None, pauza)."""
    kontrakt = _kontrakt_z_inwentarza(inwentarz.get("funkcje") or [],
                                       inwentarz.get("zmienne") or [],
                                       inwentarz.get("klasy") or [])
    msgs = [{'role': 'user', 'parts': [
        "[SYSTEM – EDYCJA BLOKU] Plik " + fname + ", blok [" + blok_id + "] (" + etykieta + "). "
        "Polecenie użytkownika: " + user_text + "\n"
        "Poniżej aktualna treść TYLKO tego bloku. Zwróć jego NOWĄ treść (z wprowadzoną zmianą) "
        "w tagach [ZAMIEN_BLOK] ... [/ZAMIEN_BLOK] i nic więcej. Nie odtwarzaj reszty pliku – "
        "backend złoży całość sam, a wszystko poza tym blokiem zostaje bez zmian.\n"
        "ZASADY: zmieniaj WYŁĄCZNIE linie związane z poleceniem użytkownika. Każdą pozostałą "
        "linię przepisz DOKŁADNIE tak, jak jest – nie łącz linii, nie zmieniaj formatowania, "
        "komentarzy ani nazw zmiennych i nie poprawiaj niczego przy okazji.\n"
        + kontrakt +
        "<<<BLOK\n" + tresc + "\n>>>"]}]
    try:
        odp2 = _call_llm_rotacja(api_key, msgs, SYS_MECH)
    except PauzaBrakKluczy:
        return None, [], None, True
    if not odp2:
        if _rotacja_brak_kluczy():
            return None, [], None, True
        return None, [], "brak odpowiedzi modelu", False
    nowa = _blok_z_tagow(odp2, tresc)
    if nowa is None:
        return None, [], "model nie zwrócił bloku w tagach [ZAMIEN_BLOK]", False
    znane = set((inwentarz.get("funkcje") or []) + (inwentarz.get("zmienne") or [])
                + (inwentarz.get("klasy") or []))
    globale = []
    for d in _globale_z_odpowiedzi(odp2):
        mm = re.match(r"(?:let|var|const)\s+([A-Za-z_$][\w$]*)", d)
        if mm and mm.group(1) not in znane and d not in globale:
            globale.append(d)
    return nowa, globale, None, False


def _lokalna_bramka(fname, pary, deklaracje):
    """Kontrola par stary/nowy blok: problem jest NOWY, gdy jego klucz
    występuje w nowej wersji bloku, a nie występował w starej. Kontroli
    'cieni' (typ cien) nie stosujemy – wymaga całego pliku, a tu bramka
    widzi tylko fragmenty. Zwraca opisy nowych pewnych problemów."""
    low = (fname or "").lower()
    if not low.endswith((".html", ".htm", ".js", ".mjs")):
        return []
    out = []
    for para in pary:
        stara, nowa = para.get("stara") or "", para.get("nowa") or ""
        if stara == nowa:
            continue
        try:
            if low.endswith((".js", ".mjs")) or not (para.get("etykieta") or "").startswith("funkcja"):
                p0 = _waliduj_kod(fname, stara)
                p1 = _waliduj_kod(fname, nowa)
            else:
                dod = frozenset(deklaracje or [])
                p0 = _waliduj_skrypt_ast(stara, 1, dod)
                p1 = _waliduj_skrypt_ast(nowa, 1, dod)
        except Exception:
            continue
        klucze0 = {p["klucz"] for p in p0}
        for p in p1:
            if p.get("pewny") and p.get("typ") != "cien" and p["klucz"] not in klucze0:
                out.append(p["opis"])
    return out[:6]


class LocalWyborRequest(BaseModel):
    filename: str
    instrukcja: str
    mapa: List[Dict[str, Any]]
    apiKey: Optional[str] = Field(None, alias="apiKey")


class LocalBlokRequest(BaseModel):
    filename: str
    instrukcja: str
    blok_id: str
    etykieta: str = "szkielet"
    tresc: str
    inwentarz: Dict[str, Any] = {}
    apiKey: Optional[str] = Field(None, alias="apiKey")


class LocalBramkaRequest(BaseModel):
    filename: str
    pary: List[Dict[str, Any]]
    deklaracje: List[str] = []


@app.post("/local-edit/wybor")
def local_edit_wybor(r: LocalWyborRequest, user: str = Depends(get_current_user)):
    wybrane, blad, pauza, za_duze = _lokalny_wybor(r.apiKey, r.instrukcja, r.filename, r.mapa)
    return {"wybrane": wybrane, "blad": blad, "pauza_kluczy": pauza, "za_duze": za_duze}


@app.post("/local-edit/blok")
def local_edit_blok(r: LocalBlokRequest, user: str = Depends(get_current_user)):
    nowa, globale, blad, pauza = _lokalny_blok(r.apiKey, r.instrukcja, r.filename,
                                               r.blok_id, r.etykieta, r.tresc, r.inwentarz or {})
    return {"nowa_tresc": nowa, "globale": globale, "blad": blad, "pauza_kluczy": pauza}


@app.post("/local-edit/bramka")
def local_edit_bramka(r: LocalBramkaRequest, user: str = Depends(get_current_user)):
    return {"problemy": _lokalna_bramka(r.filename, r.pary, r.deklaracje)}


# --- PLIK_LOKALNY_V65: agent najpierw PATRZY na wskazany plik i pyta ---
# Po wybraniu pliku frontend woła /local-edit/analizuj: agent opisuje, co
# widzi (tekst/kod: mapa + próbka treści; DOCX: akapity i statystyki;
# obraz: wizja; PDF: wyciągnięty tekst, gdy się da), i pyta, co zmienić.
# Sama praca zależy od rodzaju pliku: małe pliki tekstowe edytuje
# /local-edit/caly (całość naraz, pełna bramka kodu), dokumenty DOCX –
# /local-edit/dokument (operacje na akapitach + wstawianie generowanych
# obrazów; dokument jest składany z powrotem na backendzie, a frontend
# zapisuje bajty w miejscu). Gdy zmiany nie da się wykonać, model zwraca
# wyjaśnienie zamiast wyniku – frontend pokazuje je jako wiadomość agenta.

def _docx_podglad(data: bytes):
    """(podglad|None, blad|None): akapity i statystyki z bajtów DOCX."""
    try:
        doc = Document(BytesIO(data))
    except Exception as e:
        return None, str(e)
    try:
        obrazy = len(doc.inline_shapes)
    except Exception:
        obrazy = 0
    obrazy_nr = [i for i, pp in enumerate(doc.paragraphs)
                 if "blip" in pp._p.xml or "graphic" in pp._p.xml]
    tabele_info = []
    try:
        from docx.table import Table as _DocxTable
        nr_par = -1
        for child in doc.element.body.iterchildren():
            if child.tag.endswith("}p"):
                nr_par += 1
            elif child.tag.endswith("}tbl"):
                try:
                    tbl = _DocxTable(child, doc)
                    komorki = []
                    for wiersz in tbl.rows:
                        for kom in wiersz.cells:
                            tx = (kom.text or "").strip()
                            if tx:
                                komorki.append(tx)
                    tabele_info.append({"po": nr_par, "w": len(tbl.rows),
                                        "k": len(tbl.columns),
                                        "tekst": " | ".join(komorki)[:300]})
                except Exception:
                    pass
    except Exception:
        tabele_info = []
    return {"akapity": [pp.text for pp in doc.paragraphs],
            "tabele": len(doc.tables), "obrazy": obrazy,
            "obrazy_nr": obrazy_nr, "tabele_info": tabele_info}, None


def _analiza_model(api_key, prompt, obraz=None, max_out=500):
    """Jedno wywołanie opisujące plik; zwraca (opis|None, pauza)."""
    parts = [prompt]
    if obraz is not None:
        parts.append(obraz)
    try:
        odp = _call_llm_rotacja(api_key, [{'role': 'user', 'parts': parts}],
                                SYSTEM_INSTRUCTION, max_out=max_out)
    except PauzaBrakKluczy:
        return None, True
    if not odp:
        if _rotacja_brak_kluczy():
            return None, True
        return None, False
    return odp.strip(), False


def _lokalna_analiza(api_key, r):
    """Zwraca słownik odpowiedzi dla /local-edit/analizuj."""
    if r.rodzaj == "tekst":
        mapa_txt = "\n".join(
            f"- [{b.get('id')}] {b.get('etykieta', 'szkielet')} ({int(b.get('znaki', 0))} znaków) – zaczyna się: {b.get('first', '')}"
            for b in (r.mapa or [])[:40])
        nazwy = ", ".join((r.inwentarz or {}).get("funkcje") or [])
        prompt = ("[SYSTEM – ANALIZA PLIKU] Użytkownik właśnie pokazał Ci plik ze swojego komputera: "
                  + r.filename + " (" + str(r.znaki) + " znaków). Plik zostaje u niego na dysku, "
                  "a Ty widzisz jego mapę" + (" i całą treść poniżej." if r.caly else " i początek treści poniżej.")
                  + "\nNapisz po polsku, krótko (2–4 zdania): co to za plik i co w nim widzisz "
                  "(rodzaj, struktura, najważniejsze elementy, o czym jest), a na końcu zapytaj, "
                  "co użytkownik chce w nim zmienić. Nie wypisuj kodu ani całej treści.\n"
                  + (("Funkcje w pliku: " + nazwy + "\n") if nazwy else "")
                  + "MAPA:\n" + mapa_txt + "\nTREŚĆ:\n" + (r.probka or "")[:6000])
        opis, pauza = _analiza_model(api_key, prompt)
        return {"opis": opis, "pauza_kluczy": pauza}
    try:
        data = base64.b64decode(r.dane_b64 or "")
    except Exception:
        return {"blad": "uszkodzone dane pliku"}
    if r.rodzaj == "docx":
        podglad, blad = _docx_podglad(data)
        if podglad is None:
            return {"blad": "nie udało się otworzyć dokumentu: " + str(blad)}
        tekst = "\n".join(t for t in podglad["akapity"] if t.strip())[:6000]
        prompt = ("[SYSTEM – ANALIZA DOKUMENTU] Użytkownik pokazał Ci dokument DOCX ze swojego komputera: "
                  + r.filename + " – " + str(len(podglad["akapity"])) + " akapitów, "
                  + str(podglad["tabele"]) + " tabel, " + str(podglad["obrazy"]) + " obrazów. "
                  "Poniżej treść dokumentu. Napisz po polsku, krótko (2–4 zdania): o czym jest ten "
                  "dokument i co zawiera, a na końcu zapytaj, co użytkownik chce w nim zmienić.\nTREŚĆ:\n" + tekst)
        opis, pauza = _analiza_model(api_key, prompt)
        return {"opis": opis, "pauza_kluczy": pauza,
                "akapity": len(podglad["akapity"]), "tabele": podglad["tabele"],
                "obrazy": podglad["obrazy"]}
    if r.rodzaj == "obraz":
        try:
            img = Image.open(BytesIO(data))
            img.load()
        except Exception:
            return {"blad": "to nie jest czytelny obraz"}
        opis, pauza = _analiza_model(
            api_key,
            "Użytkownik pokazał Ci obraz ze swojego komputera (plik " + r.filename + "). "
            "Napisz po polsku, krótko (2–4 zdania): co widzisz na tym obrazie, a na końcu "
            "zapytaj, co użytkownik chce z nim zrobić.", obraz=img, max_out=400)
        return {"opis": opis, "pauza_kluczy": pauza}
    if r.rodzaj == "pdf":
        tekst = ""
        try:
            try:
                from pypdf import PdfReader
            except ImportError:
                from PyPDF2 import PdfReader
            reader = PdfReader(BytesIO(data))
            tekst = "\n".join((strona.extract_text() or "") for strona in reader.pages[:5])[:6000]
        except Exception:
            tekst = ""
        if not tekst.strip():
            return {"opis": "To jest dokument PDF " + r.filename + " (" + str(len(data) // 1024)
                    + " KB). Nie udało mi się podejrzeć jego treści w tym kroku – jeśli opiszesz "
                    "zmianę, dołączę dokument do rozmowy i tam go przeczytam. Co chcesz z nim zrobić?"}
        opis, pauza = _analiza_model(
            api_key,
            "[SYSTEM – ANALIZA PDF] Użytkownik pokazał Ci dokument PDF: " + r.filename + ". "
            "Poniżej tekst z pierwszych stron. Napisz po polsku, krótko (2–4 zdania): o czym jest "
            "ten dokument, a na końcu zapytaj, co użytkownik chce z nim zrobić.\nTEKST:\n" + tekst)
        return {"opis": opis, "pauza_kluczy": pauza}
    return {"blad": "nieznany rodzaj pliku"}


def _lokalny_caly(api_key, user_text, fname, tresc):
    """Edycja całego małego pliku lokalnego jednym wywołaniem.
    Zwraca (nowa_treść|None, odmowa|None, pauza). Brak tagów w odpowiedzi
    modelu = jego wyjaśnienie, że zmiany nie da się wykonać."""
    msgs = [{'role': 'user', 'parts': [
        "[SYSTEM – EDYCJA CAŁEGO PLIKU] Plik " + fname + " jest na tyle mały, że edytujemy go "
        "w całości. Polecenie użytkownika: " + user_text + "\n"
        "Jeśli zmianę da się wykonać: zwróć CAŁĄ nową treść pliku (z wprowadzoną zmianą) "
        "w tagach [ZAMIEN_BLOK] ... [/ZAMIEN_BLOK] i nic więcej. Jeśli zmiany NIE da się "
        "wykonać (dotyczy czegoś, czego w pliku nie ma, albo wymaga innego formatu): napisz "
        "TYLKO krótkie wyjaśnienie po polsku, bez żadnych tagów.\n"
        "ZASADY: zmieniaj wyłącznie to, co wynika z polecenia; każdą pozostałą linię przepisz "
        "DOKŁADNIE tak, jak jest – nie łącz linii, nie zmieniaj formatowania ani nazw.\n"
        + _kontrakt_pliku(fname, tresc) +
        "<<<PLIK\n" + tresc + "\n>>>"]}]
    try:
        odp = _call_llm_rotacja(api_key, msgs, SYS_MECH)
    except PauzaBrakKluczy:
        return None, None, True
    if not odp:
        if _rotacja_brak_kluczy():
            return None, None, True
        return None, "Brak odpowiedzi modelu – spróbuj ponownie.", False
    nowa = _blok_z_tagow(odp, tresc)
    if nowa is None:
        return None, odp.strip()[:700], False
    globale = []
    for d in _globale_z_odpowiedzi(odp):
        mm = re.match(r"(?:let|var|const)\s+([A-Za-z_$][\w$]*)", d)
        if mm and not _nazwa_zadeklarowana(fname, tresc, mm.group(1)):
            globale.append(d)
    if globale:
        nowa = _wstaw_globale(fname, nowa, globale)
    if (fname or "").lower().endswith((".html", ".htm", ".js", ".mjs")):
        klucze0 = {pp["klucz"] for pp in _waliduj_kod(fname, tresc)}
        problemy = [pp for pp in _waliduj_kod(fname, nowa)
                    if pp["klucz"] not in klucze0 and pp.get("pewny")]
        if problemy:
            return None, ("Kontrola kodu odrzuciła tę edycję, więc plik na dysku zostaje bez "
                          "zmian. Problemy: " + "; ".join(pp["opis"] for pp in problemy[:3])), False
    return nowa, None, False


def _lokalny_dokument_trzon(api_key, user_text, fname, data: bytes):
    """Edycja DOCX operacjami na akapitach. Zwraca (nowe_bajty|None,
    odpowiedz, pauza). Pusta lista operacji = wyjaśnienie modelu."""
    podglad, blad = _docx_podglad(data)
    if podglad is None:
        return None, "Nie udało się otworzyć dokumentu: " + str(blad), False
    akapity = podglad["akapity"]
    obrazy_nr = set(podglad.get("obrazy_nr") or [])
    def _wiersz_akapitu(i, t):
        if i in obrazy_nr:
            return f"[{i}] " + ((t + " ") if t else "") + "🖼 [OBRAZ w dokumencie]"
        return f"[{i}] {t}"
    def _znacznik_tabeli(ti):
        return (f"[TABELA po akapicie {ti['po']}] {ti['w']}×{ti['k']}"
                + (f": {ti['tekst']}" if ti.get("tekst") else ""))
    tabele_po = {}
    for ti in (podglad.get("tabele_info") or []):
        tabele_po.setdefault(ti["po"], []).append(ti)
    linie = [_znacznik_tabeli(ti) for ti in tabele_po.get(-1, [])]
    for i, t in enumerate(akapity):
        linie.append(_wiersz_akapitu(i, t))
        for ti in tabele_po.get(i, []):
            linie.append(_znacznik_tabeli(ti))
    lista = "\n".join(linie)
    liczba_tabel = len(podglad.get("tabele_info") or [])
    if len(lista) > 12000:
        return None, ("Ten dokument jest za duży na edycję akapitową w miejscu ("
                      + str(len(akapity)) + " akapitów). Mogę go przebudować jako nowy "
                      "dokument w rozmowie – wyślij go klasycznie przez agrafkę."), False
    prompt = ("[SYSTEM – EDYCJA DOKUMENTU DOCX] Dokument " + fname + " ma " + str(len(akapity))
              + " akapitów" + ((" i " + str(liczba_tabel) + " tabel") if liczba_tabel else "")
              + " (lista poniżej, numerowane od 0). Polecenie użytkownika: " + user_text + "\n"
              "Odpowiedz WYŁĄCZNIE obiektem JSON: {\"odpowiedz\": \"krótki komunikat do użytkownika "
              "po polsku\", \"operacje\": [...]}. Dozwolone operacje: "
              "{\"op\":\"zamien\",\"nr\":N,\"tekst\":\"...\"} (podmień treść akapitu N), "
              "{\"op\":\"wstaw_po\",\"nr\":N,\"tekst\":\"...\"} (nowy akapit po akapicie N), "
              "{\"op\":\"dopisz\",\"tekst\":\"...\"} (akapit na końcu), "
              "{\"op\":\"usun\",\"nr\":N}, "
              "{\"op\":\"obraz_na_koncu\",\"prompt\":\"...\"} (wygeneruj obraz z angielskiego "
              "promptu i wstaw na końcu dokumentu), "
              "{\"op\":\"obraz_po\",\"nr\":N,\"prompt\":\"...\"} (obraz pod akapitem N). "
              "Jeśli polecenia nie da się wykonać w dokumencie tekstowym, zwróć pustą listę "
              "operacji i wyjaśnij powód w polu odpowiedz. Akapity oznaczone 🖼 zawierają obraz "
              "(zdjęcie lub grafikę) – mimo pustej treści NIE są separatorami. Polecenie o obrazie "
              "(„pod zdjęciem”, „nad grafiką”) dotyczy akapitu oznaczonego 🖼, a gdy obraz jest "
              "w dokumencie tylko jeden – właśnie jego: nowy obraz wstawisz pod nim operacją "
              "obraz_po z numerem tego akapitu. Pusty akapit bez oznaczenia 🖼 to separator – "
              "nie musisz go przepisywać. Linie [TABELA …] pokazują tabele wraz z zawartością "
              "komórek – tabel nie edytujesz operacjami, ale są częścią dokumentu: uwzględniaj "
              "ich treść i położenie i nie twierdź, że ich nie ma.\nAKAPITY:\n" + lista)
    try:
        odp = _call_llm_rotacja(api_key, [{'role': 'user', 'parts': [prompt]}], SYS_MECH)
    except PauzaBrakKluczy:
        return None, None, True
    if not odp:
        if _rotacja_brak_kluczy():
            return None, None, True
        return None, "Brak odpowiedzi modelu – spróbuj ponownie.", False
    plan = None
    mj = re.search(r"\{.*\}", odp, re.DOTALL)
    if mj:
        try:
            plan = json.loads(mj.group(0))
        except Exception:
            plan = None
    if not isinstance(plan, dict):
        return None, odp.strip()[:700], False
    odpowiedz = str(plan.get("odpowiedz") or "").strip()
    operacje = [o for o in (plan.get("operacje") or []) if isinstance(o, dict)]
    if not operacje:
        return None, odpowiedz or "Tej zmiany nie da się wykonać w tym dokumencie.", False
    doc = Document(BytesIO(data))
    paras = list(doc.paragraphs)

    def akapit(nr):
        return paras[nr] if isinstance(nr, int) and 0 <= nr < len(paras) else None

    # cele obrazów rozwiązujemy PRZED edycjami strukturalnymi (elementy XML
    # zostają te same, nawet gdy inne akapity znikają albo dochodzą)
    cele_obrazow = []
    wykonane = []
    for o in operacje:
        if o.get("op") == "obraz_po":
            cel = akapit(o.get("nr"))
            cele_obrazow.append((cel._element if cel is not None else None, str(o.get("prompt") or ""), o.get("nr")))
        elif o.get("op") == "obraz_na_koncu":
            cele_obrazow.append((None, str(o.get("prompt") or ""), None))
    # Kolejność operacji ma znaczenie: najpierw WSTAWIENIA (od końca, żeby
    # numery oryginału się zgadzały), potem podmiany treści, na końcu
    # USUWANIE. Wstawianie przed akapit, który inna operacja z tej samej
    # paczki usuwa, lądowałoby w odłączonym fragmencie XML i znikało.
    def _nr(o):
        return o.get("nr") if isinstance(o.get("nr"), int) else -1

    for o in sorted([o for o in operacje if o.get("op") == "wstaw_po"], key=lambda o: -_nr(o)):
        nr = o.get("nr")
        if akapit(nr) is not None:
            tekst_nowy = str(o.get("tekst") or "")
            if nr + 1 < len(paras):
                paras[nr + 1].insert_paragraph_before(tekst_nowy, style=akapit(nr).style)
            else:
                doc.add_paragraph(tekst_nowy, style=akapit(nr).style)
            wykonane.append(f"wstawiono akapit po akapicie {nr}")
    for o in [o for o in operacje if o.get("op") == "zamien"]:
        if akapit(o.get("nr")) is not None:
            akapit(o.get("nr")).text = str(o.get("tekst") or "")
            wykonane.append(f"zmieniono akapit {o.get('nr')}")
    for o in [o for o in operacje if o.get("op") == "dopisz"]:
        doc.add_paragraph(str(o.get("tekst") or ""))
        wykonane.append("dopisano akapit na końcu")
    for o in sorted([o for o in operacje if o.get("op") == "usun"], key=lambda o: -_nr(o)):
        cel = akapit(o.get("nr"))
        if cel is not None:
            el = cel._element
            el.getparent().remove(el)
            wykonane.append(f"usunięto akapit {o.get('nr')}")
    nieudane_obrazy = 0
    for cel_el, prompt_obrazu, nr_obrazu in cele_obrazow:
        if not prompt_obrazu:
            continue
        try:
            img_b = generate_image_from_prompt(prompt_obrazu)
            doc.add_picture(BytesIO(img_b), width=Inches(5.5))
            if cel_el is not None:
                cel_el.addnext(doc.paragraphs[-1]._element)
            wykonane.append("wstawiono obraz na końcu dokumentu" if nr_obrazu is None else f"wstawiono obraz po akapicie {nr_obrazu}")
        except Exception:
            nieudane_obrazy += 1
    if nieudane_obrazy:
        odpowiedz = (odpowiedz + " (Uwaga: nie udało się wygenerować " + str(nieudane_obrazy)
                     + " obrazu/ów – reszta zmian jest zapisana.)").strip()
    buf = BytesIO()
    doc.save(buf)
    nowe = buf.getvalue()
    if not _docx_ok(nowe):
        return None, "Zapis dokumentu się nie powiódł – dokument na dysku bez zmian.", False, []
    return nowe, odpowiedz or "Gotowe – dokument zaktualizowany.", False, wykonane


def _lokalny_dokument(api_key, user_text, fname, data: bytes):
    """PODGLAD_V67: opakowanie trzonu - zawsze zwraca (nowe, odpowiedz,
    pauza, wykonane_operacje); wczesne wyjscia trzonu (3 wartosci) dostaja
    pusta liste operacji."""
    w = _lokalny_dokument_trzon(api_key, user_text, fname, data)
    return (w[0], w[1], w[2], w[3] if len(w) > 3 else [])


class LocalAnalizaRequest(BaseModel):
    rodzaj: str  # tekst | docx | obraz | pdf
    filename: str
    znaki: int = 0
    mapa: List[Dict[str, Any]] = []
    inwentarz: Dict[str, Any] = {}
    probka: str = ""
    caly: bool = False
    dane_b64: Optional[str] = None
    apiKey: Optional[str] = Field(None, alias="apiKey")
    wersja: Optional[str] = None


class LocalCalyRequest(BaseModel):
    filename: str
    instrukcja: str
    tresc: str
    apiKey: Optional[str] = Field(None, alias="apiKey")


class LocalPodgladDokumentRequest(BaseModel):
    filename: str = ""
    dane_b64: str
    apiKey: Optional[str] = Field(None, alias="apiKey")


def _akapity_docx(data: bytes):
    """PODGLAD_V73: teksty akapitow dokumentu do podgladu (bez modelu);
    akapity z obrazem dostaja znacznik."""
    try:
        wynik = []
        for par in Document(BytesIO(data)).paragraphs:
            t = par.text
            if not t and ("blip" in par._p.xml or "graphic" in par._p.xml):
                t = "🖼 [obraz w dokumencie]"
            wynik.append(t)
        return wynik
    except Exception:
        return []


class LocalAudytRequest(BaseModel):
    filename: str
    rodzaj: str  # tekst | docx
    tresc: str = ""
    fragmenty: List[str] = []
    skan: Dict[str, Any] = {}
    dane_b64: Optional[str] = None
    apiKey: Optional[str] = Field(None, alias="apiKey")
    wersja: Optional[str] = None


class LocalWyczyscDokumentRequest(BaseModel):
    filename: str
    dane_b64: str
    apiKey: Optional[str] = Field(None, alias="apiKey")


def _audyt_docx(data: bytes):
    """AUDYT_V68: mechaniczna inspekcja DOCX jako archiwum ZIP - makra,
    osadzone obiekty, zewnetrzne relacje. Bez modelu, same fakty."""
    import zipfile
    ustalenia = []
    try:
        z = zipfile.ZipFile(BytesIO(data))
        nazwy = set(z.namelist())
    except Exception:
        return {"ocena": "uwaga",
                "podsumowanie": "Plik nie otwiera się jako poprawne archiwum DOCX.",
                "ustalenia": [{"element": "format pliku", "ryzyko": "srednie",
                               "opis": "plik nie jest poprawnym archiwum docx (zip)"}],
                "czyszczenie": False}
    if "word/vbaProject.bin" in nazwy:
        ustalenia.append({"element": "makra VBA", "ryzyko": "wysokie",
                          "opis": "dokument zawiera makra (word/vbaProject.bin) - prawdziwy .docx nie powinien ich miec; makra potrafia uruchamiac kod"})
    for n in sorted(nazwy):
        if n.startswith("word/embeddings/") and not n.endswith("/"):
            ustalenia.append({"element": "osadzony obiekt", "ryzyko": "srednie",
                              "opis": "osadzony plik/obiekt: " + n + " - moze kryc obca tresc albo program"})
    szablon_zewn, zewnetrzne = [], []
    for n in sorted(nazwy):
        if n.endswith(".rels"):
            try:
                rels_xml = z.read(n).decode("utf-8", "ignore")
            except Exception:
                continue
            for el_m in re.finditer(r"<Relationship\b[^>]*>", rels_xml):
                el = el_m.group(0)
                if 'TargetMode="External"' not in el:
                    continue
                cel_m = re.search(r'Target="([^"]+)"', el)
                typ_m = re.search(r'Type="([^"]+)"', el)
                cel = cel_m.group(1) if cel_m else "?"
                typ = typ_m.group(1) if typ_m else ""
                if "attachedTemplate" in typ:
                    szablon_zewn.append(cel)
                elif "hyperlink" not in typ:
                    zewnetrzne.append(cel)
    for cel in szablon_zewn:
        ustalenia.append({"element": "zewnętrzny szablon", "ryzyko": "wysokie",
                          "opis": "przy otwarciu dokument pobiera szablon spoza pliku: " + cel})
    for cel in zewnetrzne:
        ustalenia.append({"element": "zewnętrzna relacja", "ryzyko": "srednie",
                          "opis": "przy otwarciu dokument siega na zewnatrz: " + cel})
    try:
        ct = z.read("[Content_Types].xml").decode("utf-8", "ignore")
        if "macroEnabled" in ct:
            ustalenia.append({"element": "typ makr", "ryzyko": "srednie",
                              "opis": "deklaracja typu pliku dopuszcza makra (macroEnabled)"})
    except Exception:
        pass
    czyszczenie = any(u["element"] in ("makra VBA", "osadzony obiekt", "zewnętrzny szablon") for u in ustalenia)
    if any(u["ryzyko"] == "wysokie" for u in ustalenia):
        ocena = "podejrzany"
    elif ustalenia:
        ocena = "uwaga"
    else:
        ocena = "czysty"
    podsumowanie = ("Inspekcja mechaniczna: " + str(len(nazwy)) + " części w archiwum; "
                    + ("wykryto elementy wymagające uwagi (poniżej)." if ustalenia
                       else "brak makr, osadzonych obiektów i zewnętrznych relacji."))
    return {"ocena": ocena, "podsumowanie": podsumowanie,
            "ustalenia": ustalenia, "czyszczenie": czyszczenie}


def _wyczysc_docx(data: bytes):
    """AUDYT_V68: usuwa z DOCX makra, osadzone obiekty i zewnetrzny szablon.
    Zwraca (nowe_bajty|None, usuniete[], blad|None)."""
    import zipfile
    import xml.etree.ElementTree as ET
    usuniete = []
    try:
        zin = zipfile.ZipFile(BytesIO(data))
        czesci = {n: zin.read(n) for n in zin.namelist()}
    except Exception:
        return None, [], "plik nie jest poprawnym archiwum docx"
    do_usuniecia = set()
    if "word/vbaProject.bin" in czesci:
        do_usuniecia.add("word/vbaProject.bin")
        usuniete.append("makra (word/vbaProject.bin)")
    for n in list(czesci):
        if n.startswith("word/embeddings/") and not n.endswith("/"):
            do_usuniecia.add(n)
            usuniete.append("osadzony obiekt " + n)
    for n in list(czesci):
        if n.endswith(".rels"):
            try:
                root = ET.fromstring(czesci[n])
            except Exception:
                continue
            zmieniono = False
            for rel in list(root):
                target = rel.get("Target") or ""
                typ = rel.get("Type") or ""
                zewn = rel.get("TargetMode") == "External"
                bazowy = n.rsplit("/_rels/", 1)[0] if "/_rels/" in n else ""
                pelna = (bazowy + "/" + target).replace("//", "/").lstrip("/")
                if pelna in do_usuniecia or (zewn and "attachedTemplate" in typ):
                    root.remove(rel)
                    zmieniono = True
                    if zewn and "attachedTemplate" in typ:
                        usuniete.append("zewnętrzny szablon (" + target + ")")
            if zmieniono:
                czesci[n] = ET.tostring(root, encoding="UTF-8", xml_declaration=True)
    if not usuniete:
        return None, [], "nie znaleziono elementów do usunięcia"
    if "[Content_Types].xml" in czesci:
        try:
            root = ET.fromstring(czesci["[Content_Types].xml"])
            for el in list(root):
                if (el.get("PartName") or "").lstrip("/") in do_usuniecia:
                    root.remove(el)
            czesci["[Content_Types].xml"] = ET.tostring(root, encoding="UTF-8", xml_declaration=True)
        except Exception:
            pass
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for n, b in czesci.items():
            if n not in do_usuniecia:
                zout.writestr(n, b)
    nowe = buf.getvalue()
    if not _docx_ok(nowe):
        return None, [], "po czyszczeniu dokument nie przechodzi kontroli - oryginał bez zmian"
    return nowe, usuniete, None


def _lokalny_audyt_tekst(api_key, req):
    """AUDYT_V68: ocena bezpieczenstwa pliku tekstowego/kodu przez model,
    na bazie mechanicznego skanu z przegladarki + tresci albo fragmentow."""
    skan = req.skan or {}
    domeny = skan.get("domeny") or []
    flagi = skan.get("flagi") or []
    opis_skanu = "Domeny/adresy w pliku: " + (", ".join(domeny[:25]) if domeny else "brak") + "\n"
    if flagi:
        opis_skanu += "Wykryte wzorce:\n" + "\n".join(
            "- [" + str(f.get("typ", "?")) + "] " + str(f.get("opis", "")) + " :: " + str(f.get("fragment", ""))[:160]
            for f in flagi[:20]) + "\n"
    else:
        opis_skanu += "Wykryte wzorce: brak znanych podejrzanych wzorców.\n"
    if req.tresc:
        material = "TREŚĆ PLIKU:\n" + req.tresc
    elif req.fragmenty:
        material = "FRAGMENTY PLIKU (z podejrzanymi wzorcami):\n" + "\n---\n".join(req.fragmenty)
    else:
        material = "(brak treści - oceń po samym skanie)"
    prompt = ("[SYSTEM – AUDYT BEZPIECZEŃSTWA PLIKU] Plik " + req.filename + " wskazany przez użytkownika na jego komputerze. "
              "Oceń, czy plik jest bezpieczny: czy kradnie dane (hasła, ciasteczka, formularze), łączy się z podejrzanymi miejscami, ukrywa kod, instaluje coś, kopie kryptowaluty, rejestruje klawisze, pobiera i uruchamia inny kod. "
              "Zwykłe, jawne funkcje (własny formularz do własnej domeny, znane biblioteki z CDN, analityka) to co najwyżej 'uwaga'. "
              "Wysyłanie haseł/ciasteczek do obcych domen, keylogger, miner, zaciemniony kod pobierany z sieci = 'zlosliwy' albo 'podejrzany'. "
              "Odpowiedz WYŁĄCZNIE obiektem JSON: {\"ocena\": \"czysty|uwaga|podejrzany|zlosliwy\", \"podsumowanie\": \"2-3 zdania po polsku, konkretnie\", "
              "\"ustalenia\": [{\"element\": \"...\", \"ryzyko\": \"wysokie|srednie|niskie\", \"opis\": \"...\"}], "
              "\"czyszczenie\": true/false} - czyszczenie=true tylko wtedy, gdy podejrzane elementy da się wyciąć, a plik zachowa swoją normalną funkcję. "
              "Opieraj się wyłącznie na materiale poniżej; gdy materiału jest za mało, wybierz 'uwaga' i napisz, czego brakuje.\n"
              + opis_skanu + material)
    try:
        odp = _call_llm_rotacja(api_key, [{'role': 'user', 'parts': [prompt]}], SYS_MECH, max_out=1200)
    except PauzaBrakKluczy:
        return {"pauza_kluczy": True}
    if not odp:
        if _rotacja_brak_kluczy():
            return {"pauza_kluczy": True}
        return {"blad": "Brak odpowiedzi modelu – spróbuj ponownie."}
    plan = None
    mj = re.search(r"\{.*\}", odp, re.DOTALL)
    if mj:
        try:
            plan = json.loads(mj.group(0))
        except Exception:
            plan = None
    if not isinstance(plan, dict) or "ocena" not in plan:
        return {"ocena": "uwaga", "podsumowanie": odp.strip()[:700],
                "ustalenia": [], "czyszczenie": False}
    ocena = str(plan.get("ocena") or "uwaga")
    if ocena not in ("czysty", "uwaga", "podejrzany", "zlosliwy"):
        ocena = "uwaga"
    ustalenia = [u for u in (plan.get("ustalenia") or []) if isinstance(u, dict)][:15]
    return {"ocena": ocena, "podsumowanie": str(plan.get("podsumowanie") or "")[:700],
            "ustalenia": ustalenia, "czyszczenie": bool(plan.get("czyszczenie"))}


class LocalDokumentRequest(BaseModel):
    filename: str
    instrukcja: str
    dane_b64: str
    apiKey: Optional[str] = Field(None, alias="apiKey")


@app.post("/local-edit/analizuj")
def local_edit_analizuj(r: LocalAnalizaRequest, user: str = Depends(get_current_user)):
    print(f"ANALIZA: rodzaj={r.rodzaj} plik={r.filename} znaki={r.znaki} wersja_frontu={r.wersja}", flush=True)
    return _lokalna_analiza(r.apiKey, r)


@app.post("/local-edit/caly")
def local_edit_caly(r: LocalCalyRequest, user: str = Depends(get_current_user)):
    nowa, odmowa, pauza = _lokalny_caly(r.apiKey, r.instrukcja, r.filename, r.tresc)
    return {"nowa_tresc": nowa, "odmowa": odmowa, "pauza_kluczy": pauza}


@app.post("/local-edit/dokument")
def local_edit_dokument(r: LocalDokumentRequest, user: str = Depends(get_current_user)):
    try:
        data = base64.b64decode(r.dane_b64 or "")
    except Exception:
        return {"blad": "uszkodzone dane dokumentu"}
    nowe, odpowiedz, pauza, wykonane = _lokalny_dokument(r.apiKey, r.instrukcja, r.filename, data)
    return {"nowa_b64": base64.b64encode(nowe).decode() if nowe else None,
            "odpowiedz": odpowiedz, "pauza_kluczy": pauza, "operacje": wykonane,
            "akapit_teksty": _akapity_docx(nowe) if nowe else []}


@app.post("/local-edit/podglad-dokumentu")
def local_edit_podglad_dokumentu(r: LocalPodgladDokumentRequest, user: str = Depends(get_current_user)):
    try:
        data = base64.b64decode(r.dane_b64 or "")
    except Exception:
        return {"blad": "uszkodzone dane dokumentu"}
    return {"akapit_teksty": _akapity_docx(data)}


@app.post("/local-edit/audyt")
def local_edit_audyt(r: LocalAudytRequest, user: str = Depends(get_current_user)):
    if r.rodzaj == "docx":
        try:
            data = base64.b64decode(r.dane_b64 or "")
        except Exception:
            return {"blad": "uszkodzone dane dokumentu"}
        w = _audyt_docx(data)
        print(f"AUDYT: rodzaj=docx plik={r.filename} ocena={w['ocena']}", flush=True)
        return w
    w = _lokalny_audyt_tekst(r.apiKey, r)
    print(f"AUDYT: rodzaj=tekst plik={r.filename} ocena={w.get('ocena')}", flush=True)
    return w


@app.post("/local-edit/wyczysc-dokument")
def local_edit_wyczysc_dokument(r: LocalWyczyscDokumentRequest, user: str = Depends(get_current_user)):
    try:
        data = base64.b64decode(r.dane_b64 or "")
    except Exception:
        return {"blad": "uszkodzone dane dokumentu"}
    nowe, usuniete, blad = _wyczysc_docx(data)
    if blad:
        return {"blad": blad}
    print(f"AUDYT-CZYSZCZENIE: plik={r.filename} usuniete={usuniete}", flush=True)
    return {"nowa_b64": base64.b64encode(nowe).decode(), "usuniete": usuniete}


# --- PLIK_W_TLE_V8: tworzenie dużego pliku jako zadanie w tle (karta postępu) ---
# Duży plik nie blokuje rozmowy: /chat zwraca od razu kartę zadania, a worker
# w osobnym wątku dopisuje kolejne części pliku (każda = osobne wywołanie LLM,
# rotacja kluczy jak w PLIK_NA_RATY_V7). Użytkownik może w trakcie dalej
# rozmawiać, a z karty: wstrzymać (pauza), wznowić (play), zatrzymać (stop –
# częściowy plik zostaje do pobrania) albo usunąć zadanie z kartą.
FILE_JOBS: dict = {}
FILE_JOB_MAX_PARTS = 24
FILE_JOB_MAX_CHARS = 400_000
FILE_JOBS_BOX_FOLDER = "My_bro files/filejobs"  # wspólny folder My Bro w Boxie (porządki użytkownika)
FILE_JOBS_BOX_FOLDER_LEGACY = "filejobs"  # dawna lokalizacja – czytamy też stąd, żeby stare zadania dały się wznowić

# --- Trwałość w Boxie: postęp zadania zapisuje się w chmurze użytkownika ---
# Dzięki temu po restarcie serwera albo po przywołaniu zapisanej rozmowy
# zadanie da się wznowić od miejsca wstrzymania, a nie od początku.
def _file_job_box_path(job_id: str) -> str:
    return f"{FILE_JOBS_BOX_FOLDER}/{job_id}.json"

def _file_job_box_path_legacy(job_id: str) -> str:
    return f"{FILE_JOBS_BOX_FOLDER_LEGACY}/{job_id}.json"

def _file_job_load_snapshot(job_id: str) -> dict | None:
    """Wczytuje zapis postępu zadania z Boxa – najpierw z nowego folderu
    'My_bro files/filejobs', a jak tam nie ma, z dawnego 'filejobs'."""
    data = _box_download_json(_file_job_box_path(job_id))
    if data:
        return data
    return _box_download_json(_file_job_box_path_legacy(job_id))

def _box_upload_json(path_in_box: str, obj: dict) -> bool:
    # ODPORNOSC_V60: do 3 prób z odstępami – darmowy Space Boxa potrafi
    # spać i budzić się w trakcie zapisu (w logach: 502 i timeouty 60 s),
    # a jedna próba gubiła migawkę postępu zadania.
    for _proba in range(3):
        try:
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            files = {"file": (os.path.basename(path_in_box), data, "application/json")}
            r = requests.post(f"{BOX_BACKEND_URL}/upload", headers=_box_headers(),
                              files=files, data={"path": os.path.dirname(path_in_box)}, timeout=60)
            r.raise_for_status()
            return True
        except Exception as e:
            if _proba == 2:
                print(f"BOX save error ({path_in_box}): {e}")
                return False
            time.sleep(3 if _proba == 0 else 8)
    return False

def _box_download_json(path_in_box: str) -> dict | None:
    # ODPORNOSC_V60: 2 próby – pierwsza potrafi trafić na budzący się Box.
    for _proba in range(2):
        try:
            enc = requests.utils.quote(path_in_box, safe="")
            bt = _box_login()
            r = requests.get(f"{BOX_BACKEND_URL}/download/{enc}?token={bt}&preview=true", timeout=30)
            r.raise_for_status()
            data = r.json()
            break
        except Exception as e:
            if _proba == 1:
                print(f"BOX load error ({path_in_box}): {e}")
                return None
            time.sleep(4)
    try:
        if isinstance(data, dict):
            if "content" in data or "job_id" in data:
                return data
            raw = data.get("data")
            if isinstance(raw, str):
                try:
                    return json.loads(base64.b64decode(raw).decode("utf-8"))
                except Exception:
                    try:
                        return json.loads(raw)
                    except Exception:
                        return None
            # Zwykly slownik bez koperty tez jest trescia pliku (np.
            # {"token": ...} straznika albo {"polecenia": [...]}): wczesniej
            # taki odczyt wracal jako "brak pliku", co m.in. w kolko
            # rotowalo kod parowania i konczylo sie 403 na strazniku.
            return data
        return None
    except Exception as e:
        print(f"BOX load error ({path_in_box}): {e}")
        return None

def _box_delete_file(path_in_box: str) -> bool:
    # ODPORNOSC_V60: jak przy zapisie – do 3 prób (w logach same 502).
    for _proba in range(3):
        try:
            resp = requests.post(f"{BOX_BACKEND_URL}/delete", headers=_box_headers(),
                                 json={"path": path_in_box}, timeout=20)
            resp.raise_for_status()
            return True
        except Exception as e:
            if _proba == 2:
                print(f"BOX delete error ({path_in_box}): {e}")
                return False
            time.sleep(3 if _proba == 0 else 8)
    return False


# STRAZNIK_V83: skrzynka polecen lokalnego straznika - "rak" agenta na
# urzadzeniach uzytkownika. Polecenia zyja w Boxie uzytkownika
# ('My_bro files/guard/polecenia.json'); program straznika pobiera je
# cyklicznie spod backendu z kodem parowania (token w osobnym pliku Boxa)
# i raportuje wykonanie. Zamknieta lista akcji: blokuj / odblokuj internet
# wskazanemu programowi .exe (po stronie PC wykonuje to Zapora Windows).
GUARD_POLE_PATH = "My_bro files/guard/polecenia.json"
GUARD_TOKEN_PATH = "My_bro files/guard/token.json"


def _guard_token() -> str:
    # Pusty odczyt NIE tworzy od razu nowego kodu: chwilowa awaria Boxa
    # zamieniala kod na nowy i rozparowywala straznikow (403 'zly kod
    # parowania' na urzadzeniach). Odczyt ponawiamy; nowy kod powstaje
    # tylko, gdy pliku z kodem naprawde nie ma.
    for _ in range(3):
        dane = _box_download_json(GUARD_TOKEN_PATH) or {}
        tok = dane.get("token")
        if tok:
            return tok
        time.sleep(0.4)
    tok = secrets.token_urlsafe(18)
    _box_upload_json(GUARD_TOKEN_PATH, {"token": tok})
    return tok


def _guard_czytaj() -> list:
    dane = _box_download_json(GUARD_POLE_PATH) or {}
    lista = dane.get("polecenia")
    return lista if isinstance(lista, list) else []


def _guard_zapisz(lista: list) -> bool:
    return _box_upload_json(GUARD_POLE_PATH, {"polecenia": lista[-100:]})


GUARD_URZADZENIA_PATH = "My_bro files/guard/urzadzenia.json"


def _guard_urzadzenia() -> dict:
    dane = _box_download_json(GUARD_URZADZENIA_PATH) or {}
    urz = dane.get("urzadzenia")
    return urz if isinstance(urz, dict) else {}


def _guard_urzadzenia_zapisz(urz: dict) -> bool:
    return _box_upload_json(GUARD_URZADZENIA_PATH, {"urzadzenia": urz})


def _guard_dotkniecie_urzadzenia(klucz: str, nazwa: str = "", rodzaj: str = ""):
    # heartbeat straznika: zapis rzadziej niz co 45 s, zeby nie pisac do
    # Boxa przy kazdym odpytaniu skrzynki
    klucz = (klucz or "").strip()
    if not klucz:
        return
    urz = _guard_urzadzenia()
    wpis = urz.get(klucz) or {"programy": []}
    teraz = datetime.utcnow()
    try:
        wiek = (teraz - datetime.fromisoformat(wpis.get("ostatnio") or "")).total_seconds()
    except Exception:
        wiek = 9999
    wpis["nazwa"] = nazwa or wpis.get("nazwa") or klucz
    wpis["rodzaj"] = rodzaj or wpis.get("rodzaj") or ""
    if wiek < 45 and urz.get(klucz):
        return
    wpis["ostatnio"] = teraz.isoformat()
    wpis.setdefault("programy", [])
    urz[klucz] = wpis
    _guard_urzadzenia_zapisz(urz)


def _guard_program_stan(klucz: str, program: str, stan: str):
    # stan: 'zablokowany' / 'na liscie' / '' (usuniecie z listy urzadzenia)
    klucz = (klucz or "").strip()
    if not klucz or not program:
        return
    urz = _guard_urzadzenia()
    wpis = urz.get(klucz) or {"programy": []}
    programy = [x for x in wpis.get("programy", []) if x.get("program") != program]
    if stan:
        programy.append({"program": program, "stan": stan,
                         "od": datetime.utcnow().isoformat()})
    wpis["programy"] = programy[-50:]
    urz[klucz] = wpis
    _guard_urzadzenia_zapisz(urz)


def _guard_urzadzenie_usun(klucz: str):
    # po udanym odinstalowaniu straznik znika z rejestru urzadzen
    klucz = (klucz or "").strip()
    if not klucz:
        return
    urz = _guard_urzadzenia()
    if klucz in urz:
        del urz[klucz]
        _guard_urzadzenia_zapisz(urz)


def _guard_dodaj(akcja: str, program: str, urzadzenie: str = ""):
    akcja = (akcja or "").strip().lower()
    program = (program or "").strip().strip('"')
    if akcja not in ("blokuj", "odblokuj", "zabij", "kwarantanna", "awaryjne", "lista", "odinstaluj", "obserwacje"):
        return None, "nieznana akcja (dozwolone: blokuj, odblokuj, zabij, kwarantanna, awaryjne, lista, odinstaluj, obserwacje)"
    if akcja == "awaryjne":
        program = program or "wszystko"
    elif akcja in ("lista", "odinstaluj", "obserwacje"):
        program = ""
    elif not (program.lower().endswith(".exe")
            or re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+", program)):
        return None, "polecenie dotyczy programu .exe (komputer) albo pakietu aplikacji (telefon)"
    lista = _guard_czytaj()
    pol = {"id": uuid.uuid4().hex[:12], "akcja": akcja, "program": program,
           "urzadzenie": (urzadzenie or "").strip(),
           "status": "oczekuje", "kiedy": datetime.utcnow().isoformat(),
           "wynik": "", "komunikat": ""}
    lista.append(pol)
    if not _guard_zapisz(lista):
        return None, "nie udalo sie zapisac polecenia w Boxie"
    return pol, ""


class GuardPolecenieRequest(BaseModel):
    akcja: str
    program: str = ""
    urzadzenie: str = ""



class GuardRaportRequest(BaseModel):
    token: str
    id: str
    wynik: str
    komunikat: str = ""
    urzadzenie: str = ""


@app.get("/guard/token")
def guard_token(user: str = Depends(get_current_user)):
    return {"token": _guard_token()}


@app.post("/guard/polecenie")
def guard_polecenie(r: GuardPolecenieRequest, user: str = Depends(get_current_user)):
    pol, blad = _guard_dodaj(r.akcja, r.program, r.urzadzenie)
    if blad:
        raise HTTPException(400, blad)
    return {"ok": True, "id": pol["id"]}


@app.get("/guard/polecenia-dla-straznika")
def guard_polecenia_dla_straznika(token: str = "", urzadzenie: str = "", nazwa: str = "", rodzaj: str = ""):
    if not secrets.compare_digest(token or "", _guard_token()):
        raise HTTPException(403, "zly kod parowania")
    _guard_dotkniecie_urzadzenia(urzadzenie, nazwa, rodzaj)
    return {"polecenia": [{"id": p["id"], "akcja": p["akcja"], "program": p["program"]}
                          for p in _guard_czytaj()
                          if p.get("status") == "oczekuje"
                          and (not p.get("urzadzenie") or not urzadzenie or p.get("urzadzenie") == urzadzenie)]}


@app.post("/guard/raport")
def guard_raport(r: GuardRaportRequest):
    if not secrets.compare_digest(r.token or "", _guard_token()):
        raise HTTPException(403, "zly kod parowania")
    lista = _guard_czytaj()
    for p in lista:
        if p.get("id") == r.id:
            p["status"] = "wykonane" if r.wynik == "ok" else "blad"
            p["wynik"] = r.wynik
            p["komunikat"] = r.komunikat
            _guard_zapisz(lista)
            if r.wynik == "ok":
                urz = (r.urzadzenie or p.get("urzadzenie") or "").strip()
                if p["akcja"] in ("blokuj", "kwarantanna") and p.get("program"):
                    _guard_program_stan(urz, p["program"], "zablokowany")
                elif p["akcja"] == "odblokuj" and p.get("program"):
                    _guard_program_stan(urz, p["program"], "")
                elif p["akcja"] == "odinstaluj" and urz:
                    _guard_urzadzenie_usun(urz)
            return {"ok": True}
    raise HTTPException(404, "nie ma takiego polecenia")


class GuardListaRequest(BaseModel):
    token: str
    urzadzenie: str = ""
    programy: list = []


@app.post("/guard/lista-programow")
def guard_lista_programow(r: GuardListaRequest):
    # straznik przysyla swoj rzeczywisty stan blokad (synchronizacja listy)
    if not secrets.compare_digest(r.token or "", _guard_token()):
        raise HTTPException(403, "zly kod parowania")
    klucz = (r.urzadzenie or "").strip()
    if not klucz:
        raise HTTPException(400, "brak urzadzenia")
    urz = _guard_urzadzenia()
    wpis = urz.get(klucz) or {"programy": []}
    reczne = [x for x in wpis.get("programy", []) if x.get("stan") == "na liscie"]
    widziane = {x.get("program") for x in reczne}
    teraz = datetime.utcnow().isoformat()
    for x in r.programy:
        if isinstance(x, str) and x and x not in widziane:
            reczne.append({"program": x, "stan": "zablokowany", "od": teraz})
            widziane.add(x)
    wpis["programy"] = reczne[-50:]
    urz[klucz] = wpis
    _guard_urzadzenia_zapisz(urz)
    return {"ok": True}


class GuardProgramyRequest(BaseModel):
    urzadzenie: str
    program: str
    operacja: str


@app.post("/guard/programy")
def guard_programy(r: GuardProgramyRequest, user: str = Depends(get_current_user)):
    program = (r.program or "").strip().strip('"')
    klucz = (r.urzadzenie or "").strip()
    if not klucz:
        raise HTTPException(400, "brak urzadzenia")
    if r.operacja == "dodaj":
        if not (program.lower().endswith(".exe")
                or re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+", program)):
            raise HTTPException(400, "program to pelna sciezka .exe albo nazwa pakietu aplikacji")
        urz = _guard_urzadzenia()
        wpis = urz.get(klucz) or {"programy": []}
        if any(x.get("program") == program for x in wpis.get("programy", [])):
            return {"ok": True}
        _guard_program_stan(klucz, program, "na liscie")
    elif r.operacja == "usun":
        _guard_program_stan(klucz, program, "")
    else:
        raise HTTPException(400, "operacja: dodaj albo usun")
    return {"ok": True}


GUARD_ZDARZENIA_PATH = "My_bro files/guard/zdarzenia.json"


def _guard_czytaj_zdarzenia() -> list:
    dane = _box_download_json(GUARD_ZDARZENIA_PATH) or {}
    lista = dane.get("zdarzenia")
    return lista if isinstance(lista, list) else []


class GuardZdarzenieRequest(BaseModel):
    token: str
    rodzaj: str = "alert"
    tekst: str
    urzadzenie: str = ""


@app.post("/guard/zdarzenie")
def guard_zdarzenie(r: GuardZdarzenieRequest):
    if not secrets.compare_digest(r.token or "", _guard_token()):
        raise HTTPException(403, "zly kod parowania")
    tekst = (r.tekst or "").strip()
    if not tekst:
        raise HTTPException(400, "puste zdarzenie")
    if (r.rodzaj or "").strip().lower() == "odinstaluj" and (r.urzadzenie or "").strip():
        # odinstalowanie z okna programu: urzadzenie znika z karty
        _guard_urzadzenie_usun(r.urzadzenie)
    lista = _guard_czytaj_zdarzenia()
    lista.append({"id": uuid.uuid4().hex[:12], "kiedy": datetime.utcnow().isoformat(),
                  "rodzaj": r.rodzaj if r.rodzaj in ("alert", "podsumowanie", "odinstaluj") else "alert",
                  "tekst": tekst[:600]})
    _box_upload_json(GUARD_ZDARZENIA_PATH, {"zdarzenia": lista[-200:]})
    return {"ok": True}


@app.get("/guard/stan")
def guard_stan(user: str = Depends(get_current_user)):
    teraz = datetime.utcnow()
    urzadzenia = []
    for klucz, wpis in _guard_urzadzenia().items():
        try:
            wiek = (teraz - datetime.fromisoformat(wpis.get("ostatnio") or "")).total_seconds()
        except Exception:
            wiek = 999999
        urzadzenia.append({"klucz": klucz, "nazwa": wpis.get("nazwa") or klucz,
                           "rodzaj": wpis.get("rodzaj") or "",
                           "online": wiek <= 150, "wiek_s": int(wiek) if wiek < 999999 else None,
                           "programy": wpis.get("programy") or []})
    return {"polecenia": list(reversed(_guard_czytaj()))[:20],
            "zdarzenia": list(reversed(_guard_czytaj_zdarzenia()))[:20],
            "urzadzenia": urzadzenia}


STRAZNIK_PROMPT = """

TRYB STRAZNIKA (aktywny): lokalny program straznika na urzadzeniach uzytkownika wykonuje Twoje polecenia i raportuje. Akcje wydajesz linia na samym koncu odpowiedzi, dokladnie w formacie [STRAZNIK: akcja; cel]:
- blokuj / odblokuj: blokada albo zdjecie blokady internetu dla programu - cel to PELNA sciezka do pliku .exe (komputer) albo nazwa pakietu aplikacji (telefon),
- zabij: zamkniecie dzialajacego programu (cel jak wyzej) - tylko programy uzytkownika, nigdy systemowe,
- kwarantanna: blokada internetu + zamkniecie programu naraz (cel jak wyzej),
- awaryjne: NATYCHMIASTOWE odciecie calego internetu na 15 minut (cel: wszystko) - WYLACZNIE na wyrazne zadanie uzytkownika przy powaznym zagrozeniu; straznik sam przywroci internet po 15 minutach,
- odinstaluj: odinstalowanie samego straznika z tego urzadzenia (cel: straznik) - tylko na wyrazne zadanie uzytkownika; straznik zdejmuje wlasne blokady i autostart, raportuje wykonanie i sam sie usuwa, po czym znika z karty urzadzen,
- obserwacje: raport z cichych obserwacji monitoringu (cel: straznik) - uzyj, gdy uzytkownik pyta, co straznik zauwazyl, czy cos sie dzialo albo czy wszystko spokojnie.
Backend zamienia te linie na polecenia i usuwa je z widocznej odpowiedzi. Gdy uzytkownik poda tylko nazwe programu bez pelnej sciezki, NIE zgaduj sciezki - popros o pelna sciezke do pliku .exe. Tych tagow uzywaj wylacznie do akcji straznika. Straznik z zalozenia MILCZY: monitoruje system po cichu i NIE przysyla biezacych raportow - sam z siebie zglasza TYLKO krytyczne wykrycia klasy zlosliwego oprogramowania (z propozycja dzialania) oraz wykrycia Windows Defendera. Drobniejsze obserwacje trzyma u siebie; gdy uzytkownik pyta, co straznik zauwazyl albo czy wszystko spokojnie, wydaj polecenie [STRAZNIK: obserwacje; straznik] - straznik przysle ostatnie obserwacje jako raport na czat. Na karcie urzadzen uzytkownik moze ZAZNACZAC programy - jezeli w kontekscie widzisz liste zaznaczonych programow, to uzytkownik omawia wlasnie TE programy i polecenia wydajesz dla nich. Polecenia trafiaja na urzadzenie wybrane na karcie (backend dopisuje je sam)."""

_STRAZNIK_TAG_RE = re.compile(r"\[STRAZNIK:\s*(blokuj|odblokuj|zabij|kwarantanna|awaryjne|odinstaluj|obserwacje)\s*;\s*([^\]]+)\]", re.IGNORECASE)


def _straznik_kontekst_wybranych(r) -> str:
    wybrane = getattr(r, "straznik_wybrane", None) or []
    linie = []
    for w in wybrane[:20]:
        if isinstance(w, dict) and w.get("program"):
            linie.append(f"- {w['program']} (urzadzenie: {w.get('urzadzenie') or '?'})")
    if not linie:
        return ""
    return ("\n\nZAZNACZONE NA KARCIE URZADZEN programy (gdy uzytkownik mowi "
            "'te programy' albo 'zaznaczone', chodzi dokladnie o te):\n" + "\n".join(linie))


def _straznik_wykonaj_tagi(tekst: str, urzadzenie: str = "") -> str:
    if not tekst or "[STRAZNIK:" not in tekst:
        return tekst
    statusy = []

    def _podmien(m):
        pol, blad = _guard_dodaj(m.group(1), m.group(2), urzadzenie)
        if blad:
            statusy.append(f"\u26a0\ufe0f Straznik: nie wyslalem polecenia ({blad}).")
        elif pol["akcja"] == "odinstaluj":
            statusy.append("\U0001f4e4 Straznik: wyslalem polecenie - odinstalowanie straznika z urzadzenia. Raport z wykonania pojawi sie na czacie.")
        else:
            co = "zablokowanie internetu" if pol["akcja"] == "blokuj" else "odblokowanie internetu"
            statusy.append(f"\U0001f4e4 Straznik: wyslalem polecenie - {co}: {pol['program']}. Raport z wykonania pojawi sie na czacie.")
        return ""

    czysty = _STRAZNIK_TAG_RE.sub(_podmien, tekst).strip()
    if statusy:
        czysty = (czysty + "\n\n" + "\n".join(statusy)).strip()
    return czysty

def _file_job_save(job_id: str):
    """Zapisuje pełny postęp zadania (z treścią pliku) do Boxa. Nigdy nie
    wywraca workera. ODPORNOSC_V60: zapisy tego samego zadania są łączone
    – checkpointy potrafią iść seriami po kilka na minutę, a każdy zapis
    to pełna treść pliku i osobny wątek; gdy zapis już trwa, kolejne
    wywołania tylko zaznaczają potrzebę odświeżenia, a trwający zapis
    robi dodatkowy przebieg ze świeżym stanem."""
    job = FILE_JOBS.get(job_id)
    if job is None:
        return
    if job.get("_save_busy"):
        job["_save_dirty"] = True
        return
    job["_save_busy"] = True
    try:
        for _ in range(4):
            job["_save_dirty"] = False
            _file_job_save_once(job_id)
            if not job.get("_save_dirty"):
                break
    finally:
        job["_save_busy"] = False


def _file_job_save_once(job_id: str):
    job = FILE_JOBS.get(job_id)
    if job is None:
        return
    try:
        snapshot = {
            "job_id": job_id,
            "filename": job["filename"],
            "status": job["status"],
            "kind": job.get("kind", "file"),
            "error": job.get("error"),
            "parts": job["parts"],
            "chars": job["chars"],
            "estimate": job["estimate"],
            "truncated": job["truncated"],
            "content": "".join(job["content_parts"]),
            "plan": job.get("plan"),
            "info": job.get("info"),
            "saved_at": datetime.utcnow().isoformat() + "Z",
        }
        if job.get("kind") == "edit":
            # PAUZA_KLUCZY_V34: pełny stan edycji kawałkowej – po wznowieniu
            # edycja idzie od pierwszego niezrobionego bloku, nie od zera.
            snapshot["edit"] = {
                "user_text": job.get("edit_user_text") or "",
                "content_orig": job.get("edit_content_orig") or "",
                "chosen_ids": job.get("edit_chosen") or [],
                "bloki": job.get("edit_bloki") or {},
                "total": job.get("edit_total") or 0,
                "zmienione": job.get("edit_zmienione") or [],
                "globale": job.get("edit_globale") or [],
            }
        if _box_upload_json(_file_job_box_path(job_id), snapshot):
            print(f"FILE_JOB {job_id}: postęp zapisany w Boxie ({job['chars']} znaków, część {job['parts']})")
    except Exception as e:
        print(f"FILE_JOB {job_id}: błąd zapisu do Boxa: {e}")

def _file_job_percent(job) -> int:
    if job["status"] == "done":
        return 100
    if job.get("kind") == "edit":
        total = job.get("edit_total") or 0
        if total <= 0:
            return 0
        return min(99, int(round(100 * (job.get("parts") or 0) / total)))
    chars = job["chars"]
    if chars <= 0:
        return 0
    # Procent liczony od szacunku modelu ([SZACUJĘ:]); gdy go brak albo plik
    # urósł ponad szacunek, mianownik rośnie razem z plikiem (zostaje < 100%).
    eff = max(job["estimate"] or 0, int(chars * 1.25), 8000)
    return min(99, int(round(100 * chars / eff)))

def _file_job_snapshot(job_id: str) -> dict | None:
    job = FILE_JOBS.get(job_id)
    if not job:
        return None
    return {
        "job_id": job_id,
        "status": job["status"],
        "kind": job.get("kind", "file"),
        "filename": job["filename"],
        "percent": _file_job_percent(job),
        "chars": job["chars"],
        "parts": job["parts"],
        "estimate": job["estimate"],
        "truncated": job["truncated"],
        "error": job["error"],
        "created": job.get("created"),
        "zmienione": list(job.get("edit_zmienione") or []),
        "info": job.get("info"),
        "plan": job.get("plan"),
    }

def _file_job_continuation_instr(filename: str, full_tail: str) -> str:
    tail = full_tail[-220:].replace("\n", " ")
    return (
        "[SYSTEM] Twój plik " + filename + " został ucięty w połowie "
        "(limit jednej odpowiedzi). Kontynuuj automatycznie, bez pytania "
        "użytkownika i bez żadnego komentarza. Wyślij TYLKO dalszy ciąg treści "
        "pliku " + filename + ", dokładnie od miejsca, w którym urwałeś. "
        "Ostatnie znaki, które już wysłałeś: „…" + tail + "” – NIE powtarzaj ich "
        "ani niczego, co już jest w pliku. Nie dodawaj tagu [FILE:] ani "
        "znaczników ```. Gdy plik będzie kompletny w całości, zakończ go "
        "tagiem [/FILE] i niczym więcej."
    )

def _file_job_worker(job_id: str):
    global current_key_index
    job = FILE_JOBS.get(job_id)
    if job is None:
        return
    msgs = job["_msgs"]
    last_raw = job["_last_raw"]
    # PLANISTA_V39: dla dłuższych plików najpierw krótki plan dokończenia
    # (jedno małe wywołanie na krótkiej instrukcji) – kontynuacje trzymają
    # się planu, więc długi plik/program powstaje jako zaplanowana całość.
    # Plan nigdy nie blokuje generowania: jakikolwiek błąd = idziemy bez niego.
    if job.get("kind", "file") == "file" and not job.get("plan") and not job.get("_plan_probowany"):
        job["_plan_probowany"] = True
        try:
            pierwsza = job["content_parts"][0] if job["content_parts"] else ""
            if (job.get("estimate") or 0) >= 6000 or len(pierwsza) >= 3500:
                spec_m = next((m for m in reversed(msgs) if m.get("role") == "user"), None)
                if spec_m is not None:
                    spec_txt = spec_m["parts"][0] if spec_m.get("parts") and isinstance(spec_m["parts"][0], str) else ""
                    plan_msgs = [spec_m,
                                 {"role": "model", "parts": [pierwsza[:2500]]},
                                 {"role": "user", "parts": [
                                     "[SYSTEM – PLAN PLIKU] Na podstawie polecenia użytkownika i początku pliku "
                                     + job["filename"] + " ułóż KRÓTKI plan jego dokończenia: lista 3–8 punktów "
                                     "(kolejne sekcje/funkcje/etapy) w kolejności realizacji, każdy w jednej "
                                     "krótkiej linii, format: '1. ...'. Zwróć TYLKO listę."
                                     + _mapy_dopisek(spec_txt, job["filename"])]}]
                    plan = _call_llm_rotacja(job.get("api_key"), plan_msgs, SYS_MECH, max_out=500)
                    if plan and len(plan.strip()) >= 10:
                        job["plan"] = plan.strip()[:1200]
                        job["info"] = f"Plan: {job['plan'].count(chr(10)) + 1} etapów"
                        print(f"FILE_JOB {job_id}: PLANISTA_V39 – plan gotowy ({len(job['plan'])} znaków)")
        except Exception as e:
            print(f"FILE_JOB {job_id}: PLANISTA_V39 pominięty ({e})")
    try:
        while True:
            if FILE_JOBS.get(job_id) is not job:
                return  # zadanie usunięte przez użytkownika
            if job["stop_requested"]:
                job["status"] = "stopped"
                job["truncated"] = True
                _file_job_save(job_id)
                return
            while job["status"] == "paused":
                time.sleep(0.4)
                if FILE_JOBS.get(job_id) is not job or job["stop_requested"]:
                    if job["stop_requested"]:
                        job["status"] = "stopped"
                        job["truncated"] = True
                        _file_job_save(job_id)
                    return
            if job["parts"] >= FILE_JOB_MAX_PARTS or job["chars"] >= FILE_JOB_MAX_CHARS:
                job["status"] = "done"
                job["truncated"] = True
                print(f"FILE_JOB {job_id}: limit części/rozmiaru – plik częściowy ({job['chars']} znaków)")
                _file_job_save(job_id)
                return
            full_tail = "".join(job["content_parts"])
            # DAWKOWANIE_V38: kontynuacja NIE dokleja całej dotychczasowej
            # wymiany z modelem (rosła z każdą częścią i zjadała limity) –
            # przy większym pliku wysyłamy stałe, małe okno: specyfikację
            # zadania + początek pliku (struktura i definicje) + koniec
            # pliku (ciągłość) + instrukcję kontynuacji.
            spec = next((m for m in reversed(msgs) if m.get("role") == "user"), None)
            if spec is not None and len(full_tail) > 4500:
                okno = (full_tail[:1500]
                        + "\n\n[…środek pliku pominięty w kontekście – liczą się początek i koniec…]\n\n"
                        + full_tail[-3000:])
                call_msgs = [spec,
                             {'role': 'model', 'parts': [okno]},
                             {'role': 'user', 'parts': [_file_job_continuation_instr(job["filename"], full_tail)]}]
            else:
                call_msgs = msgs + [
                    {'role': 'model', 'parts': [last_raw]},
                    {'role': 'user', 'parts': [_file_job_continuation_instr(job["filename"], full_tail)]},
                ]
            if job.get("plan"):
                # PLANISTA_V39: kontynuacja dostaje plan i ma realizować
                # jego kolejne punkty – spójność długiego pliku/programu.
                call_msgs[-1] = {'role': 'user', 'parts': [
                    call_msgs[-1]["parts"][0]
                    + "\nPLAN PLIKU – trzymaj się go i realizuj kolejne punkty po kolei "
                      "(nie powtarzaj punktów już zrealizowanych):\n" + job["plan"]]}
            keys_try = ([job["api_key"]] if job["api_key"] else []) + api_keys[current_key_index:] + api_keys[:current_key_index]
            keys_try = list(dict.fromkeys(keys_try))  # OSZCZEDNOSC_V37: bez duplikatów
            nxt = None
            przejsciowy = False
            for idx2, k2 in enumerate(keys_try):
                if idx2 > 15: break
                try:
                    nxt = call_llm(k2, call_msgs, SYSTEM_INSTRUCTION)
                    if k2 in api_keys: current_key_index = api_keys.index(k2)
                    break
                except Exception as e:
                    if _blad_przejsciowy(str(e)):
                        przejsciowy = True
                    print(f"FILE_JOB key fail: {e}")
            if not nxt:
                if przejsciowy:
                    # PAUZA_KLUCZY_V34: limity się odnowią – zadanie czeka na
                    # decyzję użytkownika (wznów / stop), postęp jest w Boxie.
                    job["status"] = "paused_keys"
                    job["error"] = "Brak dostępnych kluczy API – limity w puli wyczerpane. Zadanie wstrzymane z zapisanym postępem; wznów je, gdy limity się odnowią."
                    _file_job_save(job_id)
                    print(f"FILE_JOB {job_id}: PAUZA – brak kluczy, postęp zapisany w Boxie")
                    return
                job["status"] = "error"
                job["error"] = "Wszystkie klucze API zawiodły podczas tworzenia pliku."
                _file_job_save(job_id)
                return
            if job["stop_requested"] or FILE_JOBS.get(job_id) is not job:
                if FILE_JOBS.get(job_id) is job:
                    job["status"] = "stopped"
                    job["truncated"] = True
                    _file_job_save(job_id)
                return  # części z wywołania po stopie nie doklejamy
            cont = nxt.strip()
            cont = re.sub(r'^\[FILE:\s*[^\]]+?\]\s*', '', cont, flags=re.IGNORECASE)
            cont = re.sub(r'^```[\w]*\s*\n?', '', cont)
            closed = "[/FILE]" in cont.upper()
            if closed:
                cont = re.split(r'\[/FILE\]', cont, flags=re.IGNORECASE)[0]
                cont = re.sub(r'\n?```$', '', cont.rstrip())
            job["content_parts"].append(cont)
            job["chars"] += len(cont)
            job["parts"] += 1
            msgs = call_msgs
            last_raw = nxt
            # zapis kontekstu w zadaniu (żeby dało się je restartować po stopie)
            # oraz trwały zapis postępu w Boxie po każdej części
            job["_msgs"] = call_msgs
            job["_last_raw"] = nxt
            print(f"FILE_JOB {job_id}: część {job['parts']} (+{len(cont)} znaków, razem {job['chars']}, {_file_job_percent(job)}%)")
            if closed:
                # BRAMKA_PLIKU_V39: gotowy kod przechodzi kontrolę jakości
                # (jak edycje) zanim zadanie zamknie się jako ukończone.
                if (job["filename"] or "").lower().endswith((".html", ".htm", ".js", ".mjs")):
                    werdykt = _bramka_pliku(job_id, job)
                    if werdykt:
                        job["info"] = werdykt
                job["status"] = "done"
                job["truncated"] = False
            _file_job_save(job_id)
            if closed:
                # MAPY_V39: struktura gotowego pliku ląduje w Boxie.
                threading.Thread(target=_mapa_zapisz,
                                 args=(job["filename"], "".join(job["content_parts"])),
                                 daemon=True).start()
                return
    except Exception as e:
        if FILE_JOBS.get(job_id) is job:
            job["status"] = "error"
            job["error"] = str(e)

def create_file_job(filename: str, first_content: str, base_msgs: list, last_raw: str,
                    api_key: str | None, estimate: int | None) -> str:
    """Startuje tworzenie pliku w wątku w tle. Zwraca job_id."""
    job_id = uuid.uuid4().hex[:12]
    FILE_JOBS[job_id] = {
        "status": "running",
        "filename": filename,
        "content_parts": [first_content] if first_content else [],
        "chars": len(first_content or ""),
        "parts": 1,
        "estimate": estimate,
        "truncated": False,
        "error": None,
        "stop_requested": False,
        "api_key": api_key,
        "created": time.time(),
        "plan": None,
        "info": None,
        "_msgs": list(base_msgs),
        "_last_raw": last_raw,
    }
    # Wstępny zapis postępu w Boxie – w osobnym wątku, żeby nie opóźniać odpowiedzi czatu
    threading.Thread(target=_file_job_save, args=(job_id,), daemon=True).start()
    threading.Thread(target=_file_job_worker, args=(job_id,), daemon=True).start()
    print(f"FILE_JOB {job_id}: start tworzenia pliku {filename} w tle (szacunek: {estimate})")
    return job_id

class FileJobControl(BaseModel):
    action: str  # pause | resume | stop | delete

@app.get("/file-job/{job_id}")
def file_job_status_ep(job_id: str, user: str = Depends(get_current_user)):
    snap = _file_job_snapshot(job_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="Zadanie nie istnieje (mógł wygasnąć przy restarcie serwera).")
    return snap

@app.post("/file-job/{job_id}/control")
def file_job_control_ep(job_id: str, r: FileJobControl, user: str = Depends(get_current_user)):
    job = FILE_JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Zadanie nie istnieje (mógł wygasnąć przy restarcie serwera).")
    action = (r.action or "").strip().lower()
    if action == "pause":
        if job["status"] == "running":
            job["status"] = "paused"
    elif action == "resume":
        # PONOW_EDYCJE_V43: odrzucona przez bramkę edycja też daje się
        # ponowić z karty – worker wraca do checkpointu (wybór bloków
        # i podmienione treści są zapisane), więc ponawiana jest tylko
        # kontrola kodu z rundą naprawy, na świeżym stanie puli kluczy.
        if job["status"] in ("paused", "paused_keys") or \
                (job["status"] == "rejected" and job.get("kind") == "edit"):
            poprzedni = job["status"]
            job["status"] = "running"
            job["error"] = None
            if job.get("kind") == "edit":
                threading.Thread(target=_edit_job_worker, args=(job_id,), daemon=True).start()
            elif poprzedni == "paused_keys":
                threading.Thread(target=_file_job_worker, args=(job_id,), daemon=True).start()
    elif action == "stop":
        job["stop_requested"] = True
        if job["status"] in ("paused", "paused_keys"):
            job["status"] = "stopped"
            job["truncated"] = True
    elif action == "delete":
        job["stop_requested"] = True
        FILE_JOBS.pop(job_id, None)
        # Usuwamy też zapis postępu z Boxa (z nowego i dawnego folderu),
        # żeby nie dało się wznowić usuniętego zadania
        def _usun_obie_kopie():
            _box_delete_file(_file_job_box_path(job_id))
            _box_delete_file(_file_job_box_path_legacy(job_id))
        threading.Thread(target=_usun_obie_kopie, daemon=True).start()
        return {"status": "deleted", "job_id": job_id}
    else:
        raise HTTPException(status_code=400, detail=f"Nieznana akcja: {action}")
    if action in ("pause", "stop"):
        threading.Thread(target=_file_job_save, args=(job_id,), daemon=True).start()
    return _file_job_snapshot(job_id)

@app.get("/file-job/{job_id}/download")
def file_job_download_ep(job_id: str, user: str = Depends(get_current_user)):
    job = FILE_JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Zadanie nie istnieje (mógł wygasnąć przy restarcie serwera).")
    content = "".join(job["content_parts"])
    return make_file_response(job["filename"], content)

def _file_job_restore(job_id: str) -> dict | None:
    """Wznawia zadanie: z pamięci (pauza/stop) albo z zapisu w Boxie (po restarcie
    serwera / przywołaniu zapisanej rozmowy). Zwraca snapshot albo None, gdy nie
    ma ani zadania, ani zapisu w chmurze."""
    job = FILE_JOBS.get(job_id)
    if job is not None:
        if job["status"] in ("paused", "paused_keys", "stopped"):
            poprzedni = job["status"]
            job["stop_requested"] = False
            job["status"] = "running"
            job["truncated"] = False
            if job.get("kind") == "edit":
                threading.Thread(target=_edit_job_worker, args=(job_id,), daemon=True).start()
            elif poprzedni in ("paused_keys", "stopped"):
                # worker plikowy w tych stanach już nie żyje (pauza na klucze
                # go zakończyła) – trzeba go postawić od nowa; przy zwykłej
                # pauzie worker sam wychodzi z pętli oczekiwania.
                threading.Thread(target=_file_job_worker, args=(job_id,), daemon=True).start()
        return _file_job_snapshot(job_id)

    data = _file_job_load_snapshot(job_id)
    if not data or "content" not in data:
        return None
    content = data.get("content") or ""
    filename = data.get("filename") or "plik.txt"
    if data.get("kind") == "edit":
        ed = data.get("edit") or {}
        orig = ed.get("content_orig") or content
        FILE_JOBS[job_id] = {
            "kind": "edit",
            "status": "running",
            "filename": filename,
            "content_parts": [orig],
            "chars": len(orig),
            "parts": int(data.get("parts") or 0),
            "estimate": None,
            "truncated": False,
            "error": None,
            "stop_requested": False,
            "api_key": None,  # jak przy zadaniach plikowych: worker użyje puli
            "created": time.time(),
            "edit_user_text": ed.get("user_text") or "",
            "edit_content_orig": orig,
            "edit_chosen": ed.get("chosen_ids") or [],
            "edit_bloki": ed.get("bloki") or {},
            "edit_total": int(ed.get("total") or len(ed.get("chosen_ids") or [])),
            "edit_zmienione": ed.get("zmienione") or [],
            "edit_globale": ed.get("globale") or [],
        }
        print(f"EDIT_JOB {job_id}: odtworzono z Boxa – kontynuuję edycję {filename} w tle")
        threading.Thread(target=_edit_job_worker, args=(job_id,), daemon=True).start()
        return _file_job_snapshot(job_id)
    tail = content[-6000:]
    FILE_JOBS[job_id] = {
        "status": "running",
        "filename": filename,
        "content_parts": [content] if content else [],
        "chars": len(content),
        "parts": int(data.get("parts") or 1),
        "estimate": data.get("estimate"),
        "truncated": False,
        "error": None,
        "stop_requested": False,
        "api_key": None,  # prywatny klucz użytkownika nie jest zapisywany – worker użyje puli
        "created": time.time(),
        "plan": data.get("plan"),
        "info": None,
        # Odtworzony kontekst: model dostaje koniec pliku i polecenie kontynuacji,
        # więc dopisuje dalszy ciąg od miejsca wstrzymania, bez zaczynania od nowa.
        "_msgs": [{'role': 'user', 'parts': [
            f"[ODTWORZONE ZADANIE] Kontynuujesz tworzenie pliku {filename} po przerwie. "
            "Dotychczasowa treść jest już zapisana; dopisz TYLKO dalszy ciąg od miejsca "
            "urwania (koniec treści dostajesz w kolejnej wiadomości jako swoją poprzednią "
            "odpowiedź) i zamknij plik tagiem [/FILE], gdy będzie kompletny."]}],
        "_last_raw": f"[FILE: {filename}]\n{tail}",
    }
    print(f"FILE_JOB {job_id}: odtworzono z Boxa ({len(content)} znaków) – kontynuuję w tle")
    threading.Thread(target=_file_job_worker, args=(job_id,), daemon=True).start()
    return _file_job_snapshot(job_id)

def _edit_job_new(fname, user_text, content, api_key, base_msgs=None):
    """PAUZA_KLUCZY_V34: rekord zadania edycji kawałkowej w rejestrze zadań
    plikowych (kind='edit') – dzięki temu edycja ma tę samą kartę w rozmowie,
    migawki w Boxie i przyciski wznów/stop/usuń co zadania plikowe.
    EDYCJA_W_TLE_V35: base_msgs (kontekst rozmowy do wyboru bloków) żyje
    tylko w pamięci zadania – migawka w Boxie go nie zawiera, więc po
    restarcie serwera wznowienie wyboru bloków opiera się na samym
    poleceniu użytkownika."""
    job_id = uuid.uuid4().hex[:12]
    FILE_JOBS[job_id] = {
        "kind": "edit",
        "status": "running",
        "filename": fname,
        # dopóki bramka nie przepuści wyniku, do pobrania jest ORYGINAŁ
        "content_parts": [content],
        "chars": len(content),
        "parts": 0,
        "estimate": None,
        "truncated": False,
        "error": None,
        "stop_requested": False,
        "api_key": api_key,
        "created": time.time(),
        "edit_user_text": user_text,
        "edit_content_orig": content,
        "edit_chosen": [],
        "edit_bloki": {},
        "edit_total": 0,
        "edit_zmienione": [],
        "edit_globale": [],
        "edit_base_msgs": list(base_msgs) if base_msgs else [],
        "edit_worker_active": False,
    }
    return job_id


def _edit_checkpoint(job_id, chosen, stare, zrobione, zmienione, globale=None):
    """Zapisuje postęp edycji (wybrane bloki + podmienione treści) do rekordu
    zadania i migawki w Boxie – po pauzie/wznowieniu edycja leci dalej od
    pierwszego niezrobionego bloku."""
    job = FILE_JOBS.get(job_id)
    if job is None:
        return
    job["edit_chosen"] = [b["id"] for b in chosen]
    job["edit_total"] = len(chosen)
    job["edit_bloki"] = {b["id"]: b["text"] for b in chosen
                         if b["text"] != stare.get(b["id"])}
    job["edit_zmienione"] = list(zmienione)
    if globale is not None:
        job["edit_globale"] = list(globale)
    job["parts"] = zrobione
    threading.Thread(target=_file_job_save, args=(job_id,), daemon=True).start()


def _edit_job_worker(job_id: str):
    """EDYCJA_W_TLE_V35: punkt wejścia workera edycji – start z /chat,
    wznowienie z karty i restore po restarcie serwera lądują tutaj.
    Strażnik edit_worker_active pilnuje, by jedno zadanie nie było
    edytowane przez dwa wątki naraz (np. podwójne kliknięcie Wznów)."""
    job = FILE_JOBS.get(job_id)
    if job is None or job.get("kind") != "edit":
        return
    if job.get("edit_worker_active"):
        print(f"EDIT_JOB {job_id}: worker już działa – pomijam duplikat")
        return
    job["edit_worker_active"] = True
    try:
        _edit_job_run(job_id, job)
    finally:
        job["edit_worker_active"] = False


def _edit_job_run(job_id: str, job):
    """Wykonuje edycję kawałkową zadania – od zera albo od checkpointu
    po pauzie (wznow mówi, które bloki są już zrobione)."""
    job["status"] = "running"
    wznow = {"chosen": job.get("edit_chosen") or [],
             "bloki": job.get("edit_bloki") or {},
             "globale": job.get("edit_globale") or []}
    try:
        wynik = _edycja_w_kawalkach(job.get("api_key"), job.get("edit_user_text") or "",
                                    job["filename"],
                                    job.get("edit_content_orig") or "",
                                    job.get("edit_base_msgs") or [],
                                    rid=None, job_id=job_id, wznow=wznow)
    except PauzaBrakKluczy:
        job["status"] = "paused_keys"
        job["error"] = "Nadal brak dostępnych kluczy API – limity w puli wyczerpane. Zadanie znów wstrzymane z zapisanym postępem; wznów je później."
        _file_job_save(job_id)
        print(f"EDIT_JOB {job_id}: ponowna pauza – brak kluczy")
        return
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        _file_job_save(job_id)
        return
    if job.get("stop_requested"):
        job["status"] = "stopped"
        _file_job_save(job_id)
        return
    if wynik is None:
        job["status"] = "error"
        job["error"] = "Edycja nie powiodła się: model nie wskazał fragmentów do zmiany albo nie zwrócił ich treści. Ponów prośbę w czacie, ewentualnie dzieląc ją na mniejsze kroki."
        _file_job_save(job_id)
        return
    nowa, zmienione, odrzucona = wynik
    if odrzucona:
        job["status"] = "rejected"
        job["error"] = "Kontrola kodu odrzuciła edycję: " + odrzucona
        # content_parts zostaje z oryginałem – pobranie da ostatnią dobrą wersję
    else:
        job["status"] = "done"
        job["content_parts"] = [nowa]
        job["chars"] = len(nowa)
        job["parts"] = job.get("edit_total") or job.get("parts") or 0
        job["edit_zmienione"] = zmienione
    if job["status"] == "done":
        # MAPY_V39: odśwież mapę edytowanego pliku w Boxie.
        threading.Thread(target=_mapa_zapisz, args=(job["filename"], nowa), daemon=True).start()
    _file_job_save(job_id)
    print(f"EDIT_JOB {job_id}: zakończono ze statusem {job['status']}")


@app.post("/file-job/{job_id}/resume")
def file_job_resume_ep(job_id: str, user: str = Depends(get_current_user)):
    snap = _file_job_restore(job_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="Brak zapisanego postępu tego zadania w chmurze.")
    return snap


# --- KOMPILATOR INSTALATORÓW NSIS (wymaga pakietu systemowego 'nsis' na serwerze) ---
NSIS_INCLUDE_WHITELIST = {
    "MUI2.nsh", "MUI.nsh", "LogicLib.nsh", "FileFunc.nsh", "Sections.nsh",
    "WinMessages.nsh", "x64.nsh", "Memento.nsh", "nsDialogs.nsh",
    "InstallOptions.nsh", "LangFile.nsh", "Colors.nsh", "MultiUser.nsh",
}
NSIS_BANNED_DIRECTIVES = ("!system", "!execute", "!packhdr", "!appendfile", "!delfile")

def build_nsis_installer(exe_name: str, nsi_script: str, sources: dict) -> bytes:
    """
    Kompiluje instalator Windows (.exe) przez makensis.
    sources: {nazwa_pliku: bajty} - pliki, do których odwołuje się skrypt (z tagów [FILE:]).
    Zwraca bajty gotowego instalatora.
    """
    import shutil
    import subprocess
    import tempfile

    makensis = shutil.which("makensis")
    if not makensis:
        raise RuntimeError("makensis niedostępny na serwerze - zainstaluj pakiet systemowy 'nsis' "
                           "(packages.txt / Dockerfile), to NIE jest pakiet Pythona.")

    # Bezpieczeństwo: skrypt NSIS nie może wykonywać poleceń na serwerze w czasie kompilacji
    lowered = nsi_script.lower()
    for bad in NSIS_BANNED_DIRECTIVES:
        if bad in lowered:
            raise RuntimeError(f"Zabroniona dyrektywa w skrypcie NSIS: {bad}")
    for inc in re.findall(r'!include\s+"([^"]+)"', nsi_script, re.IGNORECASE):
        base = os.path.basename(inc)
        if "/" in inc or "\\" in inc or base not in NSIS_INCLUDE_WHITELIST:
            raise RuntimeError(f"Niedozwolony !include w skrypcie NSIS: {inc}")

    build_dir = tempfile.mkdtemp(prefix="nsis_build_")
    try:
        for fname, data in sources.items():
            safe = os.path.basename(fname)
            with open(os.path.join(build_dir, safe), "wb") as f:
                f.write(data)
        script_path = os.path.join(build_dir, "installer.nsi")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(nsi_script)

        result = subprocess.run(
            [makensis, "installer.nsi"],
            cwd=build_dir, capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            tail = ((result.stdout or "") + "\n" + (result.stderr or ""))[-800:]
            raise RuntimeError(f"makensis zakończył się błędem: {tail}")

        exe_path = os.path.join(build_dir, exe_name)
        if not os.path.exists(exe_path):
            exes = [f for f in os.listdir(build_dir) if f.lower().endswith(".exe")]
            if not exes:
                raise RuntimeError("makensis nie wyprodukował pliku .exe")
            exe_path = os.path.join(build_dir, exes[0])
        with open(exe_path, "rb") as f:
            data = f.read()
        print(f"NSIS installer OK: {exe_name} ({len(data)} bytes)")
        return data
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)



# --- AKUMULATOR_V3: budowanie na raty (oszczędzanie tokenów) ---
_AKUM_SKIP_SUFFIXES = ("_blad.txt", "_status.txt", "_info.txt", ".docx", ".pdf", ".exe", ".job")

def _akum_history_files(history):
    """Pliki projektu z poprzednich odpowiedzi asystenta. Frontend trzyma
    wygenerowane pliki jako załączniki wiadomości w historii – zbieramy je
    wszystkie, w kolejności rozmowy (przy łączeniu nowsza wersja pliku
    nadpisuje starszą)."""
    out = []
    if not history:
        return out
    for item in history:
        if getattr(item, "isHidden", False):
            continue
        if getattr(item, "role", None) not in ("model", "assistant"):
            continue
        for a in (getattr(item, "attachments", None) or []):
            name = (getattr(a, "name", "") or "").strip()
            data = getattr(a, "data", "") or ""
            if not name or not data:
                continue
            if name.lower().endswith(_AKUM_SKIP_SUFFIXES) or name.startswith("build_"):
                continue
            out.append({"filename": name,
                        "mime_type": getattr(a, "mime_type", None) or guess_mime(name),
                        "data": data})
    return out

def _akum_user_call_html(history):
    """Duży plik użytkownika do assets: cały call.html z załącznika albo
    złożony z części call-czesc-N-z-M.txt. Zwraca tekst albo None.
    Dzięki temu model nigdy nie musi przepisywać dużego pliku."""
    if not history:
        return None
    full = None
    parts = {}
    total = None
    for item in history:
        if getattr(item, "isHidden", False):
            continue
        if getattr(item, "role", None) != "user":
            continue
        for a in (getattr(item, "attachments", None) or []):
            name = (getattr(a, "name", "") or "").strip()
            data = getattr(a, "data", "") or ""
            if not name or not data:
                continue
            try:
                raw = base64.b64decode(data)
            except Exception:
                continue
            if name == "call.html":
                full = raw
            else:
                m = re.match(r"call-czesc-(\d+)-z-(\d+)\.txt$", name)
                if m:
                    parts[int(m.group(1))] = raw
                    total = int(m.group(2))
    if full is not None:
        try:
            return full.decode("utf-8")
        except Exception:
            return None
    if total and len(parts) == total and all(i in parts for i in range(1, total + 1)):
        try:
            return b"".join(parts[i] for i in range(1, total + 1)).decode("utf-8")
        except Exception:
            return None
    return None

def _akum_is_asset_attachment(name) -> bool:
    """AKUMULATOR_V4 (OSZCZEDNY): czy załącznik to duży plik do buildu
    (call.html albo jego części)? Takie pliki nie idą do modelu."""
    n = (name or "").strip()
    return n == "call.html" or re.match(r"call-czesc-\d+-z-\d+\.txt$", n) is not None

def _trim_file_blocks(text: str) -> str:
    """W historii wysyłanej do modelu długie bloki [FILE:] z poprzednich
    odpowiedzi zastępujemy krótką notką – treść plików jest zapisana
    w załącznikach rozmowy, a bloki zjadały większość tokenów wejścia."""
    def _sub(m):
        if len(m.group(2)) <= 400:
            return m.group(0)
        return (f"[FILE: {m.group(1).strip()}]\n"
                "[treść pliku pominięta w historii dla oszczędności tokenów – "
                "plik jest zapisany w rozmowie jako załącznik]\n[/FILE]")
    text = re.sub(r"\[FILE:\s*([^\]]+?)\]\s*(.*?)\s*\[/FILE\]", _sub, text,
                  flags=re.DOTALL | re.IGNORECASE)
    # niedomknięty [FILE:] na końcu starej odpowiedzi (była ucięta) – też skracamy,
    # inaczej jeden taki blok zje większość tokenów wejścia kolejnej odpowiedzi
    def _sub_unclosed(m):
        if len(m.group(2)) <= 400:
            return m.group(0)
        return (f"[FILE: {m.group(1).strip()}]\n"
                "[treść pliku pominięta w historii dla oszczędności tokenów – "
                "plik był ucięty w oryginalnej odpowiedzi]\n[/FILE]")
    return re.sub(r"\[FILE:\s*([^\]]+?)\]\s*(.*)$", _sub_unclosed, text,
                  flags=re.DOTALL | re.IGNORECASE)

def _extract_and_build_files(ai_text: str, history=None, rid=None) -> tuple[str, list]:
    """
    Wyciąga z odpowiedzi AI tagi [FILE:], [GENERATE_DOCX:], [GENERATE_PDF:], [NSIS_INSTALLER:]
    i buduje listę plików do zwrócenia.
    Zwraca (czysty_tekst_bez_tagów, lista_plików).
    """
    files = []
    clean = ai_text
    truncated_files = []  # nazwy plików z niedomkniętych tagów (odpowiedź ucięta przez limit tokenów)

    # ETAP_PRACY_V48: tworzenie dokumentu/komiksu w czacie to praca nad
    # plikiem - zglos etap, zeby frontend pokazal duzy panel postepu
    # zamiast samych kropek (zwykla odpowiedz etapu nie zmienia).
    if rid:
        if re.search(r'\[GENERATE_PDF:[^\]]*szablon\s*=\s*komiks', ai_text, re.IGNORECASE):
            _chat_job_set_stage(rid, "Tworzę komiks: generuję ilustracje i składam strony…")
        elif "[GENERATE_PDF:" in ai_text.upper():
            _chat_job_set_stage(rid, "Tworzę dokument PDF…")
        elif "[GENERATE_DOCX:" in ai_text.upper():
            _chat_job_set_stage(rid, "Tworzę dokument DOCX…")

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

    # 1b. NIEDOMKNIĘTY [FILE: name.ext] bez [/FILE] – odpowiedź modelu urwała się
    #     w połowie pliku (limit tokenów wyjścia, np. długa gra HTML).
    #     Bez tego cała treść pliku lądowała w czacie jako "kod zamiast pliku".
    #     Zasada: wszystko od tagu do końca odpowiedzi to treść pliku – wyciągamy
    #     ją do pobrania tak samo jak domknięty plik i dopisujemy ostrzeżenie.
    unclosed_file_pattern = re.compile(
        r'\[FILE:\s*([^\]]+?)\]\s*(.*)$',
        re.DOTALL | re.IGNORECASE
    )
    m_unclosed = unclosed_file_pattern.search(clean)
    if m_unclosed:
        fname = m_unclosed.group(1).strip()
        content = m_unclosed.group(2)
        content = re.sub(r'^```[\w]*\n?', '', content.strip())
        content = re.sub(r'\n?```$', '', content)
        if content.strip():
            files.append(make_file_response(fname, content))
            truncated_files.append(fname)
            print(f"FILE_TRUNCATED: wyciągnięto niedomknięty plik {fname} ({len(content)} znaków)")
        clean = unclosed_file_pattern.sub('', clean)

    # 2. [GENERATE_DOCX: Tytuł] ... [/GENERATE_DOCX]
    docx_pattern = re.compile(
        r'\[GENERATE_DOCX:\s*([^\]]*)\]\s*(.*?)\s*\[/GENERATE_DOCX\]',
        re.DOTALL | re.IGNORECASE
    )
    for m in docx_pattern.finditer(ai_text):
        title = (m.group(1).strip() or "Dokument").split("|")[0].strip() or "Dokument"
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
            # DOKUMENTY_V10: wybor silnika (lokalny WeasyPrint / GitHub) w srodku
            pdf_files, engine_info = make_pdf_files(title, body)
            files.extend(pdf_files)
            print(f"PDF [{engine_info}]: {[f['filename'] for f in pdf_files]}")
        except Exception as e:
            print(f"PDF gen error: {e}")
            # awaryjnie oddaj treść jako .txt, żeby użytkownik coś dostał
            safe_name = re.sub(r'[^\w\s\-ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]', '', title)[:40].strip() or "dokument"
            safe_name = safe_name.replace(' ', '_') + ".txt"
            files.append(make_file_response(safe_name, f"{title}\n\n{body}"))
    clean = pdf_pattern.sub('', clean)

    # 3b. NIEDOMKNIĘTE [GENERATE_DOCX:] / [GENERATE_PDF:] bez tagów zamykających
    #     (odpowiedź ucięta) – generujemy dokument z tego, co doszło do końca tekstu.
    unclosed_docx = re.search(r'\[GENERATE_DOCX:\s*([^\]]*)\]\s*(.*)$', clean, re.DOTALL | re.IGNORECASE)
    if unclosed_docx:
        title = (unclosed_docx.group(1).strip() or "Dokument").split("|")[0].strip() or "Dokument"
        body = unclosed_docx.group(2).strip()
        if body:
            try:
                docx_bytes = create_docx_from_text(title, body)
                safe_name = re.sub(r'[^\w\s\-ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]', '', title)[:40].strip() or "dokument"
                files.append(make_file_response(safe_name.replace(' ', '_') + ".docx", docx_bytes))
                truncated_files.append(safe_name.replace(' ', '_') + ".docx")
            except Exception as e:
                print(f"DOCX gen error (unclosed): {e}")
        clean = re.sub(r'\[GENERATE_DOCX:\s*[^\]]*\]\s*.*$', '', clean, flags=re.DOTALL | re.IGNORECASE)

    unclosed_pdf = re.search(r'\[GENERATE_PDF:\s*([^\]]*)\]\s*(.*)$', clean, re.DOTALL | re.IGNORECASE)
    if unclosed_pdf:
        title = unclosed_pdf.group(1).strip() or "Dokument"
        body = unclosed_pdf.group(2).strip()
        if body:
            try:
                pdf_files, engine_info = make_pdf_files(title, body)
                files.extend(pdf_files)
                truncated_files.extend(f["filename"] for f in pdf_files)
                print(f"PDF (unclosed) [{engine_info}]: {[f['filename'] for f in pdf_files]}")
            except Exception as e:
                print(f"PDF gen error (unclosed): {e}")
                safe_name = re.sub(r'[^\w\s\-ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]', '', title)[:40].strip() or "dokument"
                files.append(make_file_response(safe_name.replace(' ', '_') + ".txt", f"{title}\n\n{body}"))
        clean = re.sub(r'\[GENERATE_PDF:\s*[^\]]*\]\s*.*$', '', clean, flags=re.DOTALL | re.IGNORECASE)

    # 4. [NSIS_INSTALLER: Nazwa-Setup.exe] ...skrypt .nsi... [/NSIS_INSTALLER]
    #    Kompiluje prawdziwy instalator Windows z plików [FILE:] z tej samej odpowiedzi.
    nsis_pattern = re.compile(
        r'\[NSIS_INSTALLER:\s*([^\]]+?)\]\s*(.*?)\s*\[/NSIS_INSTALLER\]',
        re.DOTALL | re.IGNORECASE
    )
    nsis_matches = list(nsis_pattern.finditer(ai_text))
    if nsis_matches:
        sources = {}
        for f in files:
            try:
                sources[f["filename"]] = base64.b64decode(f["data"])
            except Exception:
                continue
        for m in nsis_matches:
            exe_name = os.path.basename(m.group(1).strip()) or "Setup.exe"
            if not exe_name.lower().endswith(".exe"):
                exe_name += ".exe"
            script = m.group(2)
            script = re.sub(r'^```[\w]*\n?', '', script.strip())
            script = re.sub(r'\n?```$', '', script)
            try:
                exe_bytes = build_nsis_installer(exe_name, script, sources)
                files.append(make_file_response(exe_name, exe_bytes, "application/octet-stream"))
            except Exception as e:
                print(f"NSIS build error: {e}")
                files.append(make_file_response(
                    exe_name.replace(".exe", "_blad.txt"),
                    f"Nie udało się skompilować instalatora {exe_name}:\n{e}"
                ))
    clean = nsis_pattern.sub('', clean)

    # 5. [GH_BUILD: target] ... [/GH_BUILD] – build w chmurze (GitHub Actions)
    #    Backend NIE buduje synchronicznie: zwraca kartę buildu (pseudo-plik
    #    application/x-build-job z projektem w środku), z której frontend
    #    startuje build w tle przez POST /cloud-build/start i pokazuje
    #    status na żywo. Pliki projektu znikają z listy plików do pobrania
    #    (są osiągalne z karty przyciskiem "Pobierz sam projekt").
    gh_pattern = re.compile(
        r'\[GH_BUILD:\s*([^\]]+?)\]\s*(.*?)\s*\[/GH_BUILD\]',
        re.DOTALL | re.IGNORECASE
    )
    gh_matches = list(gh_pattern.finditer(ai_text))
    if gh_matches:
        # AKUMULATOR_V3: źródła buildu = pliki z całej rozmowy (załączniki
        # poprzednich odpowiedzi asystenta) + pliki z bieżącej odpowiedzi.
        # Na końcu dokładamy duży plik użytkownika z jego załączników –
        # nadpisuje ewentualną wersję od modelu, bo jest bajt w bajt.
        merged = {}
        for hf in _akum_history_files(history):
            merged[hf["filename"]] = hf
        for f in files:
            if not f["filename"].endswith(("_blad.txt", "_status.txt", "_info.txt",
                                           ".docx", ".pdf", ".exe")):
                merged[f["filename"]] = f
        if any(m.group(1).strip().lower() == "android" for m in gh_matches):
            asset_html = _akum_user_call_html(history)
            if asset_html is not None:
                merged["app/src/main/assets/call.html"] = make_file_response(
                    "app/src/main/assets/call.html", asset_html)
                print(f"AKUMULATOR_V3: assets/call.html z załączników użytkownika "
                      f"({len(asset_html)} znaków)")
        gh_src_files = list(merged.values())
        if gh_src_files:
            src_names = {f["filename"] for f in gh_src_files}
            files = [f for f in files if f["filename"] not in src_names]
            for m in gh_matches:
                target = m.group(1).strip().lower()
                payload = {"target": target, "sources": gh_src_files}
                files.append(make_file_response(
                    f"build_{target}.job",
                    json.dumps(payload, ensure_ascii=False),
                    "application/x-build-job",
                ))
        else:
            files.append(make_file_response(
                "build_blad.txt",
                "Nie udało się przygotować buildu w chmurze: brak plików projektu "
                "w odpowiedzi (dołącz je tagami [FILE:])."
            ))
    clean = gh_pattern.sub('', clean)

    # 6. [GH_BUILD_STATUS: numer] – dokończenie wcześniejszego builda z chmury
    ghs_pattern = re.compile(r'\[GH_BUILD_STATUS:\s*(\d+)\s*\]', re.IGNORECASE)
    for m in ghs_pattern.finditer(ai_text):
        run_id = int(m.group(1))
        try:
            outputs, info = github_build_status(run_id)
            for oname, odata in outputs.items():
                files.append(make_file_response(oname, odata, "application/octet-stream"))
            files.append(make_file_response(f"build_{run_id}_status.txt", info))
        except Exception as e:
            print(f"GH build status error: {e}")
            files.append(make_file_response(
                f"build_{run_id}_blad.txt",
                f"Nie udało się pobrać builda {run_id}:\n{e}"
            ))
    clean = ghs_pattern.sub('', clean)

    # 7. BLOKI KODU ``` W TEKŚCIE -> PLIKI DO POBRANIA (twarda zasada użytkownika:
    #    "nigdy kodu w czacie, tylko gotowe pliki"). Model mimo instrukcji czasem
    #    wkleja kod blokiem ``` zamiast tagiem [FILE:] – zamieniamy więc każdy
    #    większy blok (>= 200 znaków) na plik do pobrania i zdejmujemy go z tekstu.
    #    Krótkie fragmenty (komendy, przykłady 1-2 linijki) zostają w tekście.
    _LANG_EXT = {
        "python": "py", "py": "py", "javascript": "js", "js": "js", "typescript": "ts",
        "ts": "ts", "html": "html", "htm": "html", "css": "css", "json": "json",
        "markdown": "md", "md": "md", "sql": "sql", "bash": "sh", "shell": "sh",
        "sh": "sh", "java": "java", "c": "c", "cpp": "cpp", "c++": "cpp", "rust": "rs",
        "go": "go", "php": "php", "ruby": "rb", "yaml": "yaml", "yml": "yml",
        "xml": "xml", "plaintext": "txt", "text": "txt", "txt": "txt",
    }
    fence_pattern = re.compile(r'```(\w*)\n?(.*?)```', re.DOTALL)
    used_names = {f["filename"] for f in files}
    converted_names = []
    _fence_counter = [0]

    def _fence_to_file(m):
        lang = (m.group(1) or "").lower()
        code = (m.group(2) or "").strip("\n")
        if len(code.strip()) < 200:
            return m.group(0)  # krótki fragment – zostaje w tekście
        ext = _LANG_EXT.get(lang, lang if lang.isalnum() and len(lang) <= 5 else "txt")
        _fence_counter[0] += 1
        if ext == "html":
            base = "index.html" if _fence_counter[0] == 1 and "index.html" not in used_names else f"strona_{_fence_counter[0]}.html"
        else:
            base = f"kod_{_fence_counter[0]}.{ext}"
        name = base
        n = 2
        while name in used_names:
            stem, dot, e = base.rpartition(".")
            name = f"{stem}_{n}.{e}" if dot else f"{base}_{n}"
            n += 1
        used_names.add(name)
        files.append(make_file_response(name, code))
        converted_names.append(name)
        print(f"CODE_FENCE_TO_FILE: blok ```{lang} ({len(code)} znaków) -> plik {name}")
        return ""

    clean = fence_pattern.sub(_fence_to_file, clean)
    if converted_names:
        clean = clean.rstrip() + "\n\n📁 Kod zapisano jako plik do pobrania: " + ", ".join(converted_names) + "."

    # Ostrzeżenie o plikach z uciętej odpowiedzi (niedomknięte tagi)
    if truncated_files:
        clean = clean.rstrip() + (
            "\n\n⚠️ Uwaga: moja odpowiedź urwała się w trakcie tworzenia pliku: "
            + ", ".join(truncated_files)
            + " – plik może być niekompletny. Napisz „dokończ”, a dopiszę brakującą resztę."
        )

    # posprzątaj wielokrotne puste linie
    clean = re.sub(r'\n{3,}', '\n\n', clean).strip()
    return clean, files


# --- EDYCJA_PLIKU_V9: poprawka ma dotyczyć TEGO SAMEGO pliku ---
# Problem: plik utworzony zadaniem w tle w ogóle nie istnieje w historii
# rozmowy (jest tylko notka „Tworzę plik… w tle"), więc gdy użytkownik prosił
# np. „dodaj dwie gałki", model nie miał treści gry i wymyślał nową od zera
# (zmierzone podobieństwo: 6% linii). Dlatego przy prośbie o edycję backend
# dokleja modelowi AKTUALNĄ treść najnowszego pliku z rozmowy.
_EDIT_KEYWORDS = (
    "dodaj", "dołóż", "doloz", "zmień", "zmien", "zmiana", "zmiany", "przerób", "przerob",
    "popraw", "usuń", "usun", "edytuj", "edycja", "modyfik", "rozbuduj", "ulepsz", "napraw",
    "dopisz", "zamień", "zamien", "przenieś", "przenies", "wstaw", "powiększ", "powieksz",
    "pomniejsz", "zmniejsz", "przyspiesz", "zwolnij", "sterowanie", "sterowania",
    "gałk", "galk", "dotykow",
)

def _czy_prosba_o_edycje(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in _EDIT_KEYWORDS)

def _sygnal_pracy_plikowej(history) -> bool:
    """Czy bieżące żądanie najpewniej oznacza pracę nad plikiem (edycja
    albo tworzenie) – PANEL_WCZESNIEJ_V56. Sygnały lokalne, bez wywołań
    modelu: słowa edycji w ostatniej wiadomości użytkownika albo
    niegraficzny załącznik w jednej z dwóch ostatnich jego wiadomości
    (załączony plik i polecenie przychodzą jako osobne wiadomości)."""
    try:
        user_items = [x for x in history
                      if getattr(x, "role", "") == "user" and not getattr(x, "isHidden", False)]
        if not user_items:
            return False
        if _czy_prosba_o_edycje(user_items[-1].content or ""):
            return True
        for item in user_items[-2:]:
            for a in (getattr(item, "attachments", None) or []):
                mime = (getattr(a, "mime_type", "") or "").lower()
                if mime and not mime.startswith("image/"):
                    return True
        return False
    except Exception:
        return False

# --- KONTEKST_V57: decyzja o edycji z kontekstu CAŁEJ rozmowy ---
# Same słowa-klucze (EDYCJA_PLIKU_V9) nie wystarczą: użytkownik komentujący
# właśnie wytworzony plik („jest już znacznie lepiej, tylko obracanie
# czerwonym guzikiem powinno także obracać światło…") zwykle nie używa
# żadnego z nich, a mimo to bez wątpienia kontynuuje pracę nad tym plikiem.
# Bez tego rozpoznania prośba szła zwykłym czatem, model przepisywał plik
# od zera i powstawała niekompletna nowa wersja (diagnoza 2026-11-02).
# Decyzja patrzy więc też na kontekst: czy w pobliżu jest plik albo karta
# zadania plikowego oraz czy wypowiedź brzmi jak kontynuacja / ocena
# wyniku, a nie jak pytanie albo nowy temat. Wszystko lokalnie, bez
# wywołań modelu; wołający i tak wymaga potem znalezienia pliku.
_MARKERY_KONTYNUACJI = (
    "jest już", "jest juz", "znacznie lepiej", "o wiele lepiej", "dużo lepiej",
    "duzo lepiej", "nie działa", "nie dziala", "działa, ale", "dziala, ale",
    "działa ale", "dziala ale", "powinno", "powinien", "powinna", "powinni",
    "ma działać", "ma dzialac", "ma być", "ma byc", "prawie dobrze",
    "w końcu działa", "w koncu dziala", "nadal nie", "wciąż nie", "wciaz nie",
    "ciągle nie", "ciagle nie", "jest dobrze, tylko",
)
_POGAWEDKA_V57 = (
    "dziękuję", "dziekuje", "dzięki", "ok", "okej", "dobra", "super",
    "świetnie", "swietnie", "fajnie", "rozumiem", "zgoda", "tak", "nie",
    "miło", "milo", "cześć", "czesc", "hej", "pa", "żegnaj", "zegnaj",
    "dobranoc", "spoko", "jasne",
)

def _ostatnia_karta_pliku(history):
    """Czy ostatnia odpowiedź asystenta przed bieżącą wiadomością
    użytkownika to karta zadania plikowego (application/x-file-job)?"""
    try:
        items = [x for x in history if not getattr(x, "isHidden", False)]
        idx = None
        for i in range(len(items) - 1, -1, -1):
            if getattr(items[i], "role", "") == "user":
                idx = i
                break
        if idx is None:
            return False
        for it in reversed(items[:idx]):
            if getattr(it, "role", "") in ("assistant", "model"):
                return any((getattr(a, "mime_type", "") or "") == "application/x-file-job"
                           for a in (getattr(it, "attachments", None) or []))
        return False
    except Exception:
        return False

def _plik_w_poblizu(history):
    """Czy w ostatnich wpisach rozmowy jest plik nadający się do edycji
    albo karta zadania plikowego (żywy kontekst pracy nad plikiem)?"""
    try:
        items = [x for x in history if not getattr(x, "isHidden", False)][-5:]
        for it in items:
            for a in (getattr(it, "attachments", None) or []):
                mime = (getattr(a, "mime_type", "") or "").lower()
                if mime == "application/x-file-job":
                    return True
                ext = os.path.splitext(getattr(a, "name", "") or "")[1].lower()
                if ext in (".html", ".htm", ".py", ".js", ".css", ".txt", ".md", ".json"):
                    return True
        return False
    except Exception:
        return False

def _czy_edycja_kontekst(history, last_user_txt) -> bool:
    """KONTEKST_V57: czy bieżąca wiadomość to polecenie zmiany w pliku
    z tej rozmowy – wnioskowane z kontekstu całej wypowiedzi."""
    if _czy_prosba_o_edycje(last_user_txt):
        return True
    t = (last_user_txt or "").strip().lower()
    if not t or not _plik_w_poblizu(history):
        return False
    marker = any(m in t for m in _MARKERY_KONTYNUACJI)
    if t.endswith("?") and not marker:
        return False  # samo pytanie o plik to nie polecenie zmiany
    if marker:
        return True
    # Sama kontynuacja tuż po karcie zadania: dłuższa wypowiedź robocza,
    # nie pogawędka („dziękuję", „ok") i nie pytanie (odrzucone wyżej).
    if _ostatnia_karta_pliku(history):
        if t.rstrip(".! ") in _POGAWEDKA_V57:
            return False
        return len(t) >= 25
    return False

def _znajdz_aktualny_plik(history):
    """Najnowszy plik z rozmowy nadający się do edycji: (nazwa, treść) albo None.
    Źródła: zadanie w tle (pamięć serwera albo zapis w Boxie) lub tekstowy
    załącznik w historii. Gdy najnowszy plik jeszcze się tworzy – None."""
    for item in reversed([x for x in history if not x.isHidden]):
        for att in reversed(getattr(item, "attachments", None) or []):
            mime = (getattr(att, "mime_type", "") or "").lower()
            name = getattr(att, "name", "") or ""
            if mime == "application/x-file-job":
                try:
                    payload = json.loads(base64.b64decode(att.data).decode("utf-8"))
                    job_id = payload.get("job_id")
                    fname = payload.get("filename") or "plik"
                except Exception:
                    return None
                job = FILE_JOBS.get(job_id)
                if job is not None:
                    if job["status"] in ("done", "stopped"):
                        return fname, "".join(job["content_parts"])
                    return None  # plik jeszcze się tworzy – nie edytujemy w locie
                data = _file_job_load_snapshot(job_id)
                if data and data.get("content"):
                    return fname, data["content"]
                return None
            ext = os.path.splitext(name.lower())[1]
            if ext in (".html", ".htm", ".py", ".js", ".css", ".txt", ".md", ".json") or mime.startswith("text/"):
                try:
                    return name, base64.b64decode(att.data).decode("utf-8", errors="ignore")
                except Exception:
                    continue
    return None


# --- DAWKOWANIE_V38: krótka instrukcja do wywołań mechanicznych ---
# Pełna SYSTEM_INSTRUCTION ma ~20 tys. znaków (~6,8 tys. tokenów) – to prawie
# cały darmowy limit Groq (8000 tokenów/min), więc każde małe wywołanie
# cząstkowe (wybór bloków, edycja bloku, naprawa bramki) płaciło nią jak
# dużym zadaniem i Groq/Mistral odpadały. Wywołania mechaniczne mają własne,
# kompletne instrukcje w treści wiadomości, więc wystarcza im ta krótka rama:
# prompty cząstkowe spadają do ~2–4 tys. tokenów i mieszczą się w limitach
# szybkich dostawców – pula rozkłada się na wszystkie modele.
SYS_MECH = (
    "Jesteś precyzyjnym wykonawcą zadań technicznych dla backendu aplikacji. "
    "Odpowiadaj po polsku. Wykonuj DOKŁADNIE format wymagany w bieżącej wiadomości "
    "(tagi, JSON, fragmenty kodu) i nic poza nim: bez wstępów, wyjaśnień ani "
    "komentarzy, chyba że wiadomość mówi inaczej. Nie zmieniaj niczego, o co "
    "nie prosi bieżąca wiadomość."
)


# --- V39: PLANISTA zadań, BRAMKI jakości per typ wyniku, MAPY projektu ---
# Trzy mechanizmy ogólne (nie tylko komiksy – każde zadanie użytkownika):
# 1) PLANISTA: dłuższy plik/program powstaje wg krótkiego planu, który
#    backend zamawia jednym małym wywołaniem i dokleja do każdej
#    kontynuacji – całość trzyma się struktury zamiast być zlepkiem części.
# 2) BRAMKI: gotowy kod z generowania przechodzi tę samą kontrolę co
#    edycje (z jedną rundą auto-naprawy), a dokumenty PDF/DOCX są
#    sprawdzane strukturalnie, zanim trafią do użytkownika.
# 3) MAPY: po każdym ukończonym zadaniu backend zapisuje w Boxie zwartą
#    mapę pliku (struktura wyciągnięta LOKALNIE, zero wywołań modelu);
#    gdy kolejne polecenie wymienia taki plik, jego mapa wchodzi do
#    kontekstu planu/edycji zamiast wczytywania całości od nowa.

def _pdf_ok(b) -> bool:
    """Czy bajty wyglądają na kompletny, prawdziwy PDF."""
    return bool(b) and len(b) > 300 and b[:4] == b"%PDF" and b"%%EOF" in b[-1024:]


def _docx_ok(b) -> bool:
    """Czy bajty to poprawny pakiet DOCX (zip z wymaganymi częściami)."""
    try:
        import io as _io
        import zipfile as _zf
        if not b or not _zf.is_zipfile(_io.BytesIO(b)):
            return False
        with _zf.ZipFile(_io.BytesIO(b)) as z:
            nazwy = z.namelist()
        return "[Content_Types].xml" in nazwy and "word/document.xml" in nazwy
    except Exception:
        return False


def _bramka_dopisz_definicje(api_key, fname, content, nazwy):
    """KONTEKST_V57: bramka generowania dogrywa brakujące definicje.
    Gdy wygenerowany kod woła funkcje/zmienne, których w pliku nie ma
    (pewne problemy typu „zmienna"), jedno małe wywołanie mechaniczne ma
    dopisać TYLKO te definicje; wstawiamy je na koniec głównego skryptu.
    Zwraca nową treść albo None (plik zostaje wtedy bez zmian)."""
    try:
        if not nazwy:
            return None
        uzycia = []
        for n in nazwy[:12]:
            linie = []
            for ln in (content or "").splitlines():
                if ln.strip() and re.search(r"\b" + re.escape(n) + r"\b", ln):
                    linie.append(ln.strip()[:160])
                if len(linie) >= 2:
                    break
            uzycia.append("- " + n + ((" – użycia: " + " / ".join(linie)) if linie else ""))
        msgs = [{'role': 'user', 'parts': [
            "[SYSTEM – BRAKUJĄCE DEFINICJE] W pliku " + fname + " kod używa "
            "poniższych funkcji/zmiennych JavaScript, ale nigdzie ich nie ma – "
            "program pada z błędem ReferenceError:\n" + "\n".join(uzycia) + "\n"
            "Napisz TYLKO brakujące definicje (deklaracje funkcji i/lub "
            "inicjalizacje zmiennych) w czystym JavaScripcie, zgodne z tymi "
            "użyciami. Bez HTML, bez wyjaśnień, bez znaczników – sam kod, "
            "który da się dopisać na koniec głównego skryptu pliku."]}]
        odp = _call_llm_rotacja(api_key, msgs, SYS_MECH, max_out=4000)
        if not odp:
            if _rotacja_brak_kluczy():
                raise PauzaBrakKluczy()
            return None
        kod = (odp or "").strip()
        kod = re.sub(r"^```[a-zA-Z]*\s*", "", kod)
        kod = re.sub(r"\s*```$", "", kod).strip()
        if not kod or len(kod) > 20_000 or "[FILE:" in kod or "</html" in kod.lower():
            return None
        dopisek = ("\n// --- definicje dopisane przez bramkę kodu (KONTEKST_V57) ---\n"
                   + kod + "\n")
        low = (fname or "").lower()
        if low.endswith((".html", ".htm")):
            idx = content.rfind("</script>")
            if idx < 0:
                return None
            return content[:idx] + dopisek + content[idx:]
        return content.rstrip() + "\n" + dopisek
    except PauzaBrakKluczy:
        raise
    except Exception as e:
        print(f"BRAMKA_PLIKU_V57: dopisanie definicji pominięte ({e})")
        return None


def _wstaw_globale(fname, content, deklaracje):
    """Dopisuje deklaracje globalne na początek głównego skryptu pliku
    (po pierwszym tagu <script>) albo na początek pliku JS – najwcześniej,
    jak się da, żeby wykonały się przed każdym wywołaniem funkcji."""
    if not deklaracje:
        return content
    if (fname or "").lower().endswith((".js", ".mjs")):
        return "\n".join(deklaracje) + "\n" + content
    m = re.search(r"<script[^>]*>", content, re.IGNORECASE)
    if m:
        return content[:m.end()] + "\n" + "\n".join(deklaracje) + "\n" + content[m.end():]
    return "\n".join(deklaracje) + "\n" + content


def _globale_z_odpowiedzi(odp):
    """Deklaracje z tagów [NOWY_GLOBAL: let x = ...;] w odpowiedzi modelu
    (kanał stanu współdzielonego przy edycji blokowej, SPOJNOSC_V59).
    Przyjmujemy tylko pojedyncze, proste deklaracje ze stałą wartością
    początkową albo literałem – bez funkcji i bez kodu HTML."""
    out = []
    for m in re.finditer(r"\[NOWY_GLOBAL:\s*([^\]]+)\]", odp or ""):
        d = m.group(1).strip()
        if re.fullmatch(r"(let|var|const)\s+[A-Za-z_$][\w$]*\s*=\s*[^;\n<>]{1,120};", d) \
                and "function" not in d:
            out.append(d)
    return out


def _nazwa_zadeklarowana(fname, content, nazwa):
    """Czy nazwa ma już jakąkolwiek deklarację w skryptach pliku
    (zachowawczo: także lokalną – kanał [NOWY_GLOBAL] wtedy pomijamy,
    a spójność ocenia bramka)."""
    try:
        tekst = "\n".join(k for k, _l in _js_skrypty(fname, content))
        return re.search(r"(?:let|var|const|function|class)\s+"
                         + re.escape(nazwa) + r"\b", tekst) is not None
    except Exception:
        return True


def _kontrakt_pliku(fname, content):
    """SPOJNOSC_V59: kontrakt spójności dla edycji blokowej. Model
    przerabiający JEDEN blok nie widzi reszty pliku, więc wymyśla
    równoległe mechanizmy i lokalne kopie stanu – kontrakt daje mu
    inwentarz istniejących nazw (liczonym lokalnie, bez tokenów) i jedną
    zasadę: nowy stan współdzielony zgłasza tagiem [NOWY_GLOBAL: ...],
    a nie deklaruje go lokalnie w bloku."""
    try:
        tekst = "\n".join(k for k, _l in _js_skrypty(fname, content))

        def _zbierz(wzor):
            out = []
            for n in re.findall(wzor, tekst):
                if n not in out:
                    out.append(n)
            return out[:40]

        funkcje = _zbierz(r"function\s+([A-Za-z_$][\w$]*)\s*\(")
        klasy = _zbierz(r"\bclass\s+([A-Za-z_$][\w$]*)")
        zmienne = _zbierz(r"(?:let|var|const)\s+([A-Za-z_$][\w$]*)\s*=")
        czesci = []
        if funkcje:
            czesci.append("funkcje: " + ", ".join(funkcje))
        if zmienne:
            czesci.append("zmienne: " + ", ".join(zmienne))
        if klasy:
            czesci.append("klasy: " + ", ".join(klasy))
        inw = ("Istniejące nazwy w pliku: " + "; ".join(czesci) + ". ") if czesci else ""
        return ("KONTRAKT SPÓJNOŚCI: " + inw
                + "Używaj tych nazw zamiast wymyślać równoległe mechanizmy. "
                "ZASADA STANU: jeśli Twoja zmiana wymaga nowej zmiennej czytanej "
                "albo zapisywanej przez więcej niż jedną funkcję, NIE deklaruj "
                "jej lokalnie w tym bloku – w bloku używaj samej nazwy, a po "
                "[/ZAMIEN_BLOK] dopisz jedną linię: [NOWY_GLOBAL: let nazwa = "
                "wartość;]. Backend dopisze tę deklarację do globalnego "
                "zakresu pliku.\n")
    except Exception:
        return ""


def _usun_cienie(content, nazwy):
    """SPOJNOSC_V59: deterministyczne rozwiązanie 'cieni' stanu
    współdzielonego, bez wywołań modelu. Dla każdej nazwy: deklaracja
    lokalna z prostą wartością początkową (liczba, true/false, null,
    napis, {}, []) znika – wspólną deklarację dopisuje wołający z tej
    wartości; deklaracja z wartością liczoną zamienia się w zwykłe
    przypisanie (od teraz do zmiennej globalnej). Zwraca (treść,
    {nazwa: wartość początkowa}) – wartość tylko dla nazw, przy których
    usunięto deklarację z prostą wartością. Działa na całoliniowych,
    pojedynczych deklaracjach; deklaracji z wieloma zmiennymi naraz
    świadomie nie rusza."""
    wynik = content
    inic = {}
    for nazwa in nazwy or []:
        wzor = re.compile(r"^([ \t]*)(?:let|var|const)\s+" + re.escape(nazwa)
                          + r"\s*=\s*(.+?);\s*$")
        proste = []

        def _podm(ln):
            m = wzor.match(ln)
            if not m:
                return ln
            rhs = m.group(2).strip()
            if re.fullmatch(r"-?\d+(?:\.\d+)?|true|false|null"
                            r"|\"[^\"\n]*\"|\'[^\'\n]*\'|\{\}|\[\]", rhs):
                proste.append(rhs)
                return None
            return m.group(1) + nazwa + " = " + rhs + ";"

        nowe_linie = []
        for ln in wynik.split("\n"):
            z = _podm(ln)
            if z is not None:
                nowe_linie.append(z)
        wynik = "\n".join(nowe_linie)
        if proste:
            inic[nazwa] = proste[0]
    return wynik, inic


def _bramka_pliku(job_id, job):
    """Kontrola jakości kodu w GOTOWYM wygenerowanym pliku – dokładnie ta
    sama walidacja co bramka edycji (_waliduj_kod zwraca listę problemów
    {klucz, opis, pewny}), z jedną rundą auto-naprawy. Zwraca tekst
    informacji na kartę zadania albo None. Nigdy nie wywraca zadania:
    plik jest dostarczany tak czy siak – zmienia się tylko jego treść
    (po naprawie) i dopisek na karcie."""
    try:
        fname = job["filename"]
        pelny = "".join(job["content_parts"])
        problemy = _waliduj_kod(fname, pelny)
        if not problemy:
            return None
        print(f"BRAMKA_PLIKU_V39 {job_id}: {len(problemy)} problemów w {fname}: "
              + "; ".join(p["opis"] for p in problemy[:3]))
        bloki = _podziel_na_bloki(pelny)
        if len(bloki) >= 2 and _napraw_bloki_kodu(job.get("api_key"), fname, bloki,
                                                  {b["id"] for b in bloki}, problemy, None):
            naprawiony = "".join(b["text"] for b in bloki)
            if naprawiony != pelny:
                job["content_parts"] = [naprawiony]
                job["chars"] = len(naprawiony)
                problemy = _waliduj_kod(fname, naprawiony)
                if not problemy:
                    return "Kontrola kodu: naprawiono wykryte problemy"
        # KONTEKST_V57: gdy po rundzie naprawy zostają już TYLKO pewne
        # problemy o brakujące definicje, bramka dogrywa je jednym
        # wywołaniem. Wynik przyjmujemy wyłącznie, gdy pewnych problemów
        # faktycznie ubyło – inaczej treść zostaje sprzed dopisania.
        _pewne = [p for p in problemy if p.get("pewny", True)]
        _braki = [p["klucz"].split(":", 1)[1] for p in _pewne
                  if p.get("typ") in ("zmienna", "obiekt")
                  and ":" in (p.get("klucz") or "")]
        if _braki and len(_braki) == len(_pewne):
            _aktualny = "".join(job["content_parts"])
            _uzupelniony = _bramka_dopisz_definicje(job.get("api_key"), fname,
                                                    _aktualny, _braki)
            if _uzupelniony and _uzupelniony != _aktualny:
                _po = _waliduj_kod(fname, _uzupelniony)
                _pewne_po = [p for p in _po if p.get("pewny", True)]
                if len(_pewne_po) < len(_pewne):
                    job["content_parts"] = [_uzupelniony]
                    job["chars"] = len(_uzupelniony)
                    problemy = _po
                    print(f"BRAMKA_PLIKU_V57 {job_id}: dopisano brakujące definicje "
                          f"({len(_braki)}), pewnych problemów: {len(_pewne)} -> {len(_pewne_po)}")
                    if not problemy:
                        return "Kontrola kodu: naprawiono wykryte problemy (dopisano brakujące funkcje)"
        # SPOJNOSC_V59: same cienie stanu wspoldzielonego rozwiazujemy
        # deterministycznie (jak w bramce edycji) – jedna deklaracja
        # globalna z wartosci poczatkowej konsumenta zamiast lokalnych
        # kopii w kazdej funkcji.
        _pewne59g = [p for p in problemy if p.get("pewny", True)]
        _cienie59g = [p for p in _pewne59g if p.get("typ") == "cien"]
        if _cienie59g and len(_cienie59g) == len(_pewne59g):
            _nazwy59g = [p["klucz"].split(":", 1)[1] for p in _cienie59g]
            _akt59g = "".join(job["content_parts"])
            _napr59g, _inic59g = _usun_cienie(_akt59g, _nazwy59g)
            if _inic59g and all(n in _inic59g for n in _nazwy59g):
                _kand59g = _wstaw_globale(
                    fname, _napr59g,
                    [f"let {n} = {_inic59g[n]};" for n in _nazwy59g])
                _po59g = _waliduj_kod(fname, _kand59g)
                if len([p for p in _po59g if p.get("pewny", True)]) < len(_pewne59g):
                    job["content_parts"] = [_kand59g]
                    job["chars"] = len(_kand59g)
                    problemy = _po59g
                    print(f"BRAMKA_PLIKU_V59 {job_id}: rozwiązano cienie "
                          f"zmiennych: {', '.join(_inic59g)}")
                    if not problemy:
                        return "Kontrola kodu: naprawiono wykryte problemy (współdzielony stan zmiennych)"
        pewne = [p for p in problemy if p.get("pewny", True)]
        if pewne:
            return "⚠ Kontrola kodu: " + "; ".join(p["opis"] for p in pewne[:2])[:170]
        if problemy:
            return "⚠ Kontrola kodu (możliwe problemy): " + "; ".join(p["opis"] for p in problemy[:2])[:170]
        return "Kontrola kodu: naprawiono wykryte problemy"
    except PauzaBrakKluczy:
        print(f"BRAMKA_PLIKU_V39 {job_id}: pominięta – brak kluczy do rundy naprawy")
        return None
    except Exception as e:
        print(f"BRAMKA_PLIKU_V39 {job_id}: pominięta ({e})")
        return None


MAPY_FOLDER = "My_bro files/system/mapy"


def _mapa_struktura(content: str, filename: str) -> list:
    """Lokalny zarys struktury pliku – bez wywołań modelu: etykiety bloków
    kodu/HTML albo nagłówki tekstu/markdownu."""
    fn = (filename or "").lower()
    linie = []
    try:
        if fn.endswith((".html", ".htm", ".js", ".mjs", ".css", ".py")):
            for b in _podziel_na_bloki(content):
                linie.append(f"{b['label']} ({b['text'].count(chr(10)) + 1} linii)")
        else:
            for wiersz in content.splitlines():
                w = wiersz.strip()
                if w.startswith("#"):
                    linie.append(w[:80])
    except Exception:
        pass
    return linie[:14]


def _mapa_sciezka(filename: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename or "plik")
    return f"{MAPY_FOLDER}/{safe}.json"


def _mapa_zapisz(filename: str, content: str):
    """Zapisuje zwartą mapę pliku do Boxa i aktualizuje indeks map.
    Zero wywołań modelu; błędy tylko loguje."""
    try:
        mapa = {"plik": filename, "znakow": len(content or ""),
                "data": datetime.utcnow().isoformat() + "Z",
                "struktura": _mapa_struktura(content or "", filename)}
        _box_upload_json(_mapa_sciezka(filename), mapa)
        indeks = _box_download_json(f"{MAPY_FOLDER}/indeks.json") or {}
        if not isinstance(indeks, dict):
            indeks = {}
        indeks[filename] = {"znakow": mapa["znakow"], "data": mapa["data"]}
        _box_upload_json(f"{MAPY_FOLDER}/indeks.json", indeks)
        print(f"MAPY_V39: zapisano mapę {filename} ({len(mapa['struktura'])} pozycji struktury)")
    except Exception as e:
        print(f"MAPY_V39: zapis mapy {filename} nieudany: {e}")


def _mapa_czytaj(filename: str):
    try:
        d = _box_download_json(_mapa_sciezka(filename))
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _mapy_kontekst(tekst: str, wlasny_plik: str) -> str:
    """Gdy polecenie/specyfikacja wymienia INNY plik, który ma zapisaną
    mapę, zwraca zwarty opis jego struktury – kontekst projektu bez
    wczytywania całego pliku. Puste, gdy nic nie pasuje."""
    try:
        indeks = _box_download_json(f"{MAPY_FOLDER}/indeks.json") or {}
    except Exception:
        return ""
    if not isinstance(indeks, dict):
        return ""
    trafione = []
    for nazwa in indeks:
        if nazwa and nazwa != wlasny_plik and nazwa in (tekst or ""):
            m = _mapa_czytaj(nazwa)
            if m:
                opis = "; ".join(m.get("struktura") or [])[:500]
                trafione.append(f"Powiązany plik projektu {nazwa} ({m.get('znakow', 0)} znaków): {opis}")
    return "\n".join(trafione[:3])


def _mapy_dopisek(tekst: str, wlasny_plik: str) -> str:
    kontekst = _mapy_kontekst(tekst, wlasny_plik)
    if not kontekst:
        return ""
    return "\nKONTEKST PROJEKTU (zapisane mapy powiązanych plików):\n" + kontekst


# --- EDYCJA_W_KAWALKACH_V10: duża edycja rozłożona na małe zapytania ---
# Cały plik w jednym zapytaniu (np. gra 34 tys. znaków + historia = ~20 tys.
# tokenów) przekracza darmowe limity dostawców (Groq TPM 8000, dzienne limity
# Gemini). Dlatego dużą edycję rozkładamy:
#   faza 1: model dostaje tylko MAPĘ bloków pliku i wskazuje, które bloki
#           trzeba zmienić (odpowiedź: [EDYTUJ_BLOKI: B2, B5]);
#   faza 2: dla każdego wskazanego bloku osobne, małe zapytanie tylko z treścią
#           tego bloku – model zwraca jego nową treść [ZAMIEN_BLOK];
#   backend składa plik z powrotem w całość (bloki spoza edycji co do znaku
#   bez zmian). Każde zapytanie jest kilka razy mniejsze od edycji w całości.
# --- PAUZA_KLUCZY_V34: wykrywanie wyczerpania puli kluczy ---
# Gdy WSZYSTKIE klucze padna na błędach PRZEJŚCIOWYCH (limity 429/quota,
# 503/504, timeouty), proces nie ma umierać z utratą postępu (tak wygasła
# edycja gry na 98%): ma się wstrzymać z zapisem stanu, a użytkownik sam
# zdecyduje, czy wznowić. Błędy trwałe (403 tier, zły klucz, za duży prompt)
# pauzy NIE powodują – kończą jak dotychczas.
_ROTACJA_TLS = threading.local()


def _blad_przejsciowy(tekst: str) -> bool:
    t = (tekst or "").lower()
    for znacznik in ("429", "resource_exhausted", "quota", "rate limit",
                     "too many requests", "503", "504", "unavailable",
                     "deadline_exceeded", "timed out", "timeout"):
        if znacznik in t:
            return True
    return False


def _rotacja_brak_kluczy() -> bool:
    """Czy ostatnie _call_llm_rotacja padło na wyczerpanej puli kluczy?"""
    return bool(getattr(_ROTACJA_TLS, "brak_kluczy", False))


class PauzaBrakKluczy(Exception):
    """Pula kluczy chwilowo wyczerpana – proces ma się wstrzymać
    (z checkpointem), a nie zakończyć błędem."""


# --- OSZCZEDNOSC_V37: wyciszenia kluczy i modeli po błędach limitów ---
# Po błędzie limitu backend ZAPAMIĘTUJE, do kiedy dany model (albo cały
# klucz przy martwym kluczu) nie ma szans, i pomija go lokalnie zamiast
# płacić kolejnymi wywołaniami HTTP przy każdym zapytaniu. Dłuższe
# wyciszenia zapisują się w chmurze (Box), więc restart Space ich nie
# kasuje. Całość działa w try/except – wyciszenie nigdy nie psuje wywołania.
_MUTE = {}
_MUTE_LOCK = threading.Lock()
_MUTE_BOX_PATH = "My_bro files/system/wyciszenia.json"
_MUTE_LOADED = [False]
_MUTE_LAST_SAVE = [0.0]


def _mute_kid(key):
    import hashlib
    return hashlib.sha256((key or "").encode()).hexdigest()[:12]


def _mute_wczytaj():
    if _MUTE_LOADED[0]:
        return
    _MUTE_LOADED[0] = True
    try:
        data = _box_download_json(_MUTE_BOX_PATH)
        now = time.time()
        if isinstance(data, dict):
            for k, v in data.items():
                try:
                    if float(v) > now:
                        _MUTE[k] = float(v)
                except Exception:
                    pass
        if _MUTE:
            print(f"WYCISZENIA: wczytano {len(_MUTE)} aktywnych wyciszeń z chmury")
    except Exception:
        pass


def _mute_zapisz(force=False):
    try:
        now = time.time()
        if not force and now - _MUTE_LAST_SAVE[0] < 30:
            return
        _MUTE_LAST_SAVE[0] = now
        with _MUTE_LOCK:
            dane = {k: v for k, v in _MUTE.items() if v > now + 600}
        def _wyslij():
            try:
                _box_upload_json(_MUTE_BOX_PATH, dane)
            except Exception:
                pass
        threading.Thread(target=_wyslij, daemon=True).start()
    except Exception:
        pass


def _wyciszony(key, model):
    """Czy (klucz, model) jest teraz wyciszony; model '*' oznacza cały klucz."""
    try:
        _mute_wczytaj()
        kid = _mute_kid(key)
        now = time.time()
        with _MUTE_LOCK:
            for scope in ("*", model):
                do = _MUTE.get(f"{kid}|{scope}")
                if do and do > now:
                    return True
        return False
    except Exception:
        return False


def _wycisz(key, model, blad):
    """Klasyfikuje błąd limitu i wycisza (klucz, model) na odpowiedni czas:
    martwy klucz 1 h (cały klucz), dzienna pula 6 h, model spoza planu 24 h,
    tempo 429 – 4 min, przeciążenie 503/504 – 1 min. Inne błędy bez wyciszenia."""
    try:
        t = str(blad).lower()
        zakres = model
        if ("401" in t or "invalid api key" in t or "invalid_api_key" in t
                or "unauthorized" in t):
            sekund, zakres = 3600, "*"
        elif ("per day" in t or "free_tier_requests" in t or "limit: 0" in t
              or "requests per day" in t or "per_day" in t):
            sekund = 6 * 3600
        elif "not available in your subscription" in t or "not available in your tier" in t:
            sekund = 24 * 3600
        elif ("429" in t or "quota" in t or "rate limit" in t
              or "too many requests" in t or "resource_exhausted" in t):
            sekund = 240
        elif "503" in t or "unavailable" in t or "504" in t:
            sekund = 60
        else:
            return
        kid = _mute_kid(key)
        with _MUTE_LOCK:
            _MUTE[f"{kid}|{zakres}"] = max(_MUTE.get(f"{kid}|{zakres}", 0),
                                           time.time() + sekund)
        print(f"WYCISZENIE: {_provider_name(key)}/{zakres} na {sekund // 60} min (limit dostawcy)")
        _mute_zapisz(force=(sekund >= 3600))
    except Exception:
        pass


def _call_llm_rotacja(api_key, msgs, sys_prompt, max_out=8000):
    """Wywołanie LLM z rotacją kluczy (jak w pętlach czatu). Tekst albo None."""
    global current_key_index
    _ROTACJA_TLS.brak_kluczy = False
    keys = ([api_key] if api_key else []) + api_keys[current_key_index:] + api_keys[:current_key_index]
    keys = list(dict.fromkeys(keys))  # OSZCZEDNOSC_V37: ten sam klucz tylko raz
    byl_przejsciowy = False
    for idx, k in enumerate(keys):
        if idx > 15: break
        try:
            out = call_llm(k, msgs, sys_prompt, max_out=max_out)
            if k in api_keys: current_key_index = api_keys.index(k)
            if out: return out
        except Exception as e:
            if _blad_przejsciowy(str(e)):
                byl_przejsciowy = True
            print(f"ROTACJA key fail: {e}")
    _ROTACJA_TLS.brak_kluczy = byl_przejsciowy
    return None

def _nawias_zamyka(txt: str, start: int) -> int:
    """Indeks '}' zamykającego blok otwarty na txt[start] (proste zliczanie nawiasów)."""
    depth = 0
    i = start
    while i < len(txt):
        c = txt[i]
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1

def _podziel_na_bloki(content: str) -> list:
    """Dzieli treść pliku na bloki: nazwane funkcje JS + odcinki szkieletu między
    nimi (szkielet cięty co ~6000 znaków na granicach linii). Bloki w kolejności
    pokrywają CAŁY plik – złożenie ich daje oryginał co do znaku."""
    spans = []
    for m in re.finditer(r"function\s+([A-Za-z_$][\w$]*)\s*\(", content):
        brace = content.find("{", m.end())
        if brace == -1:
            continue
        end = _nawias_zamyka(content, brace)
        if end == -1:
            continue
        spans.append((m.start(), end + 1, f"funkcja {m.group(1)}"))
    spans.sort()
    clean = []
    last_end = -1
    for s, e, lbl in spans:
        if s >= last_end:  # pomijamy funkcje zagnieżdżone w innych (zostają w ich bloku)
            clean.append((s, e, lbl))
            last_end = e
    blocks = []

    def add_skeleton(a, b):
        while b - a > 6000:
            cut = content.rfind("\n", a + 1000, a + 6000)
            if cut == -1:
                cut = a + 5999
            blocks.append({"label": "szkielet", "text": content[a:cut + 1]})
            a = cut + 1
        if b > a:
            blocks.append({"label": "szkielet", "text": content[a:b]})

    pos = 0
    for s, e, lbl in clean:
        if s > pos:
            add_skeleton(pos, s)
        blocks.append({"label": lbl, "text": content[s:e]})
        pos = e
    if pos < len(content):
        add_skeleton(pos, len(content))
    for i, b in enumerate(blocks, 1):
        b["id"] = f"B{i}"
        first = next((ln.strip() for ln in b["text"].splitlines() if ln.strip()), "")
        b["first"] = first[:90]
    return blocks

# --- BRAMA_KODU_V33: walidacja kodu po edycji + kontrola zakresu zmian ---
# Powod: poprawka gry (przyciski) wrocila z modelem-przepisanymi blokami kodu,
# w ktorych deklaracja zmiennej wpadla w komentarz (pColor) i dopisano ruch
# liczony z nieistniejacej wlasciwosci (touchMove.angle) - gra zamarzala przy
# pierwszym nacisnieciu. Backend nie przepuszcza juz takich edycji po cichu:
# sprawdza kod, probuje jednej naprawy blokow i w razie porazki odrzuca
# edycje, zostajac przy ostatniej dobrej wersji pliku.
_JS_GLOBALS = {
    "window", "document", "console", "Math", "JSON", "Object", "Array",
    "String", "Number", "Boolean", "Promise", "Symbol", "BigInt", "Error",
    "TypeError", "ReferenceError", "SyntaxError", "RangeError", "Date",
    "RegExp", "Map", "Set", "WeakMap", "WeakSet", "parseInt", "parseFloat",
    "isNaN", "isFinite", "NaN", "Infinity", "undefined", "setTimeout",
    "setInterval", "clearTimeout", "clearInterval", "requestAnimationFrame",
    "cancelAnimationFrame", "performance", "navigator", "location",
    "history", "localStorage", "sessionStorage", "alert", "confirm",
    "prompt", "fetch", "Image", "Audio", "Video", "Canvas", "Event",
    "CustomEvent", "KeyboardEvent", "MouseEvent", "TouchEvent", "URL",
    "Blob", "FileReader", "FormData", "encodeURIComponent",
    "decodeURIComponent", "encodeURI", "decodeURI", "globalThis", "self",
    "addEventListener", "removeEventListener", "getComputedStyle",
    "devicePixelRatio", "innerWidth", "innerHeight", "screen",
}


def _js_skrypty(fname, content):
    """[(kod, numer_pierwszej_linii)] - skrypty z pliku HTML albo caly JS."""
    low = (fname or "").lower()
    if low.endswith((".js", ".mjs")):
        return [(content, 1)]
    if not low.endswith((".html", ".htm")):
        return []
    out = []
    for m in re.finditer(r"<script[^>]*>(.*?)</script>", content or "",
                         re.DOTALL | re.IGNORECASE):
        out.append((m.group(1), (content[:m.start(1)].count("\n") + 1)))
    return out


def _ast_walk(node, parent=None):
    yield node, parent
    for v in vars(node).values():
        if isinstance(v, list):
            for it in v:
                if hasattr(it, "type"):
                    yield from _ast_walk(it, node)
        elif hasattr(v, "type"):
            yield from _ast_walk(v, node)


def _ast_lin(node, linia0):
    try:
        return linia0 + int(node.loc.start.line) - 1
    except Exception:
        return linia0


def _waliduj_skrypt_ast(kod, linia0, dodatkowe=frozenset()):
    """Kontrola przez prawdziwy parser JS (esprima): skladnia, zmienne
    uzyte a niezadeklarowane, odczyty wlasciwosci, ktore nie istnieja."""
    import esprima
    problemy = []
    tree = None
    for parser in (esprima.parseScript, esprima.parseModule):
        try:
            tree = parser(kod, {"loc": True})
            break
        except Exception:
            tree = None
    if tree is None:
        try:
            esprima.parseScript(kod, {"loc": True})
        except Exception as e:
            msg = str(e)
            ln = getattr(e, "lineNumber", None)
            if not ln:
                mm = re.search(r"[Ll]ine (\d+)", msg)
                ln = int(mm.group(1)) if mm else None
            problemy.append({
                "typ": "skladnia", "pewny": True,
                "klucz": "skladnia:" + re.sub(r"\d+", "#", msg)[:70],
                "opis": f"błąd składni JavaScript: {msg}",
                "linia": (linia0 + ln - 1) if ln else linia0})
        return problemy
    declared, decl_ids = set(), set()

    def _zbierz_wzorzec(pat):
        if pat is None:
            return
        for sub, _p in _ast_walk(pat):
            if sub.type == "Identifier":
                declared.add(sub.name)
                decl_ids.add(id(sub))

    nodes = list(_ast_walk(tree))
    for node, _parent in nodes:
        t = node.type
        if t == "VariableDeclarator":
            _zbierz_wzorzec(node.id)
        elif t in ("FunctionDeclaration", "FunctionExpression",
                   "ArrowFunctionExpression",
                   "ClassDeclaration", "ClassExpression"):
            if getattr(node, "id", None) is not None:
                declared.add(node.id.name)
                decl_ids.add(id(node.id))
            for prm in (node.params or []):
                _zbierz_wzorzec(prm)
        elif t == "CatchClause":
            _zbierz_wzorzec(node.param)
        elif t == "AssignmentExpression" and node.left.type == "Identifier":
            declared.add(node.left.name)  # tolerancja: global dorozumiany
    pierwsze_uzycie = {}
    for node, parent in nodes:
        if node.type != "Identifier" or id(node) in decl_ids:
            continue
        if parent is not None:
            if parent.type == "MemberExpression" and parent.property is node \
                    and not parent.computed:
                continue
            if parent.type == "Property" and parent.key is node \
                    and not parent.computed:
                continue
            if parent.type in ("LabeledStatement", "BreakStatement",
                               "ContinueStatement") \
                    and getattr(parent, "label", None) is node:
                continue
            # DIETA_V60: nazwa metody/pola klasy to nie jest użycie
            # zmiennej – bez tego walidator zgłaszał init, playGunshot,
            # constructor czy shoot jako "brakujące zmienne" w każdym
            # pliku z klasami (18 fałszywych zgłoszeń w grave.html).
            if parent.type in ("MethodDefinition", "PropertyDefinition") \
                    and getattr(parent, "key", None) is node:
                continue
        pierwsze_uzycie.setdefault(node.name, _ast_lin(node, linia0))
    for name, ln in pierwsze_uzycie.items():
        if name not in declared and name not in _JS_GLOBALS \
                and name not in dodatkowe:
            problemy.append({
                "typ": "zmienna", "pewny": True, "klucz": f"zmienna:{name}",
                "opis": f"zmienna '{name}' jest używana, ale nigdzie nie jest "
                        "zadeklarowana (przy uruchomieniu: ReferenceError)",
                "linia": ln})
    # wlasciwosci obiektow-stanu zdefiniowanych literalem w tym skrypcie
    literals = {}
    for node, _parent in nodes:
        if node.type == "VariableDeclarator" and node.id.type == "Identifier" \
                and getattr(node, "init", None) is not None \
                and node.init.type == "ObjectExpression":
            keys = set()
            for pr in node.init.properties:
                if pr.type == "Property":
                    k = pr.key
                    keys.add(k.name if k.type == "Identifier" else k.value)
            if keys:
                literals[node.id.name] = keys
    przypisane = {n: set() for n in literals}
    odczyty = {}
    for node, parent in nodes:
        if node.type == "MemberExpression" and not node.computed \
                and node.object.type == "Identifier" \
                and node.object.name in literals \
                and node.property.type == "Identifier":
            prop = node.property.name
            if parent is not None and parent.type == "AssignmentExpression" \
                    and parent.left is node:
                przypisane[node.object.name].add(prop)
            else:
                odczyty.setdefault((node.object.name, prop),
                                   _ast_lin(node, linia0))
    for (obj, prop), ln in odczyty.items():
        if prop not in literals[obj] and prop not in przypisane[obj]:
            problemy.append({
                "typ": "wlasciwosc", "pewny": True, "klucz": f"wl:{obj}.{prop}",
                "opis": f"'{obj}.{prop}' jest odczytywane, ale obiekt '{obj}' "
                        "nie ma takiej właściwości w swojej definicji ani nie "
                        "jest ona nigdzie przypisana (wartość: undefined/NaN)",
                "linia": ln})
    # SPOJNOSC_V59: martwe okablowanie – kontrole PRZEPLYWU danych, nie
    # tylko deklaracji. (a) Obiekt uzywany za warunkiem typeof, ale nigdzie
    # niezadeklarowany: warunek jest zawsze falszywy, wiec kod za nim nigdy
    # sie nie wykona (tak zginal prawy kontroler grave.html – Player.update
    # czytal niezadeklarowany joystickRight spod typeof).
    nazwy_typeof = set()
    for node, _p in nodes:
        if node.type == "UnaryExpression" and getattr(node, "operator", "") == "typeof" \
                and getattr(node, "argument", None) is not None \
                and node.argument.type == "Identifier":
            nazwy_typeof.add(node.argument.name)
    dostepy = set()
    for node, _p in nodes:
        if node.type == "MemberExpression" and node.object.type == "Identifier":
            dostepy.add(node.object.name)
    for _nm in sorted(nazwy_typeof):
        if _nm in dostepy and _nm not in declared and _nm not in _JS_GLOBALS \
                and _nm not in dodatkowe:
            problemy.append({
                "typ": "obiekt", "pewny": True, "klucz": f"obiekt:{_nm}",
                "opis": f"obiekt '{_nm}' jest używany (m.in. za warunkiem typeof), "
                        "ale nigdzie nie jest zadeklarowany – kod za tym warunkiem "
                        "nigdy się nie wykona (zadeklaruj go raz, globalnie)",
                "linia": pierwsze_uzycie.get(_nm, linia0)})
    # (b) Cienie stanu wspoldzielonego: ta sama nazwa deklarowana lokalnie
    # w kilku funkcjach, a nigdzie globalnie – przy czym jedna funkcja
    # zapisuje ja wartoscia liczona (producent), a inna tylko ja czyta,
    # liczac na wartosc z zewnatrz (konsument). Kazda widzi wlasna kopie,
    # wiec mechanizm jest martwy mimo poprawnych deklaracji (tak naprawa
    # bramki "naprawila" rightStickX/Y w grave.html). Zwykle zmienne
    # robocze (rect, dx…) nie lapia sie w ten wzorzec: kazda funkcja
    # sama je liczy i sama czyta.
    parent_of = {id(n): pp for n, pp in nodes}

    def _zakres(node):
        cur = parent_of.get(id(node))
        while cur is not None:
            if cur.type in ("FunctionDeclaration", "FunctionExpression",
                            "ArrowFunctionExpression"):
                return id(cur)
            cur = parent_of.get(id(cur))
        return 0

    dekl_lok, dekl_glob = {}, set()
    prod, czyt = {}, {}
    for node, _p in nodes:
        if node.type == "VariableDeclarator" and node.id.type == "Identifier":
            zak = _zakres(node)
            if zak == 0:
                dekl_glob.add(node.id.name)
            else:
                dekl_lok.setdefault(node.id.name, {})[zak] = _ast_lin(node, linia0)
                _init = getattr(node, "init", None)
                if _init is not None and _init.type != "Literal":
                    prod.setdefault(node.id.name, set()).add(zak)
        elif node.type in ("FunctionDeclaration", "ClassDeclaration") \
                and getattr(node, "id", None) is not None:
            if _zakres(node) == 0:
                dekl_glob.add(node.id.name)
        elif node.type == "AssignmentExpression" and node.left.type == "Identifier":
            if getattr(node, "operator", "=") != "=" \
                    or getattr(node.right, "type", "") != "Literal":
                prod.setdefault(node.left.name, set()).add(_zakres(node))
        elif node.type == "UpdateExpression" \
                and getattr(node, "argument", None) is not None \
                and node.argument.type == "Identifier":
            prod.setdefault(node.argument.name, set()).add(_zakres(node))
    for node, parent in nodes:
        if node.type != "Identifier" or id(node) in decl_ids:
            continue
        if parent is not None:
            if parent.type == "MemberExpression" and parent.property is node \
                    and not parent.computed:
                continue
            if parent.type == "Property" and parent.key is node \
                    and not parent.computed:
                continue
            if parent.type in ("LabeledStatement", "BreakStatement",
                               "ContinueStatement") \
                    and getattr(parent, "label", None) is node:
                continue
            # DIETA_V60: nazwa metody/pola klasy to nie jest użycie
            # zmiennej – bez tego walidator zgłaszał init, playGunshot,
            # constructor czy shoot jako "brakujące zmienne" w każdym
            # pliku z klasami (18 fałszywych zgłoszeń w grave.html).
            if parent.type in ("MethodDefinition", "PropertyDefinition") \
                    and getattr(parent, "key", None) is node:
                continue
            if parent.type == "VariableDeclarator" and parent.id is node:
                continue
            if parent.type == "AssignmentExpression" and parent.left is node:
                continue
            if parent.type == "UpdateExpression" \
                    and getattr(parent, "argument", None) is node:
                continue
        czyt.setdefault(node.name, set()).add(_zakres(node))
    for _nm, _zakmap in sorted(dekl_lok.items()):
        if len(_zakmap) < 2 or _nm in dekl_glob or _nm in _JS_GLOBALS:
            continue
        _prod = prod.get(_nm, set())
        _kons = [z for z in czyt.get(_nm, set())
                 if z != 0 and z in _zakmap and z not in _prod]
        if _prod and _kons:
            problemy.append({
                "typ": "cien", "pewny": True, "klucz": f"cien:{_nm}",
                "opis": f"zmienna '{_nm}' jest zadeklarowana lokalnie w kilku "
                        "funkcjach, ale nigdzie globalnie – jedna funkcja ją "
                        "zapisuje, inna tylko czyta, a każda widzi własną kopię, "
                        "więc wartości się nie przenoszą (zadeklaruj ją raz na "
                        "poziomie skryptu, a lokalne deklaracje usuń)",
                "linia": min(_zakmap.values())})
    return problemy


_JS_KEYWORDS = {
    "if", "else", "for", "while", "do", "return", "function", "const",
    "let", "var", "new", "typeof", "instanceof", "in", "of", "switch",
    "case", "break", "continue", "try", "catch", "finally", "throw",
    "class", "extends", "this", "super", "delete", "void", "yield",
    "async", "await", "static", "get", "set", "default", "from", "export",
    "import", "true", "false", "null",
}


def _js_czysty_kod(kod):
    """Kod JS bez komentarzy i z wygaszonymi tresciami napisow (do analizy
    tekstowej). Deklaracja schowana w komentarzu znika - jak dla przegladarki."""
    out, i, n, stan = [], 0, len(kod), None
    while i < n:
        c = kod[i]
        if stan is None:
            if kod.startswith("//", i):
                j = kod.find("\n", i)
                j = n if j == -1 else j
                out.append(" " * (j - i))
                i = j
            elif kod.startswith("/*", i):
                j = kod.find("*/", i + 2)
                j = n if j == -1 else j + 2
                out.append(re.sub(r"[^\n]", " ", kod[i:j]))
                i = j
            elif c in "'\"`":
                stan = c
                out.append(" ")
                i += 1
            else:
                out.append(c)
                i += 1
        else:
            if c == "\\":
                out.append("  ")
                i += 2
            elif c == stan:
                stan = None
                out.append(" ")
                i += 1
            else:
                out.append("\n" if c == "\n" else " ")
                i += 1
    return "".join(out)


def _waliduj_skrypt_txt(kod, linia0, dodatkowe=frozenset()):
    """Kontrola tekstowa (gdy brak parsera esprima): te same dwie klasy
    bledow, ale wyniki sa NIEPEWNE (brama moze prosic o naprawe, nie odrzuca)."""
    problemy = []
    czysty = _js_czysty_kod(kod)

    def linia(pos):
        return linia0 + czysty[:pos].count("\n")

    declared = set()
    for m in re.finditer(r"\b(?:let|const|var)\s+([A-Za-z_$][\w$]*)", czysty):
        declared.add(m.group(1))
        seg = czysty[m.end():czysty.find(";", m.end()) if czysty.find(";", m.end()) != -1 else len(czysty)]
        for m2 in re.finditer(r",\s*([A-Za-z_$][\w$]*)\s*=", seg):
            declared.add(m2.group(1))
    for m in re.finditer(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)", czysty):
        declared.add(m.group(1))
        for prm in m.group(2).split(","):
            prm = prm.strip().split("=")[0].strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", prm):
                declared.add(prm)
    for m in re.finditer(r"\bclass\s+([A-Za-z_$][\w$]*)", czysty):
        declared.add(m.group(1))
    for m in re.finditer(r"([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{", czysty):
        declared.add(m.group(1))  # definicje metod w klasach i literałach
    for m in re.finditer(r"\b([A-Za-z_$][\w$]*)\s*=(?![=>])", czysty):
        declared.add(m.group(1))  # przypisanie bez deklaracji - tolerancja
    uzyte = {}
    for m in re.finditer(r"[A-Za-z_$][\w$]*", czysty):
        name = m.group(0)
        if name in _JS_KEYWORDS:
            continue
        if m.start() > 0 and czysty[m.start() - 1] == ".":
            continue
        nastepny = czysty[m.end():m.end() + 1]
        if nastepny == ":" :
            continue
        uzyte.setdefault(name, linia(m.start()))
    for name, ln in uzyte.items():
        if name not in declared and name not in _JS_GLOBALS \
                and name not in dodatkowe:
            problemy.append({
                "typ": "zmienna", "pewny": False, "klucz": f"zmienna:{name}",
                "opis": f"zmienna '{name}' prawdopodobnie nie jest nigdzie "
                        "zadeklarowana (kontrola uproszczona)",
                "linia": ln})
    # wlasciwosci obiektow-stanu (tekstowo - dosc precyzyjne)
    for m in re.finditer(r"\b([A-Za-z_$][\w$]*)\s*=\s*\{", czysty):
        obj = m.group(1)
        brace = czysty.find("{", m.start())
        end = _nawias_zamyka(czysty, brace)
        if end == -1:
            continue
        wnetrze = czysty[brace + 1:end]
        keys, depth = set(), 0
        for km in re.finditer(r"([A-Za-z_$][\w$]*)\s*:|[{}]", wnetrze):
            if km.group(0) in "{}":
                depth += 1 if km.group(0) == "{" else -1
            elif depth == 0 and km.group(1):
                keys.add(km.group(1))
        if not keys:
            continue
        przyp = set(re.findall(rf"\b{re.escape(obj)}\.([A-Za-z_$][\w$]*)\s*=", czysty))
        for rm in re.finditer(rf"\b{re.escape(obj)}\.([A-Za-z_$][\w$]*)", czysty):
            prop = rm.group(1)
            if prop not in keys and prop not in przyp:
                problemy.append({
                    "typ": "wlasciwosc", "pewny": False,
                    "klucz": f"wl:{obj}.{prop}",
                    "opis": f"'{obj}.{prop}' jest odczytywane, ale obiekt "
                            f"'{obj}' nie ma takiej właściwości (kontrola "
                            "uproszczona)",
                    "linia": linia(rm.start())})
                break
    return problemy


def _waliduj_kod(fname, content):
    """Problemy w skryptach pliku [{typ, klucz, opis, linia, pewny}].
    Pusta lista = czysto. Wlasny blad walidacji nigdy nie blokuje edycji."""
    try:
        problemy = []
        dodatkowe = frozenset(
            re.findall(r"\bid\s*=\s*[\"']([^\"']+)", content or ""))
        for kod, linia0 in _js_skrypty(fname, content):
            if not kod.strip():
                continue
            try:
                import esprima  # noqa: F401
                problemy += _waliduj_skrypt_ast(kod, linia0, dodatkowe)
            except ImportError:
                problemy += _waliduj_skrypt_txt(kod, linia0, dodatkowe)
        # SPOJNOSC_V59: funkcja zdefiniowana, ale nigdzie nie wywolywana
        # (ostrzezenie – sama definicja niczego nie uruchamia; w grave.html
        # updateShootStick istniala, lecz zaden handler jej nie wolal)
        try:
            _tekst_skr = "\n".join(k for k, _l in _js_skrypty(fname, content))
            for _nf in sorted(set(re.findall(
                    r"function\s+([A-Za-z_$][\w$]*)\s*\(", _tekst_skr))):
                if len(re.findall(r"\b" + re.escape(_nf) + r"\b",
                                   content or "")) <= 1:
                    problemy.append({
                        "typ": "martwa", "pewny": False,
                        "klucz": f"martwa:{_nf}",
                        "opis": f"funkcja '{_nf}' jest zdefiniowana, ale nigdzie "
                                "nie jest wywoływana – prawdopodobnie brakuje jej "
                                "podpięcia (zdarzenie albo wywołanie)",
                        "linia": 0})
        except Exception:
            pass
        return problemy
    except Exception as e:
        print(f"BRAMA_KODU: walidacja pominięta ({e})")
        return []


def _prosba_tylko_o_wyglad(text):
    """Czy polecenie dotyczy WYLACZNIE wygladu/ukladu (bez mechaniki kodu)?"""
    t = (text or "").lower()
    kodowe = ("mechanik", "logik", "funkcj", "strzel", "przeciwnik", "wrog",
              "wróg", "fizyk", "obraż", "obraz", "punkt", "prędko", "predko",
              "zachowan", "algorytm", "ruch gracza", "ruch postaci",
              "szybkostrzel", "zadawan")
    if any(k in t for k in kodowe):
        return False
    wyglad = ("przycisk", "klawisz", "wygląd", "wyglad", "kolor", "położ",
               "poloz", "pozycj", "styl", "czcionk", "rozmiar", "przesu",
               "podnieś", "podnies", "opuść", "opusc", "margines", "odstęp",
               "odstep", "ekran", "ikon", "tło", "tlo", "ramk", "środk",
               "srodk", "css", "układ", "uklad", "wyżej", "wyzej", "niżej",
               "nizej", "lewo", "prawo", "góra", "dół")
    return any(k in t for k in wyglad)


def _blok_kodowy(b):
    """Czy blok zawiera kod JavaScript (a nie style/markup)?"""
    if (b.get("label") or "").startswith("funkcja"):
        return True
    t = b.get("text") or ""
    if "<style" in t:
        return False
    return ("function" in t) or ("=>" in t)


def _liczba_zmienionych_linii(stary, nowy):
    import difflib
    sm = difflib.SequenceMatcher(None, stary.splitlines(), nowy.splitlines())
    n = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "delete"):
            n += i2 - i1
        if tag in ("replace", "insert"):
            n += j2 - j1
    return n


def _blok_z_tagow(odp, stary_tekst=None):
    """Treść bloku z tagów [ZAMIEN_BLOK]...[/ZAMIEN_BLOK] z odpowiedzi modelu.
    Usuwa co najwyżej po JEDNEJ nowej linii przy tagach. Wcześniejsza wersja
    zjadała WSZYSTKIE białe znaki na brzegach bloku – blok
    kończący się komentarzem // tracił końcową nową linię i po złożeniu
    pliku wykomentowywał pierwszy wiersz NASTĘPNEGO bloku (np. deklarację
    funkcji), co rozwalało składnię całości. Dlatego: gdy stary blok kończył
    się nową linią albo nowy kończy się komentarzem //, nowa linia wraca."""
    m = re.search(r"\[ZAMIEN_BLOK\](.*?)\[/ZAMIEN_BLOK\]", odp or "",
                  re.DOTALL | re.IGNORECASE)
    if not m:
        return None
    t = m.group(1)
    if t.startswith("\r\n"):
        t = t[2:]
    elif t.startswith("\n"):
        t = t[1:]
    if t.endswith("\r\n"):
        t = t[:-2]
    elif t.endswith("\n"):
        t = t[:-1]
    if not t.endswith("\n"):
        ostatnia = t.rstrip().rsplit("\n", 1)[-1].strip()
        if (stary_tekst or "").endswith("\n") or ostatnia.startswith("//"):
            t += "\n"
    return t


def _napraw_bloki_kodu(api_key, fname, blocks, chosen_ids, problemy, rid=None):
    """Jedna runda auto-naprawy: model dostaje TYLKO bloki, w ktorych brama
    wykryla problemy, razem z diagnostyka. Zwraca True, gdy cos podmieniono."""
    # zakresy linii blokow w zlozonym pliku
    zakresy, linia = {}, 1
    for b in blocks:
        nl = b["text"].count("\n") + 1
        zakresy[b["id"]] = (linia, linia + nl - 1)
        linia += nl
    docelowe = []
    for p in problemy:
        for b in blocks:
            if b["id"] not in chosen_ids:
                continue
            a, z = zakresy[b["id"]]
            if a - 2 <= (p.get("linia") or 0) <= z + 2 and b["id"] not in docelowe:
                docelowe.append(b["id"])
    if not docelowe:
        docelowe = list(chosen_ids)[:2]
    poprawiono = False
    for bid in docelowe[:2]:
        b = next(x for x in blocks if x["id"] == bid)
        diag = "\n".join(f"- {p['opis']} (linia {p.get('linia')})"
                         for p in problemy)
        _chat_job_set_stage(rid, f"Brama kodu: naprawiam fragment {b['label']}…")
        msgs = [{'role': 'user', 'parts': [
            "[SYSTEM – NAPRAWA BLOKU PO KONTROLI KODU] Plik " + fname + ", blok [" +
            b["id"] + "] (" + b["label"] + "). Automatyczna kontrola kodu wykryła "
            "problemy w tym bloku:\n" + diag + "\n"
            "Zwróć POPRAWIONĄ treść TYLKO tego bloku w tagach [ZAMIEN_BLOK] ... "
            "[/ZAMIEN_BLOK] i nic więcej. Napraw wyłącznie wskazane problemy; "
            "każdą pozostałą linię przepisz DOKŁADNIE tak, jak jest – nie łącz "
            "linii, nie zmieniaj formatowania ani nazw.\n<<<BLOK\n" + b["text"] + "\n>>>"]}]
        odp = _call_llm_rotacja(api_key, msgs, SYS_MECH)
        if not odp:
            if _rotacja_brak_kluczy():
                raise PauzaBrakKluczy()
            continue
        nowa_tresc = _blok_z_tagow(odp, b["text"])
        if nowa_tresc is not None:
            b["text"] = nowa_tresc
            poprawiono = True
            print(f"BRAMA_KODU: naprawiono blok {bid} w {fname}")
    return poprawiono


def _edycja_w_kawalkach(api_key, user_text, fname, content, base_msgs, rid=None,
                        job_id=None, wznow=None):
    """Dwufazowa edycja dużego pliku małymi zapytaniami.
    Zwraca (nowa_treść, [etykiety zmienionych bloków], odrzucona_info|None)
    albo None (wtedy wołający wraca do klasycznej edycji całą treścią).
    BRAMA_KODU_V33: po złożeniu pliku kod HTML/JS przechodzi kontrolę –
    najpierw zakres (prośba tylko o wygląd nie rusza bloków kodu), potem
    walidację z jedną rundą auto-naprawy; gdy krytyczne błędy zostają,
    edycja jest ODRZUCANA i zwracany jest oryginalny plik.
    PAUZA_KLUCZY_V34: przy job_id postępy idą do checkpointów zadania,
    a wyczerpanie puli kluczy podnosi PauzaBrakKluczy (zadanie czeka na
    decyzję użytkownika); wznow={chosen, bloki} odtwarza stan po pauzie."""
    blocks = _podziel_na_bloki(content)
    if len(blocks) < 2:
        return None
    by_id = {b["id"]: b for b in blocks}

    def mapa_txt(lista):
        return "\n".join(
            f"- [{b['id']}] {b['label']} ({len(b['text'])} znaków) – zaczyna się: {b['first']}"
            for b in lista)

    def wybierz(lista):
        _chat_job_set_stage(rid, "Edycja w kawałkach: wybieram fragmenty do zmiany…")
        # DIETA_V60: faza 1 NIE dostaje historii rozmowy (base_msgs) –
        # do wyboru bloków wystarcza bieżące polecenie, mapa pliku i mapy
        # projektu. Z historią prompt fazy 1 miał ~9,6 tys. tokenów i żaden
        # szybki dostawca (Groq, limit 8 tys.) nie mógł go przyjąć, więc
        # bloki wybierał najsłabszy model z końca kaskady.
        faza1_msgs = [{'role': 'user', 'parts': [
            "[SYSTEM – EDYCJA W KAWAŁKACH] Użytkownik chce zmienić plik " + fname + ", ale plik jest "
            "za duży, by edytować go w jednym zapytaniu. Poniżej MAPA bloków pliku. Wskaż TYLKO bloki, "
            "które trzeba zmienić, aby wykonać polecenie użytkownika – odpowiedz wyłącznie w formacie: "
            "[EDYTUJ_BLOKI: B2, B5] (maks. 4 bloki, bez żadnego innego tekstu). "
            "Jeśli polecenie dotyczy tylko wyglądu (style CSS, przyciski, układ strony), "
            "NIE wskazuj bloków z kodem JavaScript. Jeśli polecenie dotyczy zachowania, sterowania lub zdarzeń, dołącz także blok podpinający zdarzenia (setup/addEventListener) oraz blok korzystający ze zmienianego stanu – zmiana samej funkcji logiki zwykle nie wystarcza.\n"
            "Polecenie użytkownika: " + user_text + "\nMAPA PLIKU " + fname + ":\n" + mapa_txt(lista)
            + _mapy_dopisek(user_text, fname)]}]
        # FAZA1_ODPORNA_V41: budżet odpowiedzi 1000 (modele rozumujące
        # potrafią zużyć 400 na samo myślenie i uciąć tag), a gdy odpowiedź
        # przyjdzie bez tagu – ratujemy wybór z treści (produkcja 2026-11-02:
        # Gemini odpowiedział opisowo i edycja padła mimo dobrej odpowiedzi).
        odp1 = _call_llm_rotacja(api_key, faza1_msgs, SYS_MECH, max_out=1000)
        if not odp1:
            if _rotacja_brak_kluczy():
                raise PauzaBrakKluczy()
            return None
        m = re.search(r"\[EDYTUJ_BLOKI:\s*([^\]]+)\]", odp1, re.IGNORECASE)
        if m:
            ids = [x.strip().upper() for x in re.split(r"[,\s]+", m.group(1)) if x.strip()]
        else:
            ids = []
            for tok in re.findall(r"\bB(\d+)\b", odp1):
                bid = "B" + tok
                if bid in by_id and bid not in ids:
                    ids.append(bid)
            if ids:
                print(f"EDYCJA_W_KAWALKACH: brak tagu [EDYTUJ_BLOKI:], ale z odpowiedzi "
                      f"wyciągnięto bloki {ids} – idę dalej")
            else:
                print("EDYCJA_W_KAWALKACH: model nie wskazał bloków – odpowiedź: "
                      + odp1[:200].replace("\n", " "))
                return None
        return [by_id[i] for i in ids if i in by_id and by_id[i] in lista][:4]

    # PAUZA_KLUCZY_V34: wznowienie z checkpointu – wybór bloków już zapadł
    wznow_ids = (wznow or {}).get("chosen") or []
    wznow_bloki = (wznow or {}).get("bloki") or {}
    globale = list((wznow or {}).get("globale") or [])  # SPOJNOSC_V59
    chosen = None
    if wznow_ids:
        chosen = [by_id[i] for i in wznow_ids if i in by_id][:4] or None
        if chosen:
            print(f"EDYCJA_W_KAWALKACH: wznawiam z zapisu – {len(chosen)} bloków, "
                  f"{len(wznow_bloki)} już podmienionych")
    if not chosen:
        chosen = wybierz(blocks)
        if chosen is None:
            return None
        # BRAMA_KODU_V33: kontrola zakresu – prośba TYLKO o wygląd nie rusza kodu
        if _prosba_tylko_o_wyglad(user_text):
            if chosen and all(_blok_kodowy(b) for b in chosen):
                dozwolone = [b for b in blocks if not _blok_kodowy(b)]
                if dozwolone:
                    print("BRAMA_KODU: prośba tylko o wygląd, a model wskazał same "
                          "bloki kodu – ponawiam wybór z mapy bez kodu")
                    chosen = wybierz(dozwolone) or []
            przed = len(chosen)
            chosen = [b for b in chosen if not _blok_kodowy(b)]
            if len(chosen) < przed:
                print(f"BRAMA_KODU: odfiltrowano {przed - len(chosen)} bloków kodu "
                      "przy prośbie tylko o wygląd")
            if not chosen:
                print("EDYCJA_W_KAWALKACH: bramka zakresu – brak dozwolonych bloków, przerywam")
                return None
        if not chosen:
            return None
    if sum(len(b["text"]) for b in chosen) > 0.6 * len(content):
        print("EDYCJA_W_KAWALKACH: wskazane bloki to większość pliku – wracam do zwykłej edycji")
        return None
    stare = {b["id"]: b["text"] for b in chosen}
    for b in chosen:
        if b["id"] in wznow_bloki:
            b["text"] = wznow_bloki[b["id"]]
    zmienione = []
    if job_id:
        _edit_checkpoint(job_id, chosen, stare, 0, zmienione, globale=globale)
    for n, b in enumerate(chosen, 1):
        if b["id"] in wznow_bloki:
            zmienione.append(f"{b['label']} ({_liczba_zmienionych_linii(stare[b['id']], b['text'])} zm. linii)")
            continue
        _chat_job_set_stage(rid, f"Edycja w kawałkach: przerabiam fragment {n}/{len(chosen)} ({b['label']})…")
        faza2_msgs = [{'role': 'user', 'parts': [
            "[SYSTEM – EDYCJA BLOKU] Plik " + fname + ", blok [" + b["id"] + "] (" + b["label"] + "). "
            "Polecenie użytkownika: " + user_text + "\n"
            "Poniżej aktualna treść TYLKO tego bloku. Zwróć jego NOWĄ treść (z wprowadzoną zmianą) "
            "w tagach [ZAMIEN_BLOK] ... [/ZAMIEN_BLOK] i nic więcej. Nie odtwarzaj reszty pliku – "
            "backend złoży całość sam, a wszystko poza tym blokiem zostaje bez zmian.\n"
            "ZASADY: zmieniaj WYŁĄCZNIE linie związane z poleceniem użytkownika. Każdą pozostałą "
            "linię przepisz DOKŁADNIE tak, jak jest – nie łącz linii, nie zmieniaj formatowania, "
            "komentarzy ani nazw zmiennych i nie poprawiaj niczego przy okazji.\n"
            + _kontrakt_pliku(fname, content) +
            "<<<BLOK\n" + b["text"] + "\n>>>"]}]
        odp2 = _call_llm_rotacja(api_key, faza2_msgs, SYS_MECH)
        if not odp2:
            if _rotacja_brak_kluczy():
                raise PauzaBrakKluczy()
            return None
        nowa_tresc = _blok_z_tagow(odp2, b["text"])
        if nowa_tresc is None:
            print(f"EDYCJA_W_KAWALKACH: brak [ZAMIEN_BLOK] dla {b['id']} – przerywam, wracam do zwykłej edycji")
            return None
        b["text"] = nowa_tresc
        zmienione.append(f"{b['label']} ({_liczba_zmienionych_linii(stare[b['id']], b['text'])} zm. linii)")
        for _d59 in _globale_z_odpowiedzi(odp2):
            _m59 = re.match(r"(?:let|var|const)\s+([A-Za-z_$][\w$]*)", _d59)
            if _m59 and not any(
                    re.match(r"(?:let|var|const)\s+" + re.escape(_m59.group(1)) + r"\b", _g59)
                    for _g59 in globale) \
                    and not _nazwa_zadeklarowana(fname, content, _m59.group(1)):
                globale.append(_d59)
                print(f"EDYCJA_W_KAWALKACH: model zgłosił nową deklarację globalną: {_d59}")
        print(f"EDYCJA_W_KAWALKACH: podmieniono blok {b['id']} ({b['label']}) w pliku {fname}")
        if job_id:
            _edit_checkpoint(job_id, chosen, stare, n, zmienione, globale=globale)
    nowa = "".join(b["text"] for b in blocks)
    # SPOJNOSC_V59: deklaracje globalne zgloszone przez model kanalami
    # [NOWY_GLOBAL] (albo odzyskane ze wznowienia) wchodza do zlozonego
    # pliku PRZED walidacja – bramka widzi je jako zwykle deklaracje.
    if globale:
        _nowa59 = _wstaw_globale(fname, nowa, globale)
        if _nowa59 != nowa:
            nowa = _nowa59
            zmienione.append("dopisano deklaracje globalne: " + ", ".join(
                re.match(r"(?:let|var|const)\s+([A-Za-z_$][\w$]*)", _g59).group(1)
                for _g59 in globale))
            print(f"EDYCJA_W_KAWALKACH: dopisano {len(globale)} deklaracji "
                  f"globalnych w {fname}")
    # BRAMA_KODU_V33: walidacja złożonego pliku (tylko NOWE problemy nas obchodzą)
    if (fname or "").lower().endswith((".html", ".htm", ".js", ".mjs")):
        klucze0 = {p["klucz"] for p in _waliduj_kod(fname, content)}
        problemy = [p for p in _waliduj_kod(fname, nowa) if p["klucz"] not in klucze0]
        if problemy:
            print(f"BRAMA_KODU: {fname} po edycji ma {len(problemy)} nowych problemów – próbuję naprawy")
            _chat_job_set_stage(rid, "Brama kodu: sprawdzam poprawność kodu po edycji…")
            if _napraw_bloki_kodu(api_key, fname, blocks,
                                  {b["id"] for b in chosen}, problemy, rid=rid):
                nowa = "".join(b["text"] for b in blocks)
                problemy = [p for p in _waliduj_kod(fname, nowa)
                            if p["klucz"] not in klucze0]
        # DEKLARACJE_V58: dogranie brakujących deklaracji zmiennych
        _pewne58 = [p for p in problemy if p.get("pewny", True)]
        _braki58 = [p["klucz"].split(":", 1)[1] for p in _pewne58
                    if p.get("typ") == "zmienna"
                    and (p.get("klucz") or "").startswith("zmienna:")]
        if _braki58 and len(_braki58) == len(_pewne58):
            _chat_job_set_stage(rid, "Brama kodu: dopisuję brakujące deklaracje zmiennych…")
            _uzup58 = _bramka_dopisz_definicje(api_key, fname, nowa, _braki58)
            if _uzup58 and _uzup58 != nowa:
                _prob58 = [p for p in _waliduj_kod(fname, _uzup58)
                           if p["klucz"] not in klucze0]
                if not [p for p in _prob58 if p.get("pewny", True)]:
                    nowa = _uzup58
                    problemy = _prob58
                    zmienione.append("dopisano brakujące deklaracje: "
                                     + ", ".join(_braki58))
                    print(f"BRAMA_KODU_V58: dopisano deklaracje "
                          f"{', '.join(_braki58)} w {fname}")
        # SPOJNOSC_V59: gdy zostaja TYLKO cienie stanu wspoldzielonego,
        # rozwiazujemy je deterministycznie zamiast odrzucac edycje:
        # lokalne deklaracje znikaja (albo staja sie przypisaniami),
        # a na poczatek skryptu trafia jedna wspolna deklaracja.
        _pewne59e = [p for p in problemy if p.get("pewny", True)]
        _cienie59e = [p for p in _pewne59e if p.get("typ") == "cien"]
        if _cienie59e and len(_cienie59e) == len(_pewne59e):
            _nazwy59e = [p["klucz"].split(":", 1)[1] for p in _cienie59e]
            _napr59e, _inic59e = _usun_cienie(nowa, _nazwy59e)
            if _inic59e and all(n in _inic59e for n in _nazwy59e):
                _kand59e = _wstaw_globale(
                    fname, _napr59e,
                    [f"let {n} = {_inic59e[n]};" for n in _nazwy59e])
                _prob59e = [p for p in _waliduj_kod(fname, _kand59e)
                            if p["klucz"] not in klucze0]
                if not [p for p in _prob59e if p.get("pewny", True)]:
                    nowa = _kand59e
                    problemy = _prob59e
                    zmienione.append("naprawiono współdzielony stan: "
                                     + ", ".join(_inic59e))
                    print(f"BRAMA_KODU_V59: rozwiązano cienie zmiennych "
                          f"{', '.join(_inic59e)} w {fname}")
        pewne = [p for p in problemy if p.get("pewny", True)]
        if pewne:
            info = "; ".join(p["opis"] for p in pewne[:3])
            print(f"BRAMA_KODU: edycja {fname} ODRZUCONA: {info}")
            return content, [], info
        if problemy:
            zmienione.append("⚠ uproszczona kontrola kodu zgłasza możliwe problemy: "
                             + "; ".join(p["opis"] for p in problemy[:2]))
    return nowa, zmienione, None


@app.post("/chat")
def chat(r: ChatRequest, resp: Response, user: str = Depends(get_current_user)):
    global current_key_index
    _chat_job_register(r.request_id)  # chmurka oczekiwania może tym zadaniem sterować (pauza/stop)
    if _sygnal_pracy_plikowej(r.history):  # PANEL_WCZESNIEJ_V56: panel procesu od początku, nie po fakcie
        _chat_job_set_stage(r.request_id, "Przygotowuję pracę nad plikiem…")
    prompt_parts = []
    for i in [x for x in r.history if not x.isHidden]:
        _content = i.content
        if _content and i.role in ("model", "assistant"):
            _content = _trim_file_blocks(_content)  # AKUMULATOR_V3: odchudzanie historii
        parts = [_content] if _content else []
        if i.attachments:
            for a in i.attachments:
                # AKUMULATOR_V4 (OSZCZEDNY): części call.html i sam call.html NIE idą
                # do modelu – backend dokleja je do buildu z surowych załączników
                # (_akum_user_call_html), więc model dostaje tylko krótką notkę.
                # Bez tego jedno zapytanie ważyło ~33 tys. tokenów i darmowe
                # klucze (np. Groq, limit 8000/min) odrzucały je w całości (413).
                if _akum_is_asset_attachment(getattr(a, "name", "")):
                    parts.append(f"[załącznik: {a.name} – treść pominięta dla oszczędności tokenów; "
                                 "backend dołączy ten plik do buildu co do bajta]")
                else:
                    parts.append(process_att(a))
        prompt_parts.append({'role': i.role, 'parts': parts})

    # --- EDYCJA_PLIKU_V9: gdy użytkownik prosi o poprawkę w istniejącym pliku,
    #     doklejamy modelowi jego AKTUALNĄ treść (inaczej napisze nowy od zera) ---
    try:
        _last_user_txt = ""
        for _it in reversed(r.history):
            if _it.role == "user" and not _it.isHidden:
                _last_user_txt = _it.content or ""
                break
        if _last_user_txt and _czy_edycja_kontekst(r.history, _last_user_txt):
            _found = _znajdz_aktualny_plik(r.history)
            if _found:
                _fname, _fcontent = _found
                # Duży plik: edycja w kawałkach działa W PEŁNI W TLE, jak
                # generowanie plików (EDYCJA_W_TLE_V35) – rekord zadania i jego
                # migawka w Boxie powstają od razu, worker na serwerze robi
                # edycję niezależnie od okna przeglądarki, a karta w rozmowie
                # pokazuje postęp, wstrzymuje przy braku kluczy i po zakończeniu
                # daje pobranie gotowego pliku (albo oryginału, gdy bramka
                # kodu edycję odrzuci).
                if _fcontent and len(_fcontent) > 14_000:
                    _edit_jid = _edit_job_new(_fname, _last_user_txt, _fcontent,
                                              r.apiKey, base_msgs=prompt_parts)
                    _file_job_save(_edit_jid)
                    threading.Thread(target=_edit_job_worker, args=(_edit_jid,),
                                     daemon=True).start()
                    _payload = {"job_id": _edit_jid, "filename": _fname, "estimate": None}
                    return {
                        # KARTA_BEZ_OPISU_V40: sama karta wystarczy – bez
                        # opisowego tekstu obok karty zadania.
                        "generated_text": "",
                        "files": [make_file_response(
                            f"filejob_{_edit_jid}.job",
                            json.dumps(_payload, ensure_ascii=False),
                            "application/x-file-job")],
                    }
                if _fcontent and len(_fcontent) <= 150_000:
                    prompt_parts.append({'role': 'user', 'parts': [
                        "[SYSTEM – POPRAWKA W ISTNIEJĄCYM PLIKU] Użytkownik prosi o zmianę w pliku "
                        + _fname + ", który powstał wcześniej w tej rozmowie. NIE twórz nowej wersji "
                        "od zera ani uproszczonej. Weź poniższą AKTUALNĄ treść pliku, wprowadź TYLKO "
                        "zmiany, o które prosi użytkownik (cała reszta – grafika, mechanika, jakość – "
                        "zostaje bez zmian), i wyślij CAŁY zaktualizowany plik jako [FILE: " + _fname + "] "
                        "pod tą samą nazwą.\n<<<AKTUALNY PLIK " + _fname + "\n" + _fcontent + "\n>>>"
                    ]})
                    print(f"EDYCJA_PLIKU_V9: doklejono aktualną treść {_fname} ({len(_fcontent)} znaków) do kontekstu edycji")
    except Exception as _ee:
        print(f"EDYCJA_PLIKU_V9: pominięto ({_ee})")

    loops = 0
    while loops < 3:
        loops += 1
        keys = ([r.apiKey] if r.apiKey else []) + api_keys[current_key_index:] + api_keys[:current_key_index]
        ai_text = None
        for idx, k in enumerate(keys):
            if idx > 15: break
            try:
                ai_text = call_llm(k, prompt_parts, SYSTEM_INSTRUCTION + ((STRAZNIK_PROMPT + _straznik_kontekst_wybranych(r)) if r.straznik else ""))
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
            _chat_job_set_stage(r.request_id, "Generuję obraz…")  # OBRAZ_PRACA_V63
            try:
                p = re.search(r'prompt="([^"]*)"', ai_text).group(1)
                return {"image_file": {"filename":"gen.png", "data": base64.b64encode(generate_image_from_prompt(p)).decode()}, "generated_text": user_msg or "Obraz:"}
            except: return {"generated_text": "Błąd gen."}
        elif "[EDIT_IMAGE:" in ai_text:
            _chat_job_set_stage(r.request_id, "Generuję obraz…")  # OBRAZ_PRACA_V63
            try:
                att = next((a for i in reversed(r.history) if i.attachments for a in i.attachments if "image" in a.mime_type), None)
                if not att: return {"generated_text": "Brak obrazu."}
                return {"image_file": {"filename":"edit.png", "data": base64.b64encode(edit_image(att.data, ai_text)).decode()}, "generated_text": user_msg or "Zrobione."}
            except: return {"generated_text": "Błąd edycji."}
        elif "[CHANGE_BACKGROUND:" in ai_text:
            _chat_job_set_stage(r.request_id, "Generuję obraz…")  # OBRAZ_PRACA_V63
            try:
                att = next((a for i in reversed(r.history) if i.attachments for a in i.attachments if "image" in a.mime_type), None)
                if not att: return {"generated_text": "Brak obrazu."}
                fg = remove(base64.b64decode(att.data))
                bg = generate_image_from_prompt(re.search(r'background_prompt="([^"]*)"', ai_text).group(1))
                return {"image_file": {"filename":"bg.png", "data": base64.b64encode(composite_images(fg, bg)).decode()}, "generated_text": user_msg or "Tło zmienione."}
            except: return {"generated_text": "Błąd tła."}
        elif "[INPAINT_IMAGE:" in ai_text:
            _chat_job_set_stage(r.request_id, "Generuję obraz…")  # OBRAZ_PRACA_V63
            try:
                att = next((a for i in reversed(r.history) if i.attachments for a in i.attachments if "image" in a.mime_type), None)
                return {"image_file": {"filename":"inp.png", "data": base64.b64encode(inpaint_image(att.data, re.search(r'prompt="([^"]*)"', ai_text).group(1))).decode()}, "generated_text": user_msg or "Usunięto."}
            except: return {"generated_text": "Błąd inpaint."}
        elif "[CREATE_COMPOSITION:" in ai_text:
            _chat_job_set_stage(r.request_id, "Generuję obraz…")  # OBRAZ_PRACA_V63
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

        # --- AUTOBUILD_V6 + PLIK_NA_RATY_V7: dokańczanie bez zatrzymywania się ---
        # Backend sam dopytuje model w ramach TEGO zapytania, zamiast zmuszać
        # użytkownika do poganiania agenta. Każda kontynuacja to osobne wywołanie
        # LLM (osobny limit tokenów na część), a klucze rotują przy błędach
        # i między częściami – jedno zadanie może więc zużyć wielokrotnie więcej
        # tokenów, niż mieści jedna odpowiedź, i trwać tak długo, aż plik będzie
        # kompletny albo użytkownik przerwie czekanie przyciskiem stop (■).
        # Dwa przypadki:
        #  A) projekt (pliki .gradle/.csproj/.cpp/...) bez tagu buildu – jak dawniej;
        #  B) plik [FILE:] rozpoczęty, ale NIEDOMKNIĘTY (odpowiedź ucięta na limicie
        #     tokenów) – model dopisuje dalszy ciąg dokładnie od miejsca urwania,
        #     aż zamknie [/FILE]. Gdyby częściowy wynik i tak wrócił do użytkownika
        #     (limit części/rozmiaru), parser wyciągnie go jako plik z ostrzeżeniem.
        # Zabezpieczenia: maks. 24 kontynuacje i maks. ~400 tys. znaków całości.
        ai_full = ai_text
        _file_tag_re = re.compile(r"\[FILE:\s*([^\]]+?)\]", re.IGNORECASE)
        _file_close_re = re.compile(r"\[/FILE\]", re.IGNORECASE)
        def _pliki_projektu(txt):
            nazwy = {m.group(1).strip() for m in _file_tag_re.finditer(txt)}
            def _projektowa(n):
                return (n.endswith((".gradle", ".csproj", ".sln", ".cpp", ".h", ".hpp", ".java",
                                    ".kt", ".nsi", "CMakeLists.txt", "AndroidManifest.xml")))
            return nazwy, any(_projektowa(n) for n in nazwy)
        def _niedomkniety_plik(txt):
            opens = [m.group(1).strip() for m in _file_tag_re.finditer(txt)]
            if len(opens) > len(_file_close_re.findall(txt)):
                return opens[-1]
            return None

        # --- PLIK_W_TLE_V8: duży plik (niedomknięty, niebędący projektem do buildu)
        #     leci do zadania w tle z kartą postępu, a rozmowa wraca do użytkownika
        #     od razu – może dalej pisać, a plik dokończy się w tle. ---
        _niedomkniety_start = _niedomkniety_plik(ai_full)
        _nazwy_start, _czy_projekt_start = _pliki_projektu(ai_text)
        if (_niedomkniety_start and not _czy_projekt_start
                and "[GH_BUILD" not in ai_full and "[NSIS_INSTALLER" not in ai_full):
            _opens = list(_file_tag_re.finditer(ai_full))
            _m_open = _opens[-1]
            _first_content = ai_full[_m_open.end():]
            _first_content = re.sub(r'^\s*```[\w]*\n?', '', _first_content)
            _intro = re.sub(r'\[SZACUJĘ:[^\]]*\]', '', ai_full[:_m_open.start()]).strip()
            _est_m = re.search(r"\[SZACUJĘ:\s*~?\s*(\d+)", ai_full)
            _estimate = int(_est_m.group(1)) if _est_m else None
            _job_id = create_file_job(
                filename=_niedomkniety_start,
                first_content=_first_content,
                base_msgs=prompt_parts,
                last_raw=ai_text,
                api_key=r.apiKey,
                estimate=_estimate,
            )
            _job_payload = {"job_id": _job_id, "filename": _niedomkniety_start, "estimate": _estimate}
            return {
                # KARTA_BEZ_OPISU_V40: gdy model nic nie napisał przed
                # tagiem pliku, nie dokładamy opisu – sama karta wystarczy.
                "generated_text": _intro,
                "files": [make_file_response(
                    f"filejob_{_job_id}.job",
                    json.dumps(_job_payload, ensure_ascii=False),
                    "application/x-file-job",
                )],
            }

        _MAX_CONT = 24
        _MAX_FULL_CHARS = 400_000
        _cont = 0
        _chat_stopped = False
        while _cont < _MAX_CONT:
            if "[GH_BUILD" in ai_full or "[NSIS_INSTALLER" in ai_full:
                break
            if len(ai_full) >= _MAX_FULL_CHARS:
                print("PLIK_NA_RATY_V7: limit rozmiaru całości – koniec kontynuacji")
                break
            _niedomkniety = _niedomkniety_plik(ai_full)
            _nazwy, _czy_projekt = _pliki_projektu(ai_text)  # tylko najnowsza partia: sam tekst = model skonczyl
            if not _niedomkniety and not _czy_projekt:
                break
            # Sterowanie z chmurki: pauza czeka między częściami, stop przerywa pętlę
            _chat_job_set_stage(
                r.request_id,
                (f"Dopisuję dalszy ciąg pliku {_niedomkniety} (część {_cont + 2})…" if _niedomkniety
                 else "Zbieram kolejne pliki projektu…"),
                parts=_cont + 1)
            if not _chat_job_gate(r.request_id):
                _chat_stopped = True
                print("CHAT_JOB: użytkownik zatrzymał zadanie z chmurki – przerywam doklejanie")
                break
            prompt_parts.append({'role': 'model', 'parts': [ai_text]})
            if _niedomkniety:
                _tail = ai_full[-220:].replace("\n", " ")
                _instr = (
                    "[SYSTEM] Twój plik " + _niedomkniety + " został ucięty w połowie "
                    "(limit jednej odpowiedzi). Kontynuuj automatycznie, bez pytania "
                    "użytkownika i bez żadnego komentarza. Wyślij TYLKO dalszy ciąg treści "
                    "pliku " + _niedomkniety + ", dokładnie od miejsca, w którym urwałeś. "
                    "Ostatnie znaki, które już wysłałeś: „…" + _tail + "” – NIE powtarzaj ich "
                    "ani niczego, co już jest w pliku. Nie dodawaj tagu [FILE:] ani "
                    "znaczników ```. Gdy plik będzie kompletny w całości, zakończ go "
                    "tagiem [/FILE] i niczym więcej."
                )
            else:
                _instr = (
                    "[SYSTEM] Kontynuuj automatycznie, bez zatrzymywania się i bez pytania "
                    "użytkownika. Jeśli projekt jest kompletny, wyślij teraz TYLKO tag "
                    "zlecenia buildu (np. [GH_BUILD: android] [/GH_BUILD]). Jeśli brakuje "
                    "plików, wyślij kolejne 1–2 pliki [FILE:] tego samego projektu. "
                    "Bez podsumowań i bez list postępu."
                )
            prompt_parts.append({'role': 'user', 'parts': [_instr]})
            # Rotacja kluczy: każda część zaczyna próby od bieżącego klucza w puli,
            # a przy błędzie/limicie przechodzi na kolejne (jak w głównej pętli).
            _keys_try = ([r.apiKey] if r.apiKey else []) + api_keys[current_key_index:] + api_keys[:current_key_index]
            _nxt = None
            for _idx2, _k2 in enumerate(_keys_try):
                if _idx2 > 15: break
                try:
                    _nxt = call_llm(_k2, prompt_parts, SYSTEM_INSTRUCTION)
                    if _k2 in api_keys: current_key_index = api_keys.index(_k2)
                    break
                except Exception as _e:
                    print(f"Key fail: {_e}")
            if not _nxt:
                break
            if _niedomkniety:
                # Kontynuacja pliku: doklejamy BEZ separatora (urwanie bywa w pół
                # słowa), po oczyszczeniu początku z ewentualnego tagu [FILE:]
                # albo ogrodzenia ```, gdyby model dodał je mimo zakazu.
                _cont_txt = _nxt.strip()
                _cont_txt = re.sub(r'^\[FILE:\s*[^\]]+?\]\s*', '', _cont_txt, flags=re.IGNORECASE)
                _cont_txt = re.sub(r'^```[\w]*\s*\n?', '', _cont_txt)
                ai_text = _cont_txt
                ai_full += _cont_txt
                print(f"PLIK_NA_RATY_V7: doklejono część {_cont + 1} pliku {_niedomkniety} "
                      f"(+{len(_cont_txt)} znaków, razem {len(ai_full)})")
            else:
                ai_text = _nxt
                ai_full += "\n" + ai_text
                print(f"AUTOBUILD_V6: automatyczna kontynuacja nr {_cont + 1}")
            _cont += 1
        # awaryjnie: kompletny projekt Androida bez tagu -> dopisujemy zlecenie sami
        _nazwy_all, _ = _pliki_projektu(ai_full)
        if "[GH_BUILD" not in ai_full and any("AndroidManifest.xml" in n for n in _nazwy_all):
            ai_full += "\n[GH_BUILD: android]\n[/GH_BUILD]"
            print("AUTOBUILD_V6: dopisano brakujący tag [GH_BUILD: android]")

        # --- NOWE: pliki kodu / docx / pdf z tagów ---
        clean_text, generated_files = _extract_and_build_files(ai_full, r.history, rid=r.request_id)
        if r.straznik:
            clean_text = _straznik_wykonaj_tagi(clean_text, r.straznik_urzadzenie or "")
        if _chat_stopped:
            clean_text = ((clean_text or "") +
                          "\n\n⏹ Zatrzymałeś to zadanie z chmurki, więc wynik powyżej jest częściowy. "
                          "Napisz „dokończ”, a dopiszę resztę.").strip()

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
