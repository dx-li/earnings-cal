"""Generate icon.ico: stylized calendar with upward bar-chart bars."""
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path


def draw_icon(size: int) -> Image.Image:
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    pad = max(1, s // 16)
    r = max(2, s // 10)  # corner radius
    bg = (30, 41, 59)      # slate-800
    accent = (110, 168, 254)  # blue accent (matches UI)
    green = (93, 211, 158)
    yellow = (255, 209, 102)
    red = (255, 107, 107)

    # rounded rect background
    d.rounded_rectangle((pad, pad, s - pad, s - pad), radius=r, fill=bg)

    # calendar header strip
    header_h = max(3, s // 5)
    d.rounded_rectangle(
        (pad, pad, s - pad, pad + header_h),
        radius=r, fill=accent,
    )
    # square off the bottom of the header
    d.rectangle((pad, pad + header_h - r, s - pad, pad + header_h), fill=accent)

    # binder rings
    ring_r = max(1, s // 32)
    ring_y = pad + header_h // 2
    for x_frac in (0.28, 0.72):
        cx = int(s * x_frac)
        d.ellipse(
            (cx - ring_r, ring_y - ring_r, cx + ring_r, ring_y + ring_r),
            fill=bg,
        )

    # bar chart bars (ascending) in the body
    body_top = pad + header_h + max(2, s // 20)
    body_bot = s - pad - max(2, s // 14)
    body_left = pad + max(2, s // 10)
    body_right = s - pad - max(2, s // 10)
    width = body_right - body_left
    n_bars = 4
    gap = max(1, s // 48)
    bar_w = (width - gap * (n_bars - 1)) // n_bars
    heights = [0.35, 0.55, 0.75, 1.0]
    colors = [accent, accent, yellow, green]
    full_h = body_bot - body_top
    for i in range(n_bars):
        x0 = body_left + i * (bar_w + gap)
        bh = int(full_h * heights[i])
        y0 = body_bot - bh
        br = max(1, s // 48)
        d.rounded_rectangle(
            (x0, y0, x0 + bar_w, body_bot),
            radius=br, fill=colors[i],
        )

    return img


def main() -> None:
    assets = Path(__file__).resolve().parents[1] / "src" / "earnings_cal" / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    # Render each size natively for crispness, then pass all as append_images
    rendered = [draw_icon(s) for s in sizes]
    biggest = rendered[-1]
    biggest.save(
        assets / "icon.ico",
        format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=rendered[:-1],
    )
    draw_icon(512).save(assets / "icon.png", format="PNG")
    print(f"wrote {assets / 'icon.ico'}, {assets / 'icon.png'}")


if __name__ == "__main__":
    main()
