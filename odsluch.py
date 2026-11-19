"""Odsłuch stream — odtwarzacz strumieni WAV (HTTP) z nasłuchu przemienników: lista strumieni, osobne okno
na każdy strumień, dekoder CW, CTCSS, wycinanie ogona, odszumianie RNNoise."""
import atexit
import collections
import ctypes
import faulthandler
import http.client
import json
import os
import queue
import re
import struct
import sys
import threading
import time
import tkinter as tk
import traceback
import types
import urllib.parse
import urllib.request
import uuid
from tkinter import messagebox, ttk

import numpy as np
import sounddevice as sd

TYTUL = "Odsłuch stream"
try:
    from domyslne import DOMYSLNY_URL, DOMYSLNA_NAZWA     # prywatne (klucz); brak w wersji „czystej”
except ImportError:
    DOMYSLNY_URL, DOMYSLNA_NAZWA = "", ""
KATALOG_CFG = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Odsluch_stream")
PLIK_CFG = os.path.join(KATALOG_CFG, "config.json")
PLIK_DZIENNIKA = os.path.join(KATALOG_CFG, "dziennik.log")
MAX_DZIENNIK_B = 1_000_000      # większy dziennik przy starcie przechodzi do dziennik.1.log
ZNAK_ZYCIA_S = 15 * 60          # co ile wpis „działa: …” (widać, kiedy program zniknął)

PREBUFOR_S = 0.5        # tyle dźwięku zbieramy przed startem odtwarzania
MAX_BUFOR_S = 3.0       # powyżej tego wyrzucamy nadmiar (dryf zegarów serwer/karta)
PROG_AKTYWNOSCI_DB = -55.0
KONIEC_NADAWANIA_S = 2.0
PONOW_PO_S = 3.0
MAX_NAGRANIE_S = 300    # dłuższe nadawanie dekodujemy tylko z pierwszych 5 minut (nagranie trzymane w mono)
DOMYSLNE_WYJSCIE = "Domyślne"
AGENT = "Odsluch-stream/1.0"
STRAZNIK_AUDIO_S = 2    # co ile sekund sprawdzać wyjście dźwięku Windows
STRUMIEN_STOI_S = 3.0   # karta nie bierze dźwięku dłużej niż tyle = strumień „stoi” (urządzenie znikło)

CW_MIN_CZYSTOSC = 0.7   # telegrafia z nadajnika: ton czysty (beacony 0,99–1,00), muzyka/mowa ~0,2–0,64
CW_MAX_ROZRZUT = 0.35   # znaki o równej głośności (beacony 0,00–0,17)
CW_SLABY_MIN_CZYSTOSC = 0.45   # słaby, zaszumiony beacon: ton mniej czysty (do ~0,47), ale znaki równe…
CW_SLABY_MAX_ROZRZUT = 0.1     # …rozrzut ≤ 0,04; muzyka PR2 miała rozrzut ≥ 0,21

MORSE = {
    ".-": "A", "-...": "B", "-.-.": "C", "-..": "D", ".": "E", "..-.": "F", "--.": "G", "....": "H",
    "..": "I", ".---": "J", "-.-": "K", ".-..": "L", "--": "M", "-.": "N", "---": "O", ".--.": "P",
    "--.-": "Q", ".-.": "R", "...": "S", "-": "T", "..-": "U", "...-": "V", ".--": "W", "-..-": "X",
    "-.--": "Y", "--..": "Z", "-----": "0", ".----": "1", "..---": "2", "...--": "3", "....-": "4",
    ".....": "5", "-....": "6", "--...": "7", "---..": "8", "----.": "9", "-..-.": "/", "..--..": "?",
    ".-.-.-": ".", "--..--": ",", "-...-": "=", ".-.-.": "+", "-....-": "-",
}


CTCSS = (67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5, 94.8, 97.4, 100.0, 103.5, 107.2,
         110.9, 114.8, 118.8, 123.0, 127.3, 131.8, 136.5, 141.3, 146.2, 151.4, 156.7, 159.8, 162.2, 165.5,
         167.9, 171.3, 173.8, 177.3, 179.9, 183.5, 186.2, 189.9, 192.8, 196.6, 199.5, 203.5, 206.5, 210.7,
         218.1, 225.7, 229.1, 233.6, 241.8, 250.3, 254.1)


_blokada_dziennika = threading.Lock()
_plik_awarii = None


def dziennik(tekst):
    """Linia w dzienniku programu (start, koniec, okna, przerwy, przełączenia dźwięku, błędy). Bez kluczy z adresów."""
    tekst = re.sub(r"(klucz=)[^&\s'\"]+", r"\1***", str(tekst))
    try:
        with _blokada_dziennika, open(PLIK_DZIENNIKA, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {tekst}\n")
    except OSError:
        pass


def wlacz_dziennik():
    """Dziennik + zapis awarii: wyjątki z każdego wątku i twarde awarie bibliotek (faulthandler, wszystkie wątki)."""
    global _plik_awarii
    try:
        os.makedirs(KATALOG_CFG, exist_ok=True)
        if os.path.exists(PLIK_DZIENNIKA) and os.path.getsize(PLIK_DZIENNIKA) > MAX_DZIENNIK_B:
            os.replace(PLIK_DZIENNIKA, PLIK_DZIENNIKA[:-4] + ".1.log")
        _plik_awarii = open(PLIK_DZIENNIKA, "a", encoding="utf-8")
        faulthandler.enable(file=_plik_awarii, all_threads=True)
    except OSError:
        pass
    sys.excepthook = lambda typ, wart, tb: dziennik(
        "BŁĄD (wątek główny):\n" + "".join(traceback.format_exception(typ, wart, tb)).rstrip())
    threading.excepthook = lambda a: dziennik(
        f"BŁĄD (wątek {a.thread.name if a.thread else '?'}):\n"
        + "".join(traceback.format_exception(a.exc_type, a.exc_value, a.exc_traceback)).rstrip())
    atexit.register(lambda: dziennik("koniec procesu"))


def pamiec_mb():
    """Prywatna pamięć procesu w RAM (MB) — do znaku życia w dzienniku."""
    class Pmc(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_uint32), ("PageFaultCount", ctypes.c_uint32)] + [(n, ctypes.c_size_t) for n in (
            "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
            "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage",
            "PrivateUsage", "PrivateWorkingSetSize", "SharedCommitUsage")]
    try:
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.K32GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Pmc), ctypes.c_uint32]
        m = Pmc()
        m.cb = ctypes.sizeof(Pmc)
        k32.K32GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(m), m.cb)
        return round(m.PrivateWorkingSetSize / 2 ** 20)
    except Exception:
        return -1


def wczytaj_cfg():
    try:
        with open(PLIK_CFG, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def zapisz_cfg(cfg):
    try:
        os.makedirs(KATALOG_CFG, exist_ok=True)
        with open(PLIK_CFG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


BLOKADA_AUDIO = threading.RLock()     # otwieranie/zamykanie strumieni PortAudio i jego odświeżanie (wszystkie okna)


class _WAVEOUTCAPSW(ctypes.Structure):
    _fields_ = [("wMid", ctypes.c_ushort), ("wPid", ctypes.c_ushort), ("vDriverVersion", ctypes.c_uint),
                ("szPname", ctypes.c_wchar * 32), ("dwFormats", ctypes.c_uint), ("wChannels", ctypes.c_ushort),
                ("wReserved1", ctypes.c_ushort), ("dwSupport", ctypes.c_uint)]


def podpis_audio():
    """(domyślne wyjście Windows, lista wyjść) czytane na żywo z MME — PortAudio pamięta stan z uruchomienia,
    więc np. po włączeniu Pulpitu zdalnego („Dźwięk zdalny”) nie widzi zmiany."""
    winmm = ctypes.windll.winmm
    winmm.waveOutMessage.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]

    def nazwa(i):
        caps = _WAVEOUTCAPSW()
        return caps.szPname if winmm.waveOutGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) == 0 else None

    domyslne, flagi = ctypes.c_uint(0xFFFFFFFF), ctypes.c_uint(0)
    winmm.waveOutMessage(ctypes.c_void_p(0xFFFFFFFF), 0x2015,          # WAVE_MAPPER, DRVM_MAPPER_PREFERRED_GET
                         ctypes.byref(domyslne), ctypes.byref(flagi))
    return nazwa(domyslne.value), tuple(nazwa(i) for i in range(winmm.waveOutGetNumDevs()))


def wyjscia_audio():
    """Nazwa -> indeks urządzenia wyjściowego (tylko MME, żeby nie dublować tych samych kart)."""
    wynik = {DOMYSLNE_WYJSCIE: None}
    try:
        mme = next((i for i, h in enumerate(sd.query_hostapis()) if h["name"] == "MME"), None)
        for i, d in enumerate(sd.query_devices()):
            if d["max_output_channels"] > 0 and (mme is None or d["hostapi"] == mme):
                wynik.setdefault(d["name"], i)
    except Exception:
        pass
    return wynik


def godzina(t):
    return time.strftime("%H:%M:%S", time.localtime(t))


def wykryj_ctcss(x, fs):
    """Ton CTCSS (Hz, z listy EIA) obecny w nagraniu albo None; analizuje najwyżej 10 s."""
    if len(x) < fs:
        return None
    x = x[: 10 * fs]
    nfft = 1 << max(18, (len(x) - 1).bit_length())     # zero-padding: prążki co ≤ 0,2 Hz
    widmo = np.abs(np.fft.rfft(x * np.hanning(len(x)), nfft))
    czest = np.fft.rfftfreq(nfft, 1 / fs)
    pasmo = np.flatnonzero((czest >= 60) & (czest <= 260))
    i = pasmo[np.argmax(widmo[pasmo])]
    a, b, c = widmo[i - 1], widmo[i], widmo[i + 1]
    if b <= 0 or b < 20 * np.median(widmo[pasmo]) or a - 2 * b + c >= 0:
        return None
    f = czest[i] + 0.5 * (a - c) / (a - 2 * b + c) * (czest[1] - czest[0])
    ton = min(CTCSS, key=lambda t: abs(t - f))
    return ton if abs(ton - f) < 1.0 else None


def opis_tonu(ton):
    return f"{ton:.1f} Hz".replace(".", ",")


def ogon(bloki, n):
    """Ostatnie n próbek z listy bloków (bez sklejania całego nagrania)."""
    wynik, ile = [], 0
    for b in reversed(bloki):
        wynik.append(b)
        ile += len(b)
        if ile >= n:
            break
    return np.concatenate(wynik[::-1])[-n:]


class FiltrCTCSS:
    """Górnoprzepustowy FIR (okno Kaisera, 60 dB): wycina podnośną CTCSS (67-254 Hz), głos od 300 Hz zostaje.
    Splot przez FFT (overlap-save), opóźnienie ~45 ms przy 48 kHz."""

    def __init__(self, fs, kanaly):
        tlumienie, przejscie, fc = 60.0, 40.0, 280.0
        n = int(np.ceil((tlumienie - 8) / (2.285 * 2 * np.pi * przejscie / fs))) | 1
        m = np.arange(n) - (n - 1) / 2
        h = -2 * fc / fs * np.sinc(2 * fc / fs * m) * np.kaiser(n, 0.1102 * (tlumienie - 8.7))
        h[(n - 1) // 2] += 1.0
        self.h = h
        self.historia = np.zeros((n - 1, kanaly), np.float32)
        self.widma = {}

    def __call__(self, x):
        buf = np.concatenate([self.historia, x])
        nfft = 1 << (len(buf) - 1).bit_length()
        H = self.widma.get(nfft)
        if H is None:
            H = self.widma[nfft] = np.fft.rfft(self.h, nfft)[:, None]
        y = np.fft.irfft(np.fft.rfft(buf, nfft, axis=0) * H, nfft, axis=0)[len(self.h) - 1:len(buf)]
        self.historia = buf[len(buf) - len(self.historia):]
        return y.astype(np.float32)


_RNNOISE = None


def sciezka_zasobu(*czesci):
    """Plik dołączony do programu — obok skryptu albo w rozpakowanym exe (PyInstaller)."""
    return os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), *czesci)


def _biblioteka_rnnoise():
    """rnnoise.dll (Xiph RNNoise 0.2, z paczki pyrnnoise) — obok skryptu albo w rozpakowanym exe."""
    global _RNNOISE
    if _RNNOISE is None:
        lib = ctypes.CDLL(sciezka_zasobu("rnnoise", "rnnoise.dll"))
        lib.rnnoise_create.argtypes = [ctypes.c_void_p]
        lib.rnnoise_create.restype = ctypes.c_void_p
        lib.rnnoise_destroy.argtypes = [ctypes.c_void_p]
        lib.rnnoise_process_frame.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float),
                                              ctypes.POINTER(ctypes.c_float)]
        lib.rnnoise_process_frame.restype = ctypes.c_float
        lib.rnnoise_get_frame_size.restype = ctypes.c_int
        _RNNOISE = lib
    return _RNNOISE


class Odszumianie:
    """RNNoise (ramki 10 ms przy 48 kHz, osobny stan na kanał) zmieszany z oryginałem w proporcji `sila`.
    RNNoise wycina telegrafię i bipy jak szum, więc przy stałym tonie bez mowy płynnie przepuszcza oryginał;
    decyzja zapada z wyprzedzeniem WYPRZEDZENIE_S, żeby nie zgubić pierwszego znaku.
    Zwraca tyle próbek, ile pełnych ramek się uzbierało (reszta czeka na następny blok)."""

    OPOZNIENIE_RNN = 960            # zmierzone opóźnienie RNNoise 0.2 przy 48 kHz (20 ms)
    WYPRZEDZENIE_S = 0.45           # > czas wykrycia tonu (do ~250 ms przy kropkach) + zapas przed tonem
    ZAPAS_PRZED_TONEM_S = 0.15
    TRZYMANIE_TONU_S = 1.5
    PROG_MOWY = 0.5                 # średnie prawdopodobieństwo mowy (RNNoise, 200 ms), powyżej = mowa, nie ton
    KROK_MIESZANIA = 0.2            # zmiana proporcji na ramkę -> pełne przejście w 50 ms

    def __init__(self, kanaly, fs):
        self.lib = _biblioteka_rnnoise()
        self.fs = fs
        self.ramka = self.lib.rnnoise_get_frame_size()
        self.stany = [self.lib.rnnoise_create(None) for _ in range(kanaly)]
        self.reszta = np.zeros((0, kanaly), np.float32)
        self.oryginal = np.zeros((self.OPOZNIENIE_RNN, kanaly), np.float32)   # oryginał wyrównany z RNNoise
        self.kolejka = collections.deque()       # (odszumione, oryginał, nr ramki) czekające na decyzję
        self._we = np.zeros(self.ramka, np.float32)
        self._wy = np.zeros(self.ramka, np.float32)
        self.sila = 1.0
        self.mieszanie = 1.0
        self.mowa = 0.0                          # prawdopodobieństwo mowy (RNNoise) w ostatniej ramce
        self._vad = collections.deque(maxlen=20)
        self._piki = collections.deque(maxlen=10)    # (częstotliwość tonu albo None, początek bloku)
        self._ramka_nr, self._probki = 0, 0
        self._ton_od, self._ton_do = -1, -1       # zakres próbek wejścia, w którym przepuszczać oryginał
        self.ton = False
        okno = 2048
        self._okno = np.hanning(okno)
        self._do_analizy = np.zeros(0, np.float32)   # bloki przychodzą w różnych długościach
        self._czest = np.fft.rfftfreq(4 * okno, 1 / fs)

    def __del__(self):
        for s in getattr(self, "stany", []):
            self.lib.rnnoise_destroy(s)

    def _wykryj_ton(self, x):
        """Ten sam ton 300-2000 Hz w ≥ 3 z ostatnich 10 bloków (~430 ms) przy braku mowy = telegrafia / bip."""
        n = len(self._okno)
        poczatek_bufora = self._probki - len(self._do_analizy)
        x = self._do_analizy = np.concatenate([self._do_analizy, x])
        pelne = len(x) // n * n
        self._do_analizy = x[pelne:]
        for i in range(0, pelne, n):
            widmo = np.abs(np.fft.rfft(x[i:i + n] * self._okno, 4 * n))
            pasmo = (self._czest >= 300) & (self._czest <= 2000)
            j = np.argmax(widmo[pasmo])
            pik = self._czest[pasmo][j]
            r = widmo[pasmo][j] / (np.median(widmo[(self._czest >= 300) & (self._czest <= 3000)]) + 1e-12)
            start = poczatek_bufora + i
            self._piki.append((pik if r > 25 else None, start))
            if r <= 25:
                continue
            zgodne = [s for p, s in self._piki if p is not None and abs(p - pik) < 15]
            mowa = np.mean(self._vad) if self._vad else 0.0
            if len(zgodne) >= 3 and mowa < self.PROG_MOWY:
                poczatek = zgodne[0] - int(self.ZAPAS_PRZED_TONEM_S * self.fs)
                if poczatek > self._ton_do:                         # nowy fragment z tonem
                    self._ton_od = poczatek
                self._ton_do = start + n + int(self.TRZYMANIE_TONU_S * self.fs)

    def __call__(self, x):
        self._wykryj_ton(x.mean(axis=1))
        self._probki += len(x)
        buf = np.concatenate([self.reszta, x])
        n = len(buf) // self.ramka * self.ramka
        pelny = np.concatenate([self.oryginal, buf[:n]])
        self.oryginal = pelny[n:]
        we = self._we.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        wy = self._wy.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        for i in range(0, n, self.ramka):
            odsz = np.empty((self.ramka, buf.shape[1]), np.float32)
            for k, stan in enumerate(self.stany):
                self._we[:] = buf[i:i + self.ramka, k] * 32768.0       # RNNoise liczy w skali int16
                self.mowa = self.lib.rnnoise_process_frame(stan, wy, we)
                odsz[:, k] = self._wy / 32768.0
            self._vad.append(self.mowa)
            self.kolejka.append((odsz, pelny[i:i + self.ramka], self._ramka_nr))
            self._ramka_nr += 1
        self.reszta = buf[n:]

        # ramki starsze niż wyprzedzenie: decyzja już znana (ton widziany z przodu)
        wynik = []
        czekaj = int(self.WYPRZEDZENIE_S * self.fs / self.ramka)
        while len(self.kolejka) > czekaj:
            odsz, oryg, nr = self.kolejka.popleft()
            probka_wejscia = nr * self.ramka - self.OPOZNIENIE_RNN
            self.ton = self._ton_od <= probka_wejscia < self._ton_do
            cel = 0.0 if self.ton else self.sila
            stare = self.mieszanie
            self.mieszanie += max(-self.KROK_MIESZANIA, min(self.KROK_MIESZANIA, cel - stare))
            g = np.linspace(stare, self.mieszanie, self.ramka, dtype=np.float32)[:, None]
            wynik.append(g * odsz + (1 - g) * oryg)
        return np.concatenate(wynik) if wynik else np.zeros((0, buf.shape[1]), np.float32)


def parsuj_godzine(tekst):
    """'7', '7:05', '07.05' -> minuty od północy albo None."""
    g, _, m = tekst.strip().replace(".", ":").partition(":")
    try:
        g, m = int(g), int(m or 0)
    except ValueError:
        return None
    return g * 60 + m if 0 <= g < 24 and 0 <= m < 60 else None


def w_godzinach(teraz, wl, wyl):
    """Czy minuta 'teraz' mieści się między włączeniem a wyłączeniem (także przez północ)."""
    if wl == wyl:
        return True
    if wl < wyl:
        return wl <= teraz < wyl
    return teraz >= wl or teraz < wyl


def _odcinki(stan, krok_ms):
    """[(włączony, czas_ms), ...] z tablicy bool."""
    wynik = []
    for v in stan:
        if wynik and wynik[-1][0] == v:
            wynik[-1][1] += krok_ms
        else:
            wynik.append([bool(v), krok_ms])
    return wynik


def _sklej(odc, min_ton, min_przerwa):
    """Za krótkie odcinki (dziury w tonie, trzaski w ciszy) doklejamy do poprzedniego."""
    wynik = []
    for stan, ms in odc:
        if wynik and ms < (min_ton if stan else min_przerwa):
            stan = wynik[-1][0]
        if wynik and wynik[-1][0] == stan:
            wynik[-1][1] += ms
        else:
            wynik.append([stan, ms])
    return wynik


def _kropka(odc):
    """Długość kropki: k-średnie na dwie grupy (kropki / kreski) albo None."""
    tony = sorted(ms for stan, ms in odc if stan)
    if len(tony) < 3:
        return None
    a = float(tony[0])
    b = 3 * a                                           # kreska = 3 kropki; start odporny na zlepki trzasków
    for _ in range(10):
        krotkie = [d for d in tony if abs(d - a) <= abs(d - b)]
        dlugie = [d for d in tony if abs(d - a) > abs(d - b)]
        a = float(np.mean(krotkie)) if krotkie else a
        b = float(np.mean(dlugie)) if dlugie else 3 * a
    # same kreski (np. „TO”): odstęp w znaku jest wtedy dużo krótszy od najkrótszego tonu
    przerwy = [ms for stan, ms in odc if not stan and ms < 1.5 * a]
    return min(a, float(np.median(przerwy))) if przerwy else a


def _rytm_cw(odc, kropka):
    """Udział tonów trwających ~1 albo ~3 kropki — w telegrafii prawie 100 %, w mowie dużo mniej."""
    tony = [ms / kropka for stan, ms in odc if stan]
    return sum(0.5 <= r <= 1.7 or 2.2 <= r <= 4.5 for r in tony) / len(tony)


def _cechy_kluczowania(x, obw, wlaczony, krok, k):
    """(czystość, rozrzut) w czasie znaków: czystość = udział tonu w całym sygnale (czysty ton ≈ 1, muzyka
    i mowa ≈ 0,2–0,4), rozrzut = zmienność głośności kolejnych znaków (kluczowany nadajnik ≈ 0, nuty dużo więcej)."""
    if not wlaczony.any():
        return 0.0, 9.9
    suma = np.concatenate([[0.0], np.cumsum(x.astype(np.float64) ** 2)])     # RMS w oknach k bez drugiego FFT
    srodki = np.arange(len(obw)) * krok
    gora = np.minimum(srodki + k - k // 2, len(x))
    dol = np.maximum(srodki - k // 2, 0)
    rms = np.sqrt(np.maximum(suma[gora] - suma[dol], 0) / k) + 1e-9
    czystosc = float(np.median(obw[wlaczony] / rms[wlaczony]) / 0.7071)       # czysty ton: obwiednia = RMS/√2·…
    poziomy, start = [], None
    for i, v in enumerate(np.append(wlaczony, False)):
        if v and start is None:
            start = i
        elif not v and start is not None:
            if i - start >= 4:                                                 # znaki od 20 ms
                poziomy.append(float(np.median(obw[start:i])))
            start = None
    rozrzut = float(np.std(poziomy) / np.mean(poziomy)) if len(poziomy) >= 3 else 9.9
    return czystosc, rozrzut


def dekoduj_cw(x, fs):
    """Szuka tonu telegrafii 400-1500 Hz i zwraca odczytany tekst albo None."""
    if len(x) < fs // 2:
        return None
    widmo = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    czest = np.fft.rfftfreq(len(x), 1 / fs)
    pasmo = (czest >= 400) & (czest <= 1500)
    if not pasmo.any():
        return None
    f0 = czest[pasmo][np.argmax(widmo[pasmo])]
    if widmo[pasmo].max() < 8 * np.median(widmo[pasmo]):
        return None                                     # brak wyraźnego tonu (mowa, szum)

    krok = fs // 200                                    # obwiednia co 5 ms
    t = np.arange(len(x)) / fs
    k = int(fs * 0.01)
    obw_pelna = np.abs(np.convolve(x * np.exp(-2j * np.pi * f0 * t), np.ones(k) / k, mode="same"))[::krok]
    obw = np.minimum(obw_pelna, np.percentile(obw_pelna, 98))     # pojedyncze trzaski nie zawyżają progu
    tlo, szczyt = np.percentile(obw, 20), np.median(obw[obw > 0.25 * obw.max()])
    if szczyt < 4 * max(tlo, 1e-6):
        return None
    wlaczony = obw > tlo + 0.5 * (szczyt - tlo)
    czyste = _sklej(_odcinki(wlaczony, 5), 20, 20)
    kropka = _kropka(czyste)
    if kropka is None:
        return None
    czyste = _sklej(czyste, 0.5 * kropka, 0.5 * kropka)
    while czyste and not czyste[0][0]:
        czyste.pop(0)
    while czyste and not czyste[-1][0]:
        czyste.pop()
    kropka = _kropka(czyste)
    if kropka is None:
        return None
    # trzask przy kluczowaniu/zamykaniu nadajnika: za krótki ton na samym początku lub końcu
    while czyste and czyste[-1][0] and czyste[-1][1] < 0.75 * kropka:
        czyste = czyste[:-2]
    while czyste and czyste[0][0] and czyste[0][1] < 0.75 * kropka:
        czyste = czyste[2:]
    if not czyste or _rytm_cw(czyste, kropka) < 0.8:
        return None
    czystosc, rozrzut = _cechy_kluczowania(x, obw_pelna, wlaczony, krok, k)
    if not ((czystosc >= CW_MIN_CZYSTOSC and rozrzut <= CW_MAX_ROZRZUT)
            or (czystosc >= CW_SLABY_MIN_CZYSTOSC and rozrzut <= CW_SLABY_MAX_ROZRZUT)):
        return None                                     # muzyka / mowa z tonami, nie kluczowany nadajnik

    tekst, znak = "", ""
    for stan, ms in czyste:
        if stan:
            znak += "-" if ms > 2 * kropka else "."
        elif ms > 2 * kropka:
            tekst += MORSE.get(znak, "?")
            znak = ""
            if ms > 5 * kropka:
                tekst += " "
    tekst = (tekst + MORSE.get(znak, "?")).strip()
    if len(tekst.replace(" ", "")) < 2 or tekst.count("?") > len(tekst) * 0.3:
        return None
    return tekst


TYPY_PLAYLIST = ("audio/x-mpegurl", "audio/mpegurl", "application/x-mpegurl", "application/vnd.apple.mpegurl",
                 "audio/x-scpls", "audio/scpls", "application/pls+xml")
NAZWY_KODEKOW = {"mp3float": "MP3", "mp3": "MP3", "aac": "AAC", "aac_latm": "AAC", "opus": "Opus",
                 "libopus": "Opus", "vorbis": "Vorbis", "flac": "FLAC"}


def adres_z_playlisty(tekst, baza):
    """Pierwszy adres strumienia z playlisty .m3u albo .pls (adresy względne liczone od adresu playlisty)."""
    for linia in tekst.splitlines():
        linia = linia.strip()
        if linia.lower().startswith("file") and "=" in linia:          # .pls: File1=http://…
            linia = linia.split("=", 1)[1].strip()
        elif not linia or linia.startswith(("#", "[")) or ("=" in linia and "://" not in linia):
            continue
        return urllib.parse.urljoin(baza, linia)
    return None


class CzytnikICY:
    """Bajty strumienia bez wtrąconych co icy-metaint bajtów metadanych ICY (StreamTitle trafia do `przy_tytule`).
    Udaje plik dla PyAV i naglowek_wav (read), pozwala podejrzeć początek bez zużycia (podejrzyj)."""

    def __init__(self, resp, metaint, przy_tytule):
        self.resp, self.metaint, self.przy_tytule = resp, metaint, przy_tytule
        self.do_metadanych = metaint
        self.zapas = b""
        self.tytul = ""
        self._czytaj = getattr(resp, "read1", resp.read)     # read1: tyle, ile już przyszło (mniejsze opóźnienie)

    def _odbierz(self, n):
        if self.metaint and self.do_metadanych == 0:
            dlugosc = self.resp.read(1)
            if not dlugosc:
                return b""
            if dlugosc[0]:
                self._metadane(czytaj(self.resp, dlugosc[0] * 16))
            self.do_metadanych = self.metaint
        if self.metaint:
            n = min(n, self.do_metadanych)
        dane = self._czytaj(n)
        if self.metaint:
            self.do_metadanych -= len(dane)
        return dane

    def _metadane(self, surowe):
        tekst = surowe.rstrip(b"\0")
        try:
            tekst = tekst.decode("utf-8")
        except UnicodeDecodeError:
            tekst = tekst.decode("cp1250", "replace")
        m = re.search(r"StreamTitle='(.*?)';", tekst, re.S)
        if m and m.group(1).strip() != self.tytul:
            self.tytul = m.group(1).strip()
            self.przy_tytule(self.tytul)

    def read(self, n=-1):
        if n is None or n < 0:
            n = 65536
        if self.zapas:
            dane, self.zapas = self.zapas[:n], self.zapas[n:]
            return dane
        return self._odbierz(n)

    def podejrzyj(self, n):
        while len(self.zapas) < n:
            dane = self._odbierz(n - len(self.zapas))
            if not dane:
                break
            self.zapas += dane
        return self.zapas[:n]


def czytaj(resp, n):
    buf = b""
    while len(buf) < n:
        k = resp.read(n - len(buf))
        if not k:
            raise ConnectionError("serwer zamknął strumień")
        buf += k
    return buf


def naglowek_wav(resp):
    """Czyta nagłówek RIFF/WAVE aż do bloku 'data'; zwraca (częstotliwość, kanały)."""
    riff = czytaj(resp, 12)
    if riff[:4] != b"RIFF" or riff[8:12] != b"WAVE":
        raise ValueError("to nie jest strumień WAV")
    fmt = None
    while True:
        cid, rozmiar = struct.unpack("<4sI", czytaj(resp, 8))
        if cid == b"data":
            break
        dane = czytaj(resp, rozmiar + (rozmiar & 1))
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", dane[:16])
    if fmt is None:
        raise ValueError("brak bloku fmt w WAV")
    tag, kanaly, fs, _, _, bity = fmt
    if tag != 1 or bity != 16:
        raise ValueError(f"nieobsługiwany format WAV (kod {tag}, {bity} bit)")
    return fs, kanaly


class Sesja:
    """Jedno słuchanie: wątek sieciowy + bufor + strumień audio. Zdarzenia dla GUI idą przez kolejkę."""

    def __init__(self, url, urzadzenie, glosnosc, zdarzenia):
        self.url = url
        self.urzadzenie = urzadzenie
        self.glosnosc = glosnosc
        self.zdarzenia = zdarzenia
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.bufor = collections.deque()
        self.ile = 0
        self.czeka = True
        self.prebufor = 0
        self.max_bufor = 0
        self.wyjscie = None
        self.format = None
        self.poziom_db = -120.0
        self.niedobory = 0
        self.wylacz_ton = False
        self.ogon_ms = 0             # 0 = bez wycinania ogona
        self.odszumianie = False
        self.sila_odszumiania = 1.0
        self.podtrzymanie = 0.0      # RMS cichego szumu dokładanego do głośnika (0 = wył.), żeby nie usypiał w ciszy
        self._szum = None
        self._szum_poz = 0
        self.ostatni_callback = None # kiedy karta ostatnio wzięła dźwięk (strażnik wykrywa „stojący” strumień)
        self.nazwa = urllib.parse.urlparse(url).netloc or "strumień"   # do dziennika (bez adresu i klucza)
        self._ostatni_blad = None
        self._proby = 0
        self.odszum = None
        self._linia = collections.deque()
        self._w_linii = 0
        self._bylo_glosno = False
        self.filtr = None
        self.ctcss_teraz = None      # ton w trwającym nadawaniu
        self.ctcss_ostatni = None    # ostatnio wykryty ton
        self._ostatni_napis = ""     # ostatni napis (ICY) wpisany na listę — bez dubli po ponownym połączeniu
        self.watek = threading.Thread(target=self._siec, daemon=True)

    def start(self):
        self.watek.start()

    def zatrzymaj(self):
        self.stop.set()
        self._zamknij_wyjscie()

    def _status(self, tekst):
        self.zdarzenia.put(("status", tekst))

    def _otworz_wyjscie(self, fs, kanaly):
        with BLOKADA_AUDIO:          # strażnik dźwięku nie odświeży PortAudio w połowie otwierania
            if self.format == (fs, kanaly) and self.wyjscie is not None:
                return
            self._zamknij_wyjscie()
            with self.lock:
                self.bufor.clear()
                self.ile = 0
                self.czeka = True
                self.prebufor = int(PREBUFOR_S * fs)
                self.max_bufor = int(MAX_BUFOR_S * fs)
            self.filtr = FiltrCTCSS(fs, kanaly)
            self.odszum = None
            self._szum = np.random.default_rng().standard_normal((2 * fs, kanaly)).astype(np.float32)   # 2 s pętli
            self._szum_poz = 0
            self._linia.clear()
            self._w_linii = 0
            self._bylo_glosno = False
            self.format = (fs, kanaly)
            self.uruchom_strumien()

    def uruchom_strumien(self):
        """Strumień PortAudio dla bieżącego formatu; wyjście szukane po nazwie (numery zmieniają się po
        odświeżeniu PortAudio), a gdy go nie ma — domyślne Windows. Bufor i obróbka zostają nietknięte."""
        fs, kanaly = self.format
        with BLOKADA_AUDIO:
            if self.stop.is_set() or self.wyjscie is not None:
                return
            indeks = wyjscia_audio().get(self.urzadzenie) if self.urzadzenie else None
            wyjscie = sd.OutputStream(samplerate=fs, channels=kanaly, dtype="float32",
                                      device=indeks, latency="high", callback=self._callback)
            wyjscie.start()
            self.ostatni_callback = time.monotonic()
            self.wyjscie = wyjscie

    def _zamknij_wyjscie(self):
        with BLOKADA_AUDIO:
            w, self.wyjscie = self.wyjscie, None
            if w is not None:
                try:
                    w.stop()
                    w.close()
                except Exception:
                    pass

    def _callback(self, out, frames, czas, status):
        self.ostatni_callback = time.monotonic()
        with self.lock:
            if self.czeka and self.ile >= self.prebufor:
                self.czeka = False
            if self.czeka:
                out.fill(0)
            else:
                n = 0
                while n < frames and self.bufor:
                    blok = self.bufor[0]
                    k = min(frames - n, len(blok))
                    out[n:n + k] = blok[:k]
                    if k == len(blok):
                        self.bufor.popleft()
                    else:
                        self.bufor[0] = blok[k:]
                    n += k
                self.ile -= n
                if n < frames:
                    out[n:].fill(0)
                    self.czeka = True
                    self.niedobory += 1
        out *= self.glosnosc
        if self.podtrzymanie and self._szum is not None:   # po głośności: działa też przy Mute i poza godzinami
            out += self._kawalek_szumu(frames) * self.podtrzymanie

    def _kawalek_szumu(self, n):
        indeksy = (self._szum_poz + np.arange(n)) % len(self._szum)
        self._szum_poz = (self._szum_poz + n) % len(self._szum)
        return self._szum[indeksy]

    def _dodaj(self, blok):
        with self.lock:
            self.bufor.append(blok)
            self.ile += len(blok)
            if self.ile > self.max_bufor:
                while self.ile > self.prebufor and self.bufor:
                    self.ile -= len(self.bufor.popleft())

    def _linia_ogona(self, x, fs, cisza):
        """Linia opóźniająca o ogon_ms: gdy po dźwięku przychodzi cisza (koniec transmisji), czekające w niej
        ostatnie ms to szum blokady (ogon) i zostają wyciszone. Zwraca bloki gotowe do odtworzenia."""
        if cisza and self._bylo_glosno and self._linia:
            pierwszy = self._linia[0]
            wygas = min(len(pierwszy), int(0.005 * fs))      # 5 ms wyciszania zamiast trzasku
            cichy = np.zeros_like(pierwszy)
            cichy[:wygas] = pierwszy[:wygas] * np.linspace(1, 0, wygas, dtype=np.float32)[:, None]
            self._linia = collections.deque([cichy] + [np.zeros_like(b) for b in list(self._linia)[1:]])
        self._bylo_glosno = not cisza
        self._linia.append(x)
        self._w_linii += len(x)
        n = int(self.ogon_ms * fs / 1000)
        gotowe = []
        while self._w_linii > n:
            b = self._linia[0]
            nadmiar = self._w_linii - n
            if len(b) <= nadmiar:
                gotowe.append(self._linia.popleft())
                self._w_linii -= len(b)
            else:
                gotowe.append(b[:nadmiar])
                self._linia[0] = b[nadmiar:]
                self._w_linii -= nadmiar
        return gotowe

    def _odszumiaj(self, x, fs):
        """RNNoise na dźwięku do głośnika (dekoder CW, CTCSS i lista pracują na oryginale)."""
        if not self.odszumianie or fs != 48000:
            self.odszum = None
            return x
        if self.odszum is None:
            try:
                self.odszum = Odszumianie(x.shape[1], fs)
            except OSError as e:
                self.odszumianie = False
                self._status(f"Odszumianie niedostępne: {e}")
                return x
        self.odszum.sila = self.sila_odszumiania
        return self.odszum(x)

    def bufor_s(self):
        if not self.format:
            return 0.0
        return self.ile / self.format[0]

    def _koniec_nadawania(self, od, do, nagranie, fs):
        try:
            x = np.concatenate(nagranie)
            cw = dekoduj_cw(x, fs)
            ton = wykryj_ctcss(x, fs)
        except Exception:
            cw = ton = None
        info = f"CW: {cw}" if cw else "fonia"
        if ton:
            self.ctcss_ostatni = ton
            info += f"   CTCSS {opis_tonu(ton)}"
        self.zdarzenia.put(("wylaczenie", od, do, info))

    def _pokaz_opis(self, opis=None, tytul=None):
        """Linia stanu: format strumienia i tytuł utworu z metadanych ICY."""
        if opis is not None:
            self._opis = opis
            if self._ostatni_blad is not None:
                dziennik(f"[{self.nazwa}] połączono ponownie po {self._proby} próbach: {opis}")
            elif not self._proby:
                dziennik(f"[{self.nazwa}] połączono: {opis}")
            self._ostatni_blad, self._proby = None, 1
        if tytul is not None:
            self._tytul = tytul
        tekst = f"Odbieram: {self._opis}"
        if self._tytul:
            tekst += f"   ♪ {self._tytul}"
        self._status(tekst)

    def _nowy_napis(self, tekst):
        """Napis ze strumienia (ICY StreamTitle, jak RDS w radiu FM): linia stanu + wpis na liście przy zmianie."""
        self._pokaz_opis(tytul=tekst)
        if tekst and tekst != self._ostatni_napis:
            self._ostatni_napis = tekst
            self.zdarzenia.put(("napis", time.time(), tekst))

    def _przetworz(self, x, fs, stan):
        """Wspólna obróbka bloku (n, kanały) float32 z dowolnego źródła: dźwięk do głośnika + aktywność, CW, CTCSS."""
        rms = float(np.sqrt(np.mean(x * x))) if len(x) else 0.0
        self.poziom_db = 20 * np.log10(rms) if rms > 1e-6 else -120.0
        # między transmisjami strumień nasłuchu ma równe zera; ogon wycinamy przed filtrem tonu
        for blok in self._linia_ogona(x, fs, cisza=float(np.abs(x).max(initial=0)) < 1e-3):
            bez_tonu = self.filtr(blok)      # filtr pracuje stale, przełączanie bez trzasku
            gotowy = self._odszumiaj(bez_tonu if self.wylacz_ton else blok, fs)
            if len(gotowy):
                self._dodaj(gotowy)

        teraz = time.time()
        if self.poziom_db > PROG_AKTYWNOSCI_DB:
            stan.ostatnio_glos = teraz
            if stan.nadaje_od is None:
                stan.nadaje_od = teraz
                stan.nagranie, stan.nagrane, stan.analiza = [], 0, 0
                self.zdarzenia.put(("wlaczenie", teraz))
        if stan.nadaje_od is not None:
            if stan.nagrane < MAX_NAGRANIE_S * fs:
                stan.nagranie.append(x.mean(axis=1))          # do CW/CTCSS wystarczy mono
                stan.nagrane += len(x)
            if stan.nagrane - stan.analiza >= fs // 2 and stan.nagrane < MAX_NAGRANIE_S * fs:
                stan.analiza = stan.nagrane                   # CTCSS na żywo: ostatnie 2 s, co 0,5 s
                ton = wykryj_ctcss(ogon(stan.nagranie, 2 * fs), fs)
                self.ctcss_teraz = ton or 0.0                 # 0.0 = sprawdzone, tonu brak
                if ton:
                    self.ctcss_ostatni = ton
            if teraz - stan.ostatnio_glos > KONIEC_NADAWANIA_S:
                self._koniec_nadawania(stan.nadaje_od, stan.ostatnio_glos, stan.nagranie, fs)
                self.ctcss_teraz = None
                stan.nadaje_od = None
                stan.nagranie, stan.nagrane = [], 0

    def _odbieraj(self, url, stan, glebokosc=0):
        """Otwiera adres i wybiera dekoder: playlista -> jej pierwszy adres, HLS -> FFmpeg sam,
        WAV -> własny odczyt PCM (najmniejsze opóźnienie), reszta (MP3, AAC, OGG, FLAC…) -> FFmpeg."""
        if glebokosc > 3:
            raise ValueError("za dużo zagnieżdżonych playlist")
        req = urllib.request.Request(url, headers={"User-Agent": AGENT, "Icy-MetaData": "1"})
        try:
            resp = urllib.request.urlopen(req, timeout=15)
        except http.client.BadStatusLine:          # stary Shoutcast odpowiada „ICY 200 OK” — łączy FFmpeg
            return self._dekoduj_av(url, stan)
        with resp:
            typ = resp.headers.get_content_type()
            sciezka = urllib.parse.urlparse(resp.geturl()).path.lower()
            if typ in TYPY_PLAYLIST or sciezka.endswith((".m3u", ".m3u8", ".pls")):
                tekst = resp.read(65536).decode("utf-8", "replace")
                if "#EXT-X-" in tekst:             # HLS: segmenty pobiera FFmpeg
                    return self._dekoduj_av(resp.geturl(), stan)
                nastepny = adres_z_playlisty(tekst, resp.geturl())
                if not nastepny:
                    raise ValueError("playlista bez adresu strumienia")
                return self._odbieraj(nastepny, stan, glebokosc + 1)
            czytnik = CzytnikICY(resp, int(resp.headers.get("icy-metaint") or 0), self._nowy_napis)
            if czytnik.podejrzyj(4) == b"RIFF":
                return self._odbieraj_wav(czytnik, stan)
            return self._dekoduj_av(czytnik, stan)

    def _odbieraj_wav(self, zrodlo, stan):
        fs, kanaly = naglowek_wav(zrodlo)
        self._otworz_wyjscie(fs, kanaly)
        self._pokaz_opis(f"WAV {fs} Hz, " + ("mono" if kanaly == 1 else f"{kanaly} kanały"))
        ramka = 2 * kanaly
        reszta = b""
        while not self.stop.is_set():
            dane = czytaj(zrodlo, 4096)            # pełne porcje po 4096 bajtów, jak od początku
            dane = reszta + dane
            n = len(dane) // ramka * ramka
            reszta = dane[n:]
            x = np.frombuffer(dane[:n], dtype="<i2").astype(np.float32) / 32768.0
            self._przetworz(x.reshape(-1, kanaly), fs, stan)

    def _dekoduj_av(self, zrodlo, stan):
        """Strumień skompresowany przez FFmpeg (PyAV), przeliczony na 48 kHz (tego wymaga RNNoise)."""
        import av                                  # FFmpeg ładowany dopiero przy strumieniach skompresowanych
        opcje = {"probesize": "65536", "analyzeduration": "1000000"}
        if isinstance(zrodlo, str):
            opcje.update(user_agent=AGENT, reconnect="1")
        kontener = av.open(zrodlo, mode="r", options=opcje, timeout=15)
        try:
            strumien = kontener.streams.audio[0]
            kanaly = 1 if (strumien.channels or 2) == 1 else 2
            fs = 48000
            przelicz = av.AudioResampler(format="flt", layout="mono" if kanaly == 1 else "stereo", rate=fs)
            self._otworz_wyjscie(fs, kanaly)
            cc = strumien.codec_context
            opis = NAZWY_KODEKOW.get(cc.name, cc.name.upper())
            if cc.bit_rate:
                opis += f" {cc.bit_rate // 1000} kb/s"
            opis += f", {cc.sample_rate / 1000:g} kHz ".replace(".", ",") + ("mono" if kanaly == 1 else "stereo")
            if isinstance(zrodlo, str) and ".m3u8" in zrodlo.lower():
                opis = "HLS " + opis
            self._pokaz_opis(opis)
            for ramka in kontener.decode(strumien):
                if self.stop.is_set():
                    return
                for r in przelicz.resample(ramka):
                    self._przetworz(r.to_ndarray().reshape(-1, kanaly), fs, stan)
        finally:
            kontener.close()
        raise ConnectionError("serwer zamknął strumień")

    def _siec(self):
        stan = types.SimpleNamespace(nadaje_od=None, ostatnio_glos=0.0, nagranie=[], nagrane=0, analiza=0)
        while not self.stop.is_set():
            try:
                self._status("Łączę…")
                self._opis, self._tytul = "", ""
                self._odbieraj(self.url, stan)
            except Exception as e:
                if self.stop.is_set():
                    break
                if str(e) != self._ostatni_blad:     # w dzienniku tylko pierwszy z serii takich samych błędów
                    dziennik(f"[{self.nazwa}] przerwa: {type(e).__name__}: {e}")
                    if self._ostatni_blad is None:
                        self._proby = 0
                    self._ostatni_blad = str(e)
                self._proby += 1
                self._status(f"Przerwa: {e} — ponawiam za {PONOW_PO_S:.0f} s")
                self.stop.wait(PONOW_PO_S)
        self._zamknij_wyjscie()


DOMYSLNE_USTAWIENIA = {
    "nazwa": "", "url": "", "wyjscie": DOMYSLNE_WYJSCIE, "glosnosc": 100,
    "harmonogram": False, "godz_wl": "07:00", "godz_wyl": "22:00",
    "wylacz_ton": False, "wytnij_ogon": True, "ogon_ms": 150,
    "odszumianie": True, "sila_odszumiania": 100,
    "podtrzymanie": False, "podtrzymanie_db": -60,
}

OPISY = {
    "lista": "Lista zapisanych strumieni (● = okno otwarte). Kliknięcie pozycji otwiera ją w nowym oknie, "
             "a jeśli okno już jest otwarte – wysuwa je na wierzch. „+ Dodaj nowy…” dodaje strumień.\n"
             "Prawy przycisk na pozycji listy: otwórz, edytuj albo usuń.",
    "url": "Adres strumienia (http/https) w tym oknie: WAV, MP3, AAC, OGG (Opus/Vorbis/FLAC), HLS (.m3u8) "
           "albo playlista .m3u/.pls. Tytuł utworu z radia internetowego pojawia się w linii stanu (♪). "
           "Zmiana adresu działa po ponownym kliknięciu „Słuchaj”.",
    "nazwa": "Nazwa strumienia: tytuł tego okna i pozycja na liście strumieni.",
    "wyjscie": "Karta dźwiękowa, na którą gra to okno. Zmiana działa po ponownym kliknięciu „Słuchaj”. "
               "Program co 2 s sprawdza dźwięk Windows: gdy zmieni się domyślne wyjście (np. po włączeniu "
               "Pulpitu zdalnego) albo urządzenie zniknie, sam przełącza dźwięk – „Domyślne” idzie za Windows, "
               "a wybrane urządzenie wraca, gdy znów się pojawi.",
    "sluchaj": "Włącza i wyłącza odbiór strumienia w tym oknie. Okna grają niezależnie, także naraz.",
    "godziny": "Harmonogram: gdy zaznaczone „wg godzin”, dźwięk gra między godziną włączenia a wyłączenia, "
               "a poza nimi jest wyciszony (odbiór i lista aktywności działają dalej). "
               "Działa też przez północ, np. 22:00–06:00. Format GG:MM.",
    "glosnosc": "Głośność tego okna 0–200 %. Kółko myszy nad suwakiem: wolno ±1 %, szybko ±10 %.",
    "mute": "Wycisza to okno (czerwony = wyciszone). Odbiór i lista aktywności działają dalej.",
    "podtrzymanie": "Dokłada do dźwięku bardzo cichy szum, żeby głośnik z automatycznym wyłączaniem (np. JBL) "
                    "nie usypiał w ciszy. Działa po regulacji głośności – także przy Mute, głośności 0 i poza "
                    "godzinami z harmonogramu – ale tylko gdy okno słucha („Słuchaj”). Jeśli głośnik nadal się "
                    "wyłącza, podnieś poziom (np. −50 dBFS); jeśli szum słychać, obniż go.",
    "ctcss": "Ton CTCSS (podnośna) wykryty w strumieniu: zielony – w trwającej transmisji, "
             "szary – ostatnio wykryty, „brak tonu” – nadawanie bez podnośnej.",
    "wylacz_ton": "Wycina podnośną CTCSS z dźwięku (filtr górnoprzepustowy od 300 Hz). Głos zostaje bez zmian.",
    "ogon": "Wycisza ostatnie milisekundy przed końcem każdej transmisji – szum blokady („ogon”). "
            "Dźwięk jest opóźniony o ustawioną długość.",
    "rnnoise": "Odszumianie siecią neuronową RNNoise, liczone na komputerze (bez internetu). "
               "Przy telegrafii i sygnałach tonowych samo się wyłącza, żeby ich nie wyciąć. "
               "Dotyczy tylko dźwięku – dekoder CW, CTCSS i lista pracują na oryginale. "
               "Jest zrobione do mowy – przy radiu z muzyką lepiej je odznaczyć.",
    "sila": "Siła odszumiania: 100 % – sam dźwięk odszumiony, mniej – domieszka oryginału "
            "(zostaje trochę naturalnego szumu).",
    "poziom": "Poziom sygnału w strumieniu (dBFS) i ilość dźwięku w buforze.",
    "lampka": "NADAJE – w strumieniu jest sygnał (trwa transmisja); cisza – brak sygnału.",
    "status": "Stan połączenia: format strumienia i – przy radiu internetowym – tytuł utworu (♪).",
    "aktywnosc": "Lista transmisji: godzina początku i końca, czas trwania, odczytana telegrafia (CW) "
                 "albo „fonia”, ton CTCSS. Na niebiesko (♪) – każda zmiana napisu z radia internetowego "
                 "(tytuł, tekst jak RDS) z godziną. Najnowsze u góry. "
                 "Prawy przycisk na liście zdejmuje zaznaczenie wpisu.",
}


class Dymek:
    """Dymek z opisem funkcji: najedź na element i kliknij prawym przyciskiem; znika po zjechaniu myszą."""
    aktywny = None

    @classmethod
    def podepnij(cls, tekst, *widgety):
        for w in widgety:
            w.bind("<Button-3>", lambda e, t=tekst: cls.pokaz(e, t), add="+")
            w.bind("<Leave>", lambda e: cls.ukryj(), add="+")

    @classmethod
    def pokaz(cls, event, tekst):
        cls.ukryj()
        top = tk.Toplevel(event.widget)
        top.wm_overrideredirect(True)
        top.attributes("-topmost", True)
        tk.Label(top, text=tekst, justify="left", wraplength=340, bg="#ffffe1", fg="#000000",
                 relief="solid", borderwidth=1, padx=8, pady=5, font=("Segoe UI", 9)).pack()
        top.update_idletasks()
        w, h = top.winfo_reqwidth(), top.winfo_reqheight()
        x = min(event.x_root + 12, top.winfo_screenwidth() - w - 4)
        y = event.y_root + 16 if event.y_root + 16 + h < top.winfo_screenheight() else event.y_root - h - 8
        top.geometry(f"+{x}+{y}")
        top.bind("<Button>", lambda e: cls.ukryj())
        top.after(20000, cls.ukryj)
        cls.aktywny = top
        return "break"

    @classmethod
    def ukryj(cls):
        if cls.aktywny is not None:
            try:
                cls.aktywny.destroy()
            except tk.TclError:
                pass
            cls.aktywny = None


class ListaStrumieni:
    """Rozwijana lista strumieni pod przyciskiem: lewy = otwórz w oknie, prawy = otwórz/edytuj/usuń."""

    def __init__(self, okno, przycisk):
        self.okno, self.przycisk = okno, przycisk
        self.top = None
        self._wstrzymaj = False      # menu prawego przycisku / okienko akcji: nie zamykać listy przy utracie fokusu

    def przelacz(self):
        if self.top:
            self.zamknij()
        else:
            self.otworz()

    def otworz(self):
        Dymek.ukryj()
        top = self.top = tk.Toplevel(self.przycisk)
        top.wm_overrideredirect(True)
        top.attributes("-topmost", True)
        lb = self.lb = tk.Listbox(top, activestyle="none", font=("Segoe UI", 10), borderwidth=1,
                                  relief="solid", highlightthickness=0, selectbackground="#0078d7")
        lb.pack(fill="both", expand=True)
        self._wypelnij()
        lb.bind("<Motion>", self._podswietl)
        lb.bind("<ButtonRelease-1>", self._lewy)
        lb.bind("<Button-3>", self._prawy)
        top.bind("<Escape>", lambda e: self.zamknij())
        top.bind("<ButtonPress>", self._klik_poza, add="+")
        lb.bind("<FocusOut>", lambda e: top.after(150, self._fokus_poza))
        top.grab_set()
        lb.focus_set()

    def _wypelnij(self):
        """Pozycje listy od nowa (po edycji/usunięciu lista zostaje otwarta i pokazuje stan bieżący)."""
        app = self.okno.app
        self.pozycje = list(app.strumienie)
        self.lb.delete(0, "end")
        for s in self.pozycje:
            self.lb.insert("end", f"{'●' if app.okno_strumienia(s) else '   '} {s['nazwa'] or '(bez nazwy)'}")
        self.lb.insert("end", "   + Dodaj nowy…")
        self.lb.configure(height=min(14, len(self.pozycje) + 1))
        self.top.update_idletasks()
        szer = max(self.przycisk.winfo_width(), 300)
        self.top.geometry(f"{szer}x{self.lb.winfo_reqheight()}+{self.przycisk.winfo_rootx()}"
                          f"+{self.przycisk.winfo_rooty() + self.przycisk.winfo_height()}")

    def _istnieje(self):
        try:
            return self.top is not None and bool(self.top.winfo_exists())
        except tk.TclError:
            return False

    def zamknij(self):
        if self.top:
            try:
                self.top.grab_release()
                self.top.destroy()
            except tk.TclError:
                pass
            self.top = None

    def _podswietl(self, e):
        self.lb.selection_clear(0, "end")
        self.lb.selection_set(self.lb.nearest(e.y))

    def _klik_poza(self, e):
        # przy przechwyceniu (grab) klik w inne miejsce aplikacji trafia do okna listy
        if e.widget is not self.lb:
            self.zamknij()

    def _fokus_poza(self):
        if self._wstrzymaj or not self._istnieje():
            return
        if self.top.focus_get() is None:                  # użytkownik przeszedł do innego programu
            self.zamknij()

    def _lewy(self, e):
        i = self.lb.nearest(e.y)
        self.zamknij()
        app = self.okno.app
        if i >= len(self.pozycje):
            app.dodaj_strumien(self.okno)
        else:
            app.otworz(self.pozycje[i], obok=self.okno)

    def _prawy(self, e):
        """Menu pozycji; lista zostaje otwarta (także po akcji — wtedy odświeżona)."""
        i = self.lb.nearest(e.y)
        if i >= len(self.pozycje):
            return "break"
        s = self.pozycje[i]
        self.lb.selection_clear(0, "end")
        self.lb.selection_set(i)
        app = self.okno.app
        menu = self.menu = tk.Menu(self.top, tearoff=0)
        menu.add_command(label="Otwórz", command=lambda: self._akcja(lambda: app.otworz(s, obok=self.okno)))
        menu.add_command(label="Edytuj…", command=lambda: self._akcja(lambda: app.edytuj(s, self.okno, nad=self.top)))
        menu.add_separator()
        menu.add_command(label="Usuń", command=lambda: self._akcja(lambda: app.usun(s, self.okno, nad=self.top)))
        self._wstrzymaj = True       # na Windows menu jest modalne, a w tym czasie przychodzi utrata fokusu
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()
        if self._istnieje():
            self.top.after(300, self._wznow)
        return "break"

    def _wznow(self):
        self._wstrzymaj = False
        if self._istnieje():
            self.top.grab_set()
            self.lb.focus_set()

    def _akcja(self, akcja):
        self._wstrzymaj = True
        try:
            akcja()
        finally:
            if self._istnieje():
                self._wypelnij()
                self.top.after(300, self._wznow)
            else:                                         # np. usunięty strumień tego okna – lista zniknęła z oknem
                self.top = None
                self._wstrzymaj = False


def dialog_strumienia(rodzic, tytul, nazwa="", url="", na_wierzchu=False):
    """Okienko z nazwą i adresem strumienia; zwraca (nazwa, url) albo None.
    na_wierzchu: nad otwartą listą strumieni (ta jest „zawsze na wierzchu”)."""
    top = tk.Toplevel(rodzic)
    top.title(tytul)
    top.transient(rodzic)
    top.resizable(False, False)
    if na_wierzchu:
        top.attributes("-topmost", True)
    f = ttk.Frame(top, padding=12)
    f.pack(fill="both", expand=True)
    v_nazwa, v_url = tk.StringVar(value=nazwa), tk.StringVar(value=url)
    ttk.Label(f, text="Nazwa:").grid(row=0, column=0, sticky="w", pady=3)
    pole = ttk.Entry(f, textvariable=v_nazwa, width=60)
    pole.grid(row=0, column=1, sticky="ew", pady=3)
    ttk.Label(f, text="Adres strumienia:").grid(row=1, column=0, sticky="w", pady=3, padx=(0, 8))
    ttk.Entry(f, textvariable=v_url, width=60).grid(row=1, column=1, sticky="ew", pady=3)
    blad = ttk.Label(f, foreground="#c62828")
    blad.grid(row=2, column=0, columnspan=2, sticky="w")
    wynik = []

    def zapisz(*_):
        if not v_url.get().strip().lower().startswith(("http://", "https://")):
            blad.configure(text="Adres musi zaczynać się od http:// albo https://")
            return
        wynik.append((v_nazwa.get().strip(), v_url.get().strip()))
        top.destroy()

    przyciski = ttk.Frame(f)
    przyciski.grid(row=3, column=0, columnspan=2, sticky="e", pady=(8, 0))
    ttk.Button(przyciski, text="Zapisz", command=zapisz).pack(side="left", padx=4)
    ttk.Button(przyciski, text="Anuluj", command=top.destroy).pack(side="left")
    top.bind("<Return>", zapisz)
    top.bind("<Escape>", lambda e: top.destroy())
    top.update_idletasks()
    top.geometry(f"+{rodzic.winfo_rootx() + 40}+{rodzic.winfo_rooty() + 40}")
    top.grab_set()
    pole.focus_set()
    rodzic.wait_window(top)
    return wynik[0] if wynik else None


class Okno:
    """Okno jednego strumienia; ustawienia czyta i zapisuje w słowniku strumienia `s`."""

    def __init__(self, app, s):
        self.app, self.s = app, s
        self.root = root = tk.Toplevel(app.root)
        self.zdarzenia = queue.Queue()
        self.sesja = None
        self._zegar = None
        self.wyjscia = app.wyjscia
        ustaw = {**DOMYSLNE_USTAWIENIA, **s}

        root.minsize(560, 420)
        root.protocol("WM_DELETE_WINDOW", lambda: app.zamknij_okno(self))
        if s.get("geometria"):
            try:
                root.geometry(s["geometria"])
            except tk.TclError:
                pass
        f = ttk.Frame(root, padding=10)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)

        e = ttk.Label(f, text="Strumienie:")
        e.grid(row=0, column=0, sticky="w")
        lista = ttk.Button(f, text="Lista strumieni  ▾", width=24)
        lista.grid(row=0, column=1, sticky="w", pady=(0, 4))
        self.lista = ListaStrumieni(self, lista)
        lista.configure(command=self.lista.przelacz)
        Dymek.podepnij(OPISY["lista"], e, lista)
        tk.Label(f, text="73 de SP9TKN", fg="#9e9e9e", font=("Segoe UI", 8)).grid(row=0, column=2, sticky="ne")

        e = ttk.Label(f, text="Adres strumienia:")
        e.grid(row=1, column=0, sticky="w")
        self.url = tk.StringVar(value=ustaw["url"])
        self.url.trace_add("write", lambda *_: self.s.update(url=self.url.get().strip()))
        pole = ttk.Entry(f, textvariable=self.url)
        pole.grid(row=1, column=1, columnspan=2, sticky="ew", pady=2)
        Dymek.podepnij(OPISY["url"], e, pole)

        e = ttk.Label(f, text="Nazwa:")
        e.grid(row=2, column=0, sticky="w")
        wiersz = ttk.Frame(f)
        wiersz.grid(row=2, column=1, sticky="ew", pady=2)
        wiersz.columnconfigure(0, weight=1)
        self.nazwa = tk.StringVar(value=ustaw["nazwa"])
        self.nazwa.trace_add("write", lambda *_: self.ustaw_tytul())
        self.ustaw_tytul()
        pole = ttk.Entry(wiersz, textvariable=self.nazwa)
        pole.grid(row=0, column=0, sticky="ew")
        Dymek.podepnij(OPISY["nazwa"], e, pole)
        e = ttk.Label(wiersz, text="Wyjście:")
        e.grid(row=0, column=1, sticky="w", padx=(10, 4))
        zapisane = ustaw["wyjscie"]
        self.wyjscie = tk.StringVar(value=zapisane if zapisane in self.wyjscia else DOMYSLNE_WYJSCIE)
        pole = self.pole_wyjscia = ttk.Combobox(wiersz, textvariable=self.wyjscie, values=list(self.wyjscia),
                                                state="readonly", width=22)
        pole.grid(row=0, column=2, sticky="e")
        Dymek.podepnij(OPISY["wyjscie"], e, pole)
        self.przycisk = ttk.Button(f, text="▶  Słuchaj", width=14, command=self.przelacz)
        self.przycisk.grid(row=2, column=2, padx=(8, 0))
        Dymek.podepnij(OPISY["sluchaj"], self.przycisk)

        e = ttk.Label(f, text="Dźwięk:")
        e.grid(row=3, column=0, sticky="w")
        godziny = ttk.Frame(f)
        godziny.grid(row=3, column=1, columnspan=2, sticky="w", pady=2)
        self.harmonogram = tk.BooleanVar(value=bool(ustaw["harmonogram"]))
        w1 = ttk.Checkbutton(godziny, text="wg godzin", variable=self.harmonogram, command=self.sprawdz_godziny)
        w1.pack(side="left")
        w2 = ttk.Label(godziny, text="włączenie")
        w2.pack(side="left", padx=(12, 4))
        self.godz_wl = tk.StringVar(value=ustaw["godz_wl"])
        w3 = ttk.Entry(godziny, textvariable=self.godz_wl, width=6, justify="center")
        w3.pack(side="left")
        w4 = ttk.Label(godziny, text="wyłączenie")
        w4.pack(side="left", padx=(12, 4))
        self.godz_wyl = tk.StringVar(value=ustaw["godz_wyl"])
        w5 = ttk.Entry(godziny, textvariable=self.godz_wyl, width=6, justify="center")
        w5.pack(side="left")
        self.stan_dzwieku = tk.Label(godziny, font=("Segoe UI", 9))
        self.stan_dzwieku.pack(side="left", padx=(12, 0))
        self.wyciszony = False
        Dymek.podepnij(OPISY["godziny"], e, w1, w2, w3, w4, w5, self.stan_dzwieku)

        e = ttk.Label(f, text="Głośność:")
        e.grid(row=4, column=0, sticky="w")
        self.glosnosc = tk.IntVar(value=int(ustaw["glosnosc"]))
        suwak = ttk.Scale(f, from_=0, to=200, variable=self.glosnosc, command=self.zmien_glosnosc)
        suwak.grid(row=4, column=1, sticky="ew", pady=2)
        prawa = ttk.Frame(f)
        prawa.grid(row=4, column=2, sticky="w", padx=(8, 0))
        self.glosnosc_txt = ttk.Label(prawa, width=6)
        self.glosnosc_txt.pack(side="left")
        self.mute = False
        self.przycisk_mute = tk.Button(prawa, text="Mute", width=6, command=self.przelacz_mute)
        self.przycisk_mute.pack(side="left", padx=(4, 0))
        self._kolor_mute = (self.przycisk_mute.cget("bg"), self.przycisk_mute.cget("fg"))
        self._kolko_czas = 0.0
        for w in (suwak, self.glosnosc_txt):
            w.bind("<MouseWheel>", self.kolko_glosnosci)
        Dymek.podepnij(OPISY["glosnosc"], e, suwak, self.glosnosc_txt)
        Dymek.podepnij(OPISY["mute"], self.przycisk_mute)

        e = ttk.Label(f, text="Głośnik:")
        e.grid(row=5, column=0, sticky="w")
        podtrz = ttk.Frame(f)
        podtrz.grid(row=5, column=1, columnspan=2, sticky="w", pady=2)
        self.podtrzymanie = tk.BooleanVar(value=bool(ustaw["podtrzymanie"]))
        w1 = ttk.Checkbutton(podtrz, text="nie usypiaj (cichy szum)", variable=self.podtrzymanie,
                             command=self.zmien_podtrzymanie)
        w1.pack(side="left")
        self._szum_ok = int(ustaw["podtrzymanie_db"])
        self.szum_db = tk.StringVar(value=str(self._szum_ok))
        w2 = ttk.Spinbox(podtrz, from_=-90, to=-30, increment=5, width=5, textvariable=self.szum_db,
                         command=self.zmien_podtrzymanie)
        w2.pack(side="left", padx=(12, 0))
        w3 = ttk.Label(podtrz, text="dBFS")
        w3.pack(side="left", padx=(3, 0))
        self.szum_db.trace_add("write", lambda *_: self.zmien_podtrzymanie())
        Dymek.podepnij(OPISY["podtrzymanie"], e, w1, w2, w3)

        e = ttk.Label(f, text="CTCSS:")
        e.grid(row=6, column=0, sticky="w")
        podnosna = ttk.Frame(f)
        podnosna.grid(row=6, column=1, columnspan=2, sticky="w", pady=2)
        self.ctcss_txt = tk.Label(podnosna, text="—", width=16, anchor="w", font=("Segoe UI", 9, "bold"))
        self.ctcss_txt.pack(side="left")
        Dymek.podepnij(OPISY["ctcss"], e, self.ctcss_txt)
        self.wylacz_ton = tk.BooleanVar(value=bool(ustaw["wylacz_ton"]))
        w1 = ttk.Checkbutton(podnosna, text="wyłącz ton", variable=self.wylacz_ton, command=self.zmien_filtr)
        w1.pack(side="left", padx=(12, 0))
        Dymek.podepnij(OPISY["wylacz_ton"], w1)
        self.wytnij_ogon = tk.BooleanVar(value=bool(ustaw["wytnij_ogon"]))
        w1 = ttk.Checkbutton(podnosna, text="wytnij ogon", variable=self.wytnij_ogon, command=self.zmien_ogon)
        w1.pack(side="left", padx=(16, 4))
        self._ogon_ok = int(ustaw["ogon_ms"])
        self.ogon_ms = tk.StringVar(value=str(self._ogon_ok))
        w2 = ttk.Spinbox(podnosna, from_=0, to=1000, increment=10, width=5, textvariable=self.ogon_ms,
                         command=self.zmien_ogon)
        w2.pack(side="left")
        w3 = ttk.Label(podnosna, text="ms")
        w3.pack(side="left", padx=(3, 0))
        self.ogon_ms.trace_add("write", lambda *_: self.zmien_ogon())
        Dymek.podepnij(OPISY["ogon"], w1, w2, w3)

        e = ttk.Label(f, text="Odszumianie:")
        e.grid(row=7, column=0, sticky="w")
        szum = ttk.Frame(f)
        szum.grid(row=7, column=1, columnspan=2, sticky="ew", pady=2)
        szum.columnconfigure(2, weight=1)
        self.odszumianie = tk.BooleanVar(value=bool(ustaw["odszumianie"]))
        w1 = ttk.Checkbutton(szum, text="RNNoise", variable=self.odszumianie, command=self.zmien_odszumianie)
        w1.grid(row=0, column=0, sticky="w")
        w2 = ttk.Label(szum, text="siła")
        w2.grid(row=0, column=1, padx=(12, 4))
        self.sila = tk.IntVar(value=int(ustaw["sila_odszumiania"]))
        w3 = ttk.Scale(szum, from_=0, to=100, variable=self.sila, command=self.zmien_odszumianie)
        w3.grid(row=0, column=2, sticky="ew")
        self.sila_txt = ttk.Label(szum, width=6)
        self.sila_txt.grid(row=0, column=3, padx=(8, 0))
        self.odszum_txt = tk.Label(szum, width=18, anchor="w", font=("Segoe UI", 9), fg="#e65100")
        self.odszum_txt.grid(row=0, column=4, sticky="w")
        Dymek.podepnij(OPISY["rnnoise"], e, w1, self.odszum_txt)
        Dymek.podepnij(OPISY["sila"], w2, w3, self.sila_txt)

        e = ttk.Label(f, text="Poziom:")
        e.grid(row=8, column=0, sticky="w")
        self.miernik = tk.Canvas(f, height=16, bg="#202020", highlightthickness=0)
        self.miernik.grid(row=8, column=1, sticky="ew", pady=4)
        self.lampka = tk.Label(f, text="cisza", width=9, bg="#3a3a3a", fg="white")
        self.lampka.grid(row=8, column=2, sticky="w", padx=(8, 0))
        Dymek.podepnij(OPISY["poziom"], e, self.miernik)
        Dymek.podepnij(OPISY["lampka"], self.lampka)

        self.status = ttk.Label(f, text="Zatrzymano", foreground="#555")
        self.status.grid(row=9, column=0, columnspan=3, sticky="w", pady=(4, 6))
        Dymek.podepnij(OPISY["status"], self.status)

        e = ttk.Label(f, text="Aktywność (najnowsze u góry):")
        e.grid(row=10, column=0, columnspan=3, sticky="w")
        ramka = ttk.Frame(f)
        ramka.grid(row=11, column=0, columnspan=3, sticky="nsew")
        f.rowconfigure(11, weight=1)
        self.aktywnosc = tk.Listbox(ramka, height=8)
        pasek = ttk.Scrollbar(ramka, command=self.aktywnosc.yview)
        self.aktywnosc.configure(yscrollcommand=pasek.set)
        self.aktywnosc.pack(side="left", fill="both", expand=True)
        pasek.pack(side="right", fill="y")
        Dymek.podepnij(OPISY["aktywnosc"], e)
        self.aktywnosc.bind("<Button-3>", self._prawy_aktywnosc)
        self.aktywnosc.bind("<Leave>", lambda ev: Dymek.ukryj(), add="+")

        self.sprawdz_godziny()
        self.zmien_odszumianie()                 # napis „100 %” przy sile także zanim okno zacznie słuchać
        self.odswiez()
        self.przelacz()

    def _prawy_aktywnosc(self, event):
        """Prawy na liście aktywności: zdejmuje zaznaczenie, a gdy nic nie jest zaznaczone – pokazuje dymek."""
        if self.aktywnosc.curselection():
            self.aktywnosc.selection_clear(0, "end")
            return "break"
        return Dymek.pokaz(event, OPISY["aktywnosc"])

    def zapisz_ustawienia(self):
        self.s.update(url=self.url.get().strip(), nazwa=self.nazwa.get().strip(),
                      wyjscie=self.wyjscie.get(), glosnosc=self.glosnosc.get(),
                      harmonogram=self.harmonogram.get(), godz_wl=self.godz_wl.get().strip(),
                      godz_wyl=self.godz_wyl.get().strip(), wylacz_ton=self.wylacz_ton.get(),
                      wytnij_ogon=self.wytnij_ogon.get(), ogon_ms=self._ogon_ok,
                      odszumianie=self.odszumianie.get(), sila_odszumiania=self.sila.get(),
                      podtrzymanie=self.podtrzymanie.get(), podtrzymanie_db=self._szum_ok)
        try:
            self.s["geometria"] = self.root.geometry()
        except tk.TclError:
            pass

    def zatrzymaj(self):
        if self.sesja:
            self.sesja.zatrzymaj()
            self.sesja = None

    def pokaz(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def zmien_filtr(self):
        if self.sesja:
            self.sesja.wylacz_ton = self.wylacz_ton.get()

    def zmien_odszumianie(self, *_):
        self.sila_txt.configure(text=f"{self.sila.get()} %")
        if self.sesja:
            self.sesja.odszumianie = self.odszumianie.get()
            self.sesja.sila_odszumiania = self.sila.get() / 100.0

    def biezace_podtrzymanie(self):
        return 10 ** (self._szum_ok / 20) if self.podtrzymanie.get() else 0.0

    def zmien_podtrzymanie(self, *_):
        try:
            db = int(self.szum_db.get())
        except ValueError:
            db = 0
        if -90 <= db <= -30:                     # błędny wpis zostawia poprzedni poziom
            self._szum_ok = db
        if self.sesja:
            self.sesja.podtrzymanie = self.biezace_podtrzymanie()

    def biezacy_ogon(self):
        return self._ogon_ok if self.wytnij_ogon.get() else 0

    def zmien_ogon(self):
        try:
            ms = int(self.ogon_ms.get())
        except ValueError:
            ms = -1
        if 0 <= ms <= 1000:                      # błędny wpis zostawia poprzednią długość
            self._ogon_ok = ms
        if self.sesja:
            self.sesja.ogon_ms = self.biezacy_ogon()

    def ustaw_tytul(self):
        self.s["nazwa"] = self.nazwa.get().strip()
        self.root.title(self.s["nazwa"] or TYTUL)

    def biezaca_glosnosc(self):
        return 0.0 if self.wyciszony or self.mute else self.glosnosc.get() / 100.0

    def przelacz_mute(self):
        self.mute = not self.mute
        tlo, tekst = ("#c62828", "white") if self.mute else self._kolor_mute
        self.przycisk_mute.configure(bg=tlo, fg=tekst, activebackground=tlo, activeforeground=tekst,
                                     relief="sunken" if self.mute else "raised")
        self.zmien_glosnosc()

    def zmien_glosnosc(self, *_):
        self.glosnosc_txt.configure(text=f"{self.glosnosc.get()} %")
        if self.sesja:
            self.sesja.glosnosc = self.biezaca_glosnosc()

    def kolko_glosnosci(self, event):
        """Kółko nad suwakiem: wolne kręcenie po 1 %, szybkie (ząbki co < 50 ms) po 10 %."""
        teraz = time.monotonic()
        dt, self._kolko_czas = teraz - self._kolko_czas, teraz
        krok = 10 if dt < 0.05 else 1
        zabki = max(1, round(abs(event.delta) / 120))
        kierunek = 1 if event.delta > 0 else -1
        self.glosnosc.set(min(200, max(0, self.glosnosc.get() + kierunek * krok * zabki)))
        self.zmien_glosnosc()
        return "break"

    def sprawdz_godziny(self):
        """Poza godzinami aktywności wycisza dźwięk (odbiór i lista aktywności działają dalej)."""
        if self._zegar:
            self.root.after_cancel(self._zegar)
        self.wyciszony = False
        opis, kolor = "", "#555"
        if self.harmonogram.get():
            wl, wyl = parsuj_godzine(self.godz_wl.get()), parsuj_godzine(self.godz_wyl.get())
            if wl is None or wyl is None:
                opis, kolor = "wpisz godziny jako GG:MM", "#c62828"
            else:
                t = time.localtime()
                if w_godzinach(t.tm_hour * 60 + t.tm_min, wl, wyl):
                    opis, kolor = f"włączony do {wyl // 60:02d}:{wyl % 60:02d}", "#2e7d32"
                else:
                    self.wyciszony = True
                    opis, kolor = f"wyciszony do {wl // 60:02d}:{wl % 60:02d}", "#757575"
        self.stan_dzwieku.configure(text=opis, fg=kolor)
        self.zmien_glosnosc()
        self._zegar = self.root.after(1000, self.sprawdz_godziny)

    def przelacz(self):
        if self.sesja:
            self.zatrzymaj()
            self.przycisk.configure(text="▶  Słuchaj")
            self.status.configure(text="Zatrzymano")
            self.ustaw_lampke(False)
            return
        if not self.url.get().strip():
            self.status.configure(text="Wklej adres strumienia (http/https: WAV, MP3, AAC, OGG, HLS, .m3u/.pls) "
                                       "i kliknij „Słuchaj”.")
            return
        wyjscie = self.wyjscie.get()
        self.sesja = Sesja(self.url.get().strip(), None if wyjscie == DOMYSLNE_WYJSCIE else wyjscie,
                           self.biezaca_glosnosc(), self.zdarzenia)
        self.sesja.nazwa = self.s.get("nazwa") or self.sesja.nazwa
        self.sesja.wylacz_ton = self.wylacz_ton.get()
        self.sesja.ogon_ms = self.biezacy_ogon()
        self.sesja.podtrzymanie = self.biezace_podtrzymanie()
        self.zmien_odszumianie()
        self.sesja.start()
        self.przycisk.configure(text="■  Stop")

    def ustaw_lampke(self, nadaje):
        self.lampka.configure(text="NADAJE" if nadaje else "cisza", bg="#c62828" if nadaje else "#3a3a3a")

    def odswiez(self):
        while True:
            try:
                z = self.zdarzenia.get_nowait()
            except queue.Empty:
                break
            if not self.sesja:
                continue
            if z[0] == "status":
                self.status.configure(text=z[1])
            elif z[0] == "wlaczenie":
                self.ustaw_lampke(True)
            elif z[0] == "wylaczenie":
                self.ustaw_lampke(False)
                self.aktywnosc.insert(0, f"{godzina(z[1])} – {godzina(z[2])}   "
                                         f"({max(1, round(z[2] - z[1]))} s)   {z[3]}")
            elif z[0] == "napis":
                self.aktywnosc.insert(0, f"{godzina(z[1])}   ♪ {z[2]}")
                self.aktywnosc.itemconfig(0, foreground="#1565c0")

        teraz = self.sesja.ctcss_teraz if self.sesja else None
        ostatni = self.sesja.ctcss_ostatni if self.sesja else None
        if teraz:
            self.ctcss_txt.configure(text=opis_tonu(teraz), fg="#2e7d32")
        elif teraz == 0.0:
            self.ctcss_txt.configure(text="brak tonu", fg="#e65100")
        elif ostatni:
            self.ctcss_txt.configure(text=f"{opis_tonu(ostatni)} (ostatnio)", fg="#757575")
        else:
            self.ctcss_txt.configure(text="—", fg="#757575")

        odszum = self.sesja.odszum if self.sesja else None
        self.odszum_txt.configure(text="ton – bez odszumiania" if odszum and odszum.ton else "")

        w = self.miernik.winfo_width()
        db = self.sesja.poziom_db if self.sesja else -120.0
        frakcja = min(1.0, max(0.0, (db + 60.0) / 60.0))
        self.miernik.delete("all")
        kolor = "#43a047" if db < -12 else ("#fdd835" if db < -3 else "#e53935")
        self.miernik.create_rectangle(0, 0, int(w * frakcja), 16, fill=kolor, width=0)
        if self.sesja and self.sesja.format:
            self.miernik.create_text(w - 4, 8, anchor="e", fill="#bbb", font=("Segoe UI", 8),
                                     text=f"{db:.0f} dBFS   bufor {self.sesja.bufor_s():.1f} s")
        self.root.after(100, self.odswiez)


class Aplikacja:
    """Lista strumieni w configu + po jednym oknie na otwarty strumień; okna grają niezależnie."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        try:
            self.root.iconbitmap(default=sciezka_zasobu("czajnik.ico"))   # domyślna ikona wszystkich okien
        except tk.TclError:
            pass
        self.wyjscia = wyjscia_audio()
        self.strumienie = self._wczytaj_strumienie(wczytaj_cfg())
        self.okna = []
        try:
            self._podpis_audio = podpis_audio()
        except Exception:
            self._podpis_audio = None
        self._ostatnie_odswiezenie = 0.0
        self.root.after(STRAZNIK_AUDIO_S * 1000, self._pilnuj_audio)
        self.root.after(ZNAK_ZYCIA_S * 1000, self._znak_zycia)
        self.root.report_callback_exception = lambda typ, wart, tb: dziennik(     # błędy w obsłudze okien
            "BŁĄD (okno):\n" + "".join(traceback.format_exception(typ, wart, tb)).rstrip())
        do_otwarcia = [s for s in self.strumienie if s.get("otwarte")] or self.strumienie[:1]
        if not do_otwarcia:
            self.strumienie.append(self._nowy_strumien())
            do_otwarcia = self.strumienie[:1]
        for s in do_otwarcia:
            self.otworz(s)

    @staticmethod
    def _nowy_strumien(**pola):
        return {**DOMYSLNE_USTAWIENIA, "id": uuid.uuid4().hex[:12], **pola}

    def _wczytaj_strumienie(self, cfg):
        lista = cfg.get("strumienie")
        if lista is None:          # config z jednym strumieniem (wcześniejsza wersja) albo pierwsze uruchomienie
            stary = {k: cfg[k] for k in DOMYSLNE_USTAWIENIA if k in cfg}
            if not stary.get("url") and DOMYSLNY_URL:
                stary.update(url=DOMYSLNY_URL, nazwa=stary.get("nazwa") or DOMYSLNA_NAZWA)
            lista = [dict(stary, otwarte=True)] if stary.get("url") else []
        return [self._nowy_strumien(**s) if "id" not in s else {**DOMYSLNE_USTAWIENIA, **s} for s in lista]

    def zapisz(self):
        for okno in self.okna:
            okno.zapisz_ustawienia()
        do_zapisu = [s for s in self.strumienie if s.get("url") or s.get("nazwa")]
        zapisz_cfg({"strumienie": do_zapisu})

    def okno_strumienia(self, s):
        return next((o for o in self.okna if o.s is s), None)

    def otworz(self, s, obok=None):
        okno = self.okno_strumienia(s)
        if okno:
            okno.pokaz()
            return okno
        if obok is not None and not s.get("geometria"):
            s["geometria"] = f"+{obok.root.winfo_rootx() + 40}+{obok.root.winfo_rooty() + 40}"
        s["otwarte"] = True
        okno = Okno(self, s)
        self.okna.append(okno)
        self.zapisz()
        dziennik(f"okno otwarte: {s.get('nazwa') or '(bez nazwy)'}")
        return okno

    def zamknij_okno(self, okno, koniec_programu=True):
        okno.zapisz_ustawienia()
        okno.zatrzymaj()
        dziennik(f"okno zamknięte: {okno.s.get('nazwa') or '(bez nazwy)'}")
        if koniec_programu and len(self.okna) == 1:   # ostatnie okno: wróci przy następnym uruchomieniu
            self.zapisz()
            dziennik("koniec programu (zamknięte ostatnie okno)")
            self.root.destroy()
            return
        okno.s["otwarte"] = False
        self.okna.remove(okno)
        okno.root.destroy()
        self.zapisz()

    def dodaj_strumien(self, okno):
        wynik = dialog_strumienia(okno.root, "Nowy strumień")
        if wynik:
            s = self._nowy_strumien(nazwa=wynik[0], url=wynik[1])
            self.strumienie.append(s)
            self.otworz(s, obok=okno)

    def edytuj(self, s, okno, nad=None):
        wynik = dialog_strumienia(okno.root, "Edycja strumienia", s.get("nazwa", ""), s.get("url", ""),
                                  na_wierzchu=nad is not None)
        if not wynik:
            return
        s.update(nazwa=wynik[0], url=wynik[1])
        otwarte = self.okno_strumienia(s)
        if otwarte:
            gra = otwarte.sesja is not None and otwarte.sesja.url != wynik[1]
            otwarte.nazwa.set(wynik[0])
            otwarte.url.set(wynik[1])
            if gra:                               # nowy adres: połącz od nowa
                otwarte.przelacz()
                otwarte.przelacz()
        self.zapisz()

    def usun(self, s, okno, nad=None):
        if not messagebox.askyesno("Usuń strumień", f"Usunąć „{s.get('nazwa') or '(bez nazwy)'}” z listy?",
                                   parent=nad or okno.root):
            return
        otwarte = self.okno_strumienia(s)
        self.strumienie.remove(s)
        if otwarte:
            if len(self.okna) == 1:               # usuwany był jedyny otwarty: pokaż inny albo pusty
                zastepczy = self.strumienie[0] if self.strumienie else self._nowy_strumien()
                if not self.strumienie:
                    self.strumienie.append(zastepczy)
                self.otworz(zastepczy, obok=otwarte)
            self.zamknij_okno(otwarte, koniec_programu=False)
        self.zapisz()

    def _pilnuj_audio(self):
        """Strażnik wyjścia dźwięku: gdy zmieni się domyślne wyjście lub lista wyjść Windows (np. Pulpit zdalny,
        odłączony głośnik) albo któraś karta przestała brać dźwięk — PortAudio od nowa, strumienie na nowe wyjście."""
        try:
            podpis = podpis_audio()
        except Exception:
            podpis = self._podpis_audio
        teraz = time.monotonic()
        sesje = [o.sesja for o in self.okna if o.sesja and o.sesja.format and not o.sesja.stop.is_set()]
        stoi = any(s.wyjscie is None or (s.ostatni_callback and teraz - s.ostatni_callback > STRUMIEN_STOI_S)
                   for s in sesje)
        if (podpis != self._podpis_audio or stoi) and teraz - self._ostatnie_odswiezenie > 5:
            if podpis != self._podpis_audio:
                stary = self._podpis_audio or (None, ())
                powod = f"zmiana dźwięku Windows: domyślne {stary[0]!r} → {podpis[0]!r}"
                if stary[1] != podpis[1]:
                    powod += (f"; wyjść {len(stary[1])} → {len(podpis[1])}"
                              f" (+{sorted(set(podpis[1]) - set(stary[1]))} −{sorted(set(stary[1]) - set(podpis[1]))})")
            else:
                powod = "karta nie bierze dźwięku: " + ", ".join(
                    f"{s.nazwa} ({'brak strumienia' if s.wyjscie is None else f'{teraz - s.ostatni_callback:.0f} s'})"
                    for s in sesje if s.wyjscie is None or teraz - s.ostatni_callback > STRUMIEN_STOI_S)
            dziennik(f"strażnik dźwięku: {powod} — odświeżam PortAudio ({len(sesje)} strumieni)")
            self._podpis_audio = podpis
            self._ostatnie_odswiezenie = teraz
            self.odswiez_audio(sesje)
        self.root.after(STRAZNIK_AUDIO_S * 1000, self._pilnuj_audio)

    def _znak_zycia(self):
        stany = "; ".join(f"{o.s.get('nazwa') or '?'}: " + (
            "nie słucha" if not o.sesja else
            f"{'gra' if o.sesja.wyjscie else 'BEZ dźwięku'}, {o.status.cget('text')[:40]}") for o in self.okna)
        dziennik(f"działa: {len(self.okna)} okien, {pamiec_mb()} MB | {stany}")
        self.root.after(ZNAK_ZYCIA_S * 1000, self._znak_zycia)

    def odswiez_audio(self, sesje):
        with BLOKADA_AUDIO:
            for s in sesje:
                s._zamknij_wyjscie()
            try:
                sd._terminate()             # PortAudio od nowa: nowa lista urządzeń i nowe domyślne wyjście
                sd._initialize()
            except Exception:
                pass
            for s in sesje:
                try:
                    s.uruchom_strumien()
                except Exception as e:
                    dziennik(f"[{s.nazwa}] wyjście dźwięku niedostępne po odświeżeniu: {type(e).__name__}: {e}")
                    s._status(f"Wyjście dźwięku niedostępne ({e}) — próbuję dalej")
        self.wyjscia = wyjscia_audio()
        for o in self.okna:
            o.wyjscia = self.wyjscia
            o.pole_wyjscia.configure(values=list(self.wyjscia))

    def uruchom(self):
        self.root.mainloop()


if __name__ == "__main__":
    wlacz_dziennik()
    dziennik(f"start programu (PID {os.getpid()}, {sys.executable if getattr(sys, 'frozen', False) else __file__})")
    Aplikacja().uruchom()
