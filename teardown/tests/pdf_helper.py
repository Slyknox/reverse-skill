"""Builds a minimal one-page PDF with given text lines (test helper, no dependencies)."""


def make_pdf(lines: list[str]) -> bytes:
	ops = ["BT", "/F1 12 Tf", "14 TL", "72 720 Td"]
	for line in lines:
		escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
		ops.append(f"({escaped}) Tj T*")
	ops.append("ET")
	stream = "\n".join(ops).encode("latin-1")
	objects = [
		b"<< /Type /Catalog /Pages 2 0 R >>",
		b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
		b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
		b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
		b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
	]
	out = bytearray(b"%PDF-1.4\n")
	offsets = []
	for i, body in enumerate(objects, 1):
		offsets.append(len(out))
		out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
	xref = len(out)
	out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
	out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
	out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
	return bytes(out)


def make_scanned_pdf(lines: list[str]) -> bytes:
	"""A PDF whose only content is an image of the text (no text layer). Needs Pillow."""
	import io

	from PIL import Image, ImageDraw, ImageFont

	font = ImageFont.truetype("DejaVuSans.ttf", 36) if _has_dejavu() else ImageFont.load_default(size=36)
	img = Image.new("L", (1400, 120 + 60 * len(lines)), 255)
	draw = ImageDraw.Draw(img)
	for i, line in enumerate(lines):
		draw.text((60, 60 + 60 * i), line, fill=0, font=font)
	buf = io.BytesIO()
	img.save(buf, format="PDF", resolution=150)
	return buf.getvalue()


def _has_dejavu() -> bool:
	from PIL import ImageFont

	try:
		ImageFont.truetype("DejaVuSans.ttf", 10)
		return True
	except OSError:
		return False
