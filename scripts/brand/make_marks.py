"""Build three Packstage wordmark concepts as outlined SVG paths.

Setup (run inside scripts/brand/):
    npm i @fontsource-variable/archivo
    pip install fonttools brotli cairosvg
Usage:
    python make_marks.py ../../static/brand
"""
import sys
from pathlib import Path
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
import cairosvg

HERE = Path(__file__).parent
OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
SRC = HERE / "node_modules/@fontsource-variable/archivo/files/archivo-latin-wdth-normal.woff2"

INK = "#22252B"      # Gaffer
COBALT = "#2B46D6"   # Backdrop cobalt
SPOT = "#FFD447"     # Spotlight


def instance(wdth, wght):
    font = TTFont(SRC)
    return instantiateVariableFont(font, {"wdth": wdth, "wght": wght})


def text_path(font, text, size, x=0, y=0, tracking=0):
    """Return (svg path data, advance width) for text at baseline y."""
    gs = font.getGlyphSet()
    cmap = font.getBestCmap()
    upm = font["head"].unitsPerEm
    scale = size / upm
    pen = SVGPathPen(gs)
    cx = x
    for ch in text:
        name = cmap[ord(ch)]
        tp = TransformPen(pen, (scale, 0, 0, -scale, cx, y))
        gs[name].draw(tp)
        cx += gs[name].width * scale + tracking
    return pen.getCommands(), cx - x - tracking


def svg(w, h, body, bg=None):
    back = f'<rect width="{w}" height="{h}" fill="{bg}"/>' if bg else ""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
            f'width="{w}" height="{h}" role="img" aria-label="Packstage">{back}{body}</svg>')


def save(name, w, h, body):
    for suffix, bg in (("", None), ("-preview", "#FBFBFA")):
        s = svg(w, h, body, bg)
        if suffix == "":
            (OUT / f"{name}.svg").write_text(s)
        else:
            cairosvg.svg2png(bytestring=s.encode(), write_to=str(OUT / f"{name}{suffix}.png"),
                             output_width=w * 2)


wide = instance(125, 760)
regular = instance(100, 520)

# A. Spotlight: the word sits on a stage-floor ellipse, the dot of light under "pack".
d, adv = text_path(wide, "packstage", 64, x=40, y=96, tracking=-1)
w = int(adv + 80)
body = (f'<ellipse cx="{40 + adv * 0.24:.1f}" cy="114" rx="{adv * 0.25:.1f}" ry="8" fill="{SPOT}"/>'
        f'<path d="{d}" fill="{INK}"/>')
save("wordmark-a-spotlight", w, 146, body)

# B. Sweep: a seamless-paper curve (the cyclorama) rises behind the word.
d, adv = text_path(wide, "packstage", 64, x=64, y=108, tracking=-1)
w = int(adv + 104)
sweep = (f'<path d="M40 30 V100 Q40 124 64 124 H{w - 40}" fill="none" stroke="{COBALT}" '
         f'stroke-width="7" stroke-linecap="round"/>')
save("wordmark-b-sweep", w, 156, sweep + f'<path d="{d}" fill="{INK}"/>')

# C. Pack box: "pack" sits inside a cobalt carton, "stage" stands beside it.
d1, a1 = text_path(wide, "pack", 60, x=56, y=96, tracking=-1)
d2, a2 = text_path(regular, "stage", 60, x=56 + a1 + 40, y=96, tracking=-0.5)
box_w = a1 + 32
w = int(56 + a1 + 40 + a2 + 40)
body = (f'<rect x="40" y="30" width="{box_w:.1f}" height="88" rx="6" fill="{COBALT}"/>'
        f'<path d="{d1}" fill="#FFFFFF"/><path d="{d2}" fill="{INK}"/>')
save("wordmark-c-carton", w, 148, body)

print("done", sorted(p.name for p in OUT.iterdir()))
