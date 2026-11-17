"""Builds clipper/web/clipper.ico (coral tile, white crop marks around a play symbol)."""
from pathlib import Path
from PIL import Image, ImageDraw

S = 1024  # draw big, downscale for smooth edges
grad = Image.new("RGBA", (S, S))
px = grad.load()
a, b = (255, 77, 94), (255, 122, 89)
for y in range(S):
    for x in range(S):
        t = (x + y) / (2 * S)
        px[x, y] = tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3)) + (255,)
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle((32, 32, S - 32, S - 32), radius=230, fill=255)
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
img.paste(grad, (0, 0), mask)
d = ImageDraw.Draw(img)
w = 58  # crop-mark corners framing a small play symbol = "a clip of a video"
L = 190
for (x, y, dx, dy) in [(200, 200, 1, 1), (824, 200, -1, 1), (200, 824, 1, -1), (824, 824, -1, -1)]:
    d.rounded_rectangle(sorted([x, x + dx * L])[:1] + sorted([y, y + dy * w])[:1] + sorted([x, x + dx * L])[1:] + sorted([y, y + dy * w])[1:], radius=22, fill="white")
    d.rounded_rectangle(sorted([x, x + dx * w])[:1] + sorted([y, y + dy * L])[:1] + sorted([x, x + dx * w])[1:] + sorted([y, y + dy * L])[1:], radius=22, fill="white")
d.polygon([(430, 360), (430, 664), (680, 512)], fill="white")
out = Path(__file__).parent / "clipper" / "web" / "clipper.ico"
img.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
img.resize((256, 256), Image.LANCZOS).save(out.with_suffix(".png"))
print("wrote", out)
