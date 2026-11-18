"""A page of a PDF, drawn by Android's own PdfRenderer, behind the few calls of pypdfium2 that the engine makes.

pypdfium2 has no build for Android (pip finds only its source, which does not build there). The engine draws a page in
one place, server.py render_page:

    pdf = pypdfium2.PdfDocument(path)
    page = pdf[n - 1]
    bitmap = page.render(scale=width / max(page.get_size()), rev_byteorder=True)
    png_bytes(bitmap.to_numpy())            # rows of pixels, red, green and blue first
    pdf.close()

and holds a lock while it does, so one page is open at a time, which is all PdfRenderer allows. Only that much is here.
Reading a PDF's words (pypdfium2's other use, when a library is first indexed) is not: a library comes with its words.
"""
import numpy
from java import jclass, jint

_File = jclass('java.io.File')
_Descriptor = jclass('android.os.ParcelFileDescriptor')
_Renderer = jclass('android.graphics.pdf.PdfRenderer')
_Page = jclass('android.graphics.pdf.PdfRenderer$Page')
_Bitmap = jclass('android.graphics.Bitmap')
_Config = jclass('android.graphics.Bitmap$Config')
_Buffer = jclass('java.nio.ByteBuffer')
# 0xFFFFFFFF as Java's int: a page is drawn on white, as paper. Said to be an int: given a bare number, the call goes to
# eraseColor(long), which reads it as a colour in a colour space that does not exist (found by running it).
WHITE = jint(-1)


class _Drawn:
    def __init__(self, pixels):
        self._pixels = pixels

    def to_numpy(self):
        return self._pixels


class _OnePage:
    def __init__(self, renderer, index):
        self._renderer, self._index = renderer, index

    def get_size(self):
        """(width, height) in points, a 72nd of an inch."""
        page = self._renderer.openPage(self._index)
        try:
            return float(page.getWidth()), float(page.getHeight())
        finally:
            page.close()

    def render(self, scale=1, rev_byteorder=True, **more):
        page = self._renderer.openPage(self._index)
        try:
            w, h = max(1, int(page.getWidth() * scale)), max(1, int(page.getHeight() * scale))
            bitmap = _Bitmap.createBitmap(w, h, _Config.ARGB_8888)
            bitmap.eraseColor(WHITE)
            page.render(bitmap, None, None, _Page.RENDER_MODE_FOR_DISPLAY)
        finally:
            page.close()
        try:
            held = _Buffer.allocate(w * h * 4)
            bitmap.copyPixelsToBuffer(held)         # ARGB_8888 is kept as red, green, blue, alpha, a byte each
            pixels = numpy.frombuffer(bytes(held.array()), dtype=numpy.uint8).reshape(h, w, 4)
        finally:
            bitmap.recycle()
        return _Drawn(pixels)


class PdfDocument:
    def __init__(self, path):
        self._descriptor = _Descriptor.open(_File(str(path)), _Descriptor.MODE_READ_ONLY)
        try:
            self._renderer = _Renderer(self._descriptor)
        except Exception:
            self._descriptor.close()
            raise

    def __len__(self):
        return self._renderer.getPageCount()

    def __getitem__(self, index):
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        return _OnePage(self._renderer, index)

    def close(self):
        try:
            self._renderer.close()
        finally:
            self._descriptor.close()
