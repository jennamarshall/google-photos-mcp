import io

from PIL import Image


def make_jpeg(size=(64, 48), color=(200, 80, 40), taken=None, gps=None, make=None, model=None) -> bytes:
    im = Image.new("RGB", size, color)
    exif = Image.Exif()
    if make:
        exif[0x010F] = make
    if model:
        exif[0x0110] = model
    if taken:
        exif.get_ifd(0x8769)[0x9003] = taken  # DateTimeOriginal "YYYY:MM:DD HH:MM:SS"
    if gps:
        def dms(v):
            v = abs(v)
            d = int(v)
            m = int((v - d) * 60)
            s = round(((v - d) * 60 - m) * 60, 4)
            return (float(d), float(m), float(s))
        g = exif.get_ifd(0x8825)
        g[1] = "N" if gps[0] >= 0 else "S"
        g[2] = dms(gps[0])
        g[3] = "E" if gps[1] >= 0 else "W"
        g[4] = dms(gps[1])
    out = io.BytesIO()
    im.save(out, "JPEG", exif=exif)
    return out.getvalue()
