"""Create deterministic native application icons from simple vector-like geometry."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


def main() -> None:
    target = Path("build-assets")
    target.mkdir(parents=True, exist_ok=True)
    size = 1024
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((72, 72, 952, 952), radius=210, fill="#176B4B")
    draw.line((270, 710, 270, 322, 512, 590, 754, 322, 754, 710), fill="white", width=86)
    draw.ellipse((704, 690, 800, 786), fill="#9ED0B5")
    image.save(target / "matelab-connector.png")
    image.save(
        target / "matelab-connector.ico",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    image.save(target / "matelab-connector.icns")


if __name__ == "__main__":
    main()
