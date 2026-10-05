"""Download the open-license handwriting fonts used by Inkyfy AI.
Run once: python static/fonts/download_fonts.py
"""
import pathlib
import urllib.request

BASE = "https://raw.githubusercontent.com/google/fonts/main/"
FILES = [
    ("ofl/patrickhand/PatrickHand-Regular.ttf", "PatrickHand-Regular.ttf"),
    ("ofl/homemadeapple/HomemadeApple-Regular.ttf", "HomemadeApple-Regular.ttf"),
    ("ofl/indieflower/IndieFlower-Regular.ttf", "IndieFlower-Regular.ttf"),
    ("ofl/sacramento/Sacramento-Regular.ttf", "Sacramento-Regular.ttf"),
    ("ofl/architectsdaughter/ArchitectsDaughter-Regular.ttf", "ArchitectsDaughter-Regular.ttf"),
    ("ofl/handlee/Handlee-Regular.ttf", "Handlee-Regular.ttf"),
    ("ofl/kalam/Kalam-Regular.ttf", "Kalam-Regular.ttf"),
    ("ofl/nothingyoucoulddo/NothingYouCouldDo.ttf", "NothingYouCouldDo.ttf"),
    ("ofl/labelleaurore/LaBelleAurore-Regular.ttf", "LaBelleAurore.ttf"),
    ("ofl/zeyada/Zeyada-Regular.ttf", "Zeyada.ttf"),
    ("ofl/waitingforthesunrise/WaitingfortheSunrise-Regular.ttf", "WaitingfortheSunrise.ttf"),
    ("ofl/shadowsintolight/ShadowsIntoLight-Regular.ttf", "ShadowsIntoLight.ttf"),
    ("ofl/reeniebeanie/ReenieBeanie-Regular.ttf", "ReenieBeanie.ttf"),
    ("ofl/schoolbell/Schoolbell-Regular.ttf", "Schoolbell-Regular.ttf"),
    ("ofl/coveredbyyourgrace/CoveredByYourGrace.ttf", "CoveredByYourGrace.ttf"),
    ("ofl/nanumpenscript/NanumPenScript-Regular.ttf", "NanumPenScript-Regular.ttf"),
    # Caveat is published as a variable TTF in the current Google Fonts repo.
    ("ofl/caveat/Caveat%5Bwght%5D.ttf", "Caveat-Regular.ttf"),
    ("ofl/caveatbrush/CaveatBrush-Regular.ttf", "CaveatBrush-Regular.ttf"),
]

here = pathlib.Path(__file__).parent
for rel, filename in FILES:
    dest = here / filename
    if dest.exists():
        continue
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        urllib.request.urlretrieve(BASE + rel, tmp)
        if tmp.stat().st_size > 1000:
            tmp.replace(dest)
            print("downloaded", dest.name)
        else:
            tmp.unlink(missing_ok=True)
            print("FAILED (empty)", rel)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        print("FAILED", rel, "-", exc)
