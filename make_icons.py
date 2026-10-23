"""Turns icon.png into icon.ico (Windows) and icon.icns (macOS) for the build."""
from PIL import Image

img = Image.open("icon.png").convert("RGBA")
side = max(img.size)
canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
big = canvas.resize((256, 256), Image.LANCZOS)   # smooth upscale for the icon files
big.save("icon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
big.resize((512, 512), Image.LANCZOS).save("icon.icns")
print("Wrote icon.ico and icon.icns")
