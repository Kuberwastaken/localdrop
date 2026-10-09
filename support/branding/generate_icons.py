"""Generate LocalDrop's platform icons from the same vector geometry.

Requires Pillow. Run from any directory: python support/branding/generate_icons.py
The SVG is the editable master; the drawing below mirrors its four cubic curves.
"""
from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[2]
TEAL = '#16665C'
WHITE = '#FFFFFF'


def curve(points):
    return [tuple(sum(((1-t)**3, 3*(1-t)**2*t, 3*(1-t)*t*t, t**3)[j]*points[j][i]
                      for j in range(4)) for i in range(2))
            for t in [k/100 for k in range(101)]]


def icon(size, tile=True, ink=WHITE, inset=0):
    scale = 4
    canvas = Image.new('RGBA', (size*scale, size*scale))
    draw = ImageDraw.Draw(canvas)
    factor = size*scale/512
    def xy(p):
        return ((p[0]*(1-2*inset)+512*inset)*factor,
                (p[1]*(1-2*inset)+512*inset)*factor)
    if tile:
        draw.rounded_rectangle((0, 0, size*scale-1, size*scale-1), radius=112*factor, fill=TEAL)
    outline = (curve([(256, 74), (220, 117), (112, 219), (112, 302)])
               + curve([(112, 302), (112, 381), (176, 438), (256, 438)])
               + curve([(256, 438), (336, 438), (400, 381), (400, 302)])
               + curve([(400, 302), (400, 219), (292, 117), (256, 74)]))
    # Mask keeps the opposing arrows transparent in monochrome tray variants.
    mask = Image.new('L', canvas.size)
    md = ImageDraw.Draw(mask)
    md.polygon([xy(p) for p in outline], fill=255)
    for arrow in [[(172, 251), (290, 251), (290, 220), (342, 270), (290, 320), (290, 289), (172, 289)],
                  [(340, 331), (222, 331), (222, 300), (170, 350), (222, 400), (222, 369), (340, 369)]]:
        md.polygon([xy(p) for p in arrow], fill=0)
    canvas.alpha_composite(Image.composite(Image.new('RGBA', canvas.size, ink),
                                           Image.new('RGBA', canvas.size), mask))
    return canvas.resize((size, size), Image.Resampling.LANCZOS)


def save(path, size, **kwargs):
    path.parent.mkdir(parents=True, exist_ok=True)
    icon(size, **kwargs).save(path)


def main():
    assets = ROOT/'app/assets/img'
    (ROOT/'app/android/app/src/main/res/values/ic_launcher_background.xml').write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<resources>\n'
        f'    <color name="ic_launcher_background">{TEAL}</color>\n</resources>\n', encoding='utf-8')
    for size in [32, 128, 256, 512]:
        save(assets/f'logo-{size}.png', size)
    for size, color in [(32, WHITE), (512, WHITE), (32, '#000000')]:
        save(assets/f'logo-{size}-{"white" if color == WHITE else "black"}.png', size, tile=False, ink=color)
    for target in [assets/'logo.ico', ROOT/'app/windows/runner/resources/app_icon.ico']:
        icon(256).save(target, sizes=[(s, s) for s in [16, 24, 32, 48, 64, 128, 256]])
    for target in (ROOT/'app/ios/Runner/Assets.xcassets/AppIcon.appiconset').glob('*.png'):
        with Image.open(target) as old:
            size = old.width
        icon(size).convert('RGB').save(target)
    for target in (ROOT/'app/macos/Runner/Assets.xcassets').rglob('*.png'):
        with Image.open(target) as old:
            size = old.width
        save(target, size, tile='StatusBar' not in str(target), ink='#000000' if 'StatusBar' in str(target) else WHITE)
    icon(1024).save(ROOT/'app/macos/ShareExtension/icon.icns')
    for target in (ROOT/'app/android/app/src/main/res').rglob('ic_launcher*.png'):
        with Image.open(target) as old:
            size = old.width
        flat = 'monochrome' in target.name or 'quicktile' in target.name
        save(target, size, tile=not flat and 'foreground' not in target.name, inset=.18 if 'foreground' in target.name else 0)
    save(ROOT/'app/android/app/src/main/ic_launcher-playstore.png', 512)
    for target in (ROOT/'app/web').rglob('*.png'):
        with Image.open(target) as old:
            size = old.width
        save(target, size)
    save(ROOT/'fastlane/metadata/android/en-US/images/icon.png', 512)
    save(ROOT/'docs/localdrop-icon.png', 512)


if __name__ == '__main__':
    main()
