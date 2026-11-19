"""Ikona programu: czajnik. Rysuje w 1024 px i zapisuje czajnik.ico (16-256 px) + podgląd PNG.
Użycie: py narzedzia/ikona_czajnik.py  (pliki lądują w katalogu projektu)."""
import os

from PIL import Image, ImageChops, ImageDraw, ImageFilter

S = 1024
KATALOG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

KORPUS = (211, 47, 47, 255)        # czerwona emalia
KORPUS_CIEN = (154, 26, 26, 255)
CIEMNY = (12, 12, 12, 255)         # rączka, gałka, podstawa — czarne
BLASK = (255, 255, 255, 150)
PARA = (190, 200, 210, 230)


def maska(rysuj):
    m = Image.new("L", (S, S), 0)
    rysuj(ImageDraw.Draw(m))
    return m


def czajnik(z_para=True):
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # rączka: gruby łuk nad korpusem, końce schodzą do boków (korpus rysowany później je przykrywa)
    d.arc((245, 175, 779, 700), start=160, end=380, fill=CIEMNY, width=62)

    # dzióbek: z prawego boku korpusu skośnie w górę
    d.polygon([(700, 610), (905, 430), (975, 405), (985, 455), (950, 470), (790, 790)], fill=KORPUS_CIEN)
    d.ellipse((935, 395, 995, 465), fill=KORPUS_CIEN)

    # korpus: dolna część elipsy ścięta płasko u dołu
    korpus = maska(lambda m: m.ellipse((150, 400, 874, 1010), fill=255))
    korpus = ImageChops.multiply(korpus, maska(lambda m: m.rectangle((0, 0, S, 880), fill=255)))
    img.paste(KORPUS, (0, 0), korpus)

    # cień po prawej stronie korpusu
    cien = ImageChops.multiply(korpus, maska(lambda m: m.ellipse((420, 330, 1060, 1100), fill=255)))
    cien = ImageChops.subtract(cien, maska(lambda m: m.ellipse((300, 300, 900, 1000), fill=255)))
    img.paste(KORPUS_CIEN, (0, 0), cien)

    # blask po lewej
    blask = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(blask).arc((240, 470, 640, 900), start=150, end=215, fill=BLASK, width=46)
    img.alpha_composite(blask.filter(ImageFilter.GaussianBlur(6)))

    # pokrywka i gałka
    d.chord((365, 355, 659, 520), start=180, end=360, fill=KORPUS_CIEN)
    d.rounded_rectangle((350, 425, 674, 462), radius=18, fill=CIEMNY)
    d.ellipse((470, 300, 554, 384), fill=CIEMNY)

    # podstawa
    d.rounded_rectangle((215, 862, 809, 922), radius=30, fill=CIEMNY)

    if z_para:                       # para nad dzióbkiem (tylko większe rozmiary — w małych byłaby kreską)
        for dx in (0, 70):
            pts = [(905 + dx + 22 * ((-1) ** i), 330 - 45 * i) for i in range(6)]
            d.line(pts, fill=PARA, width=26, joint="curve")
    return img


def main():
    duze = czajnik(True)
    male = czajnik(False)
    rozmiary = [16, 24, 32, 48, 64, 128, 256]
    obrazy = [(male if r <= 32 else duze).resize((r, r), Image.LANCZOS) for r in rozmiary]
    ico = os.path.join(KATALOG, "czajnik.ico")
    obrazy[-1].save(ico, format="ICO", sizes=[(r, r) for r in rozmiary], append_images=obrazy[:-1])
    podglad = Image.new("RGBA", (256 + 128 + 64 + 48 + 32 + 16 + 70, 256), (240, 240, 240, 255))
    x = 0
    for r, o in zip(reversed(rozmiary), reversed(obrazy)):
        podglad.alpha_composite(o, (x, 256 - r))
        x += r + 10
    podglad.save(os.path.join(KATALOG, "czajnik_podglad.png"))
    print("zapisano:", ico)


if __name__ == "__main__":
    main()
