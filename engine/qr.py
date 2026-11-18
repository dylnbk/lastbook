"""A QR code for the link a phone opens to reach this library, drawn here: no library, no network, no image file.

  matrix(text)  ->  rows of booleans, True for a dark module, with no quiet zone
  svg(text)     ->  an SVG string with the quiet zone, black on white

Byte mode, error correction M (L when the text is too long for M), versions 1 to 10: up to 213 bytes at M and 271 at L,
which is several times the length of a link. The layout follows ISO/IEC 18004: finder, timing and alignment patterns,
format and version information, the zigzag data path, and the mask with the lowest penalty.
"""

# Per version 1..10: error-correction codewords per block, and the number of blocks.
ECC_PER_BLOCK = {'L': (7, 10, 15, 20, 26, 18, 20, 24, 30, 18), 'M': (10, 16, 26, 18, 24, 16, 18, 22, 22, 26)}
BLOCKS = {'L': (1, 1, 1, 1, 1, 2, 2, 2, 2, 4), 'M': (1, 1, 1, 2, 2, 4, 4, 4, 5, 5)}
FORMAT_BITS = {'L': 1, 'M': 0}
MAX_VERSION = 10


class TooLong(ValueError):
    pass


def _raw_modules(ver):
    """Modules left for data and error correction once the function patterns are taken out."""
    n = (16 * ver + 128) * ver + 64
    if ver >= 2:
        align = ver // 7 + 2
        n -= (25 * align - 10) * align - 55
        if ver >= 7:
            n -= 36
    return n


def _data_codewords(ver, level):
    return _raw_modules(ver) // 8 - ECC_PER_BLOCK[level][ver - 1] * BLOCKS[level][ver - 1]


def _mul(x, y):
    """Product in GF(2^8) modulo x^8 + x^4 + x^3 + x^2 + 1."""
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _mul(root, 2)
    return result


def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _mul(coef, factor)
    return result


def _codewords(data, ver, level):
    """The data bytes as the final sequence of codewords: mode and length, padding, error correction, interleaved."""
    capacity = _data_codewords(ver, level)
    bits = [0, 1, 0, 0]                                                   # byte mode
    count = 8 if ver <= 9 else 16
    bits += [(len(data) >> i) & 1 for i in range(count - 1, -1, -1)]
    for b in data:
        bits += [(b >> i) & 1 for i in range(7, -1, -1)]
    bits += [0] * min(4, capacity * 8 - len(bits))                        # terminator
    bits += [0] * (-len(bits) % 8)
    words = [int(''.join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = (0xEC, 0x11)
    words += [pad[i % 2] for i in range(capacity - len(words))]

    blocks, ecc_len = BLOCKS[level][ver - 1], ECC_PER_BLOCK[level][ver - 1]
    total = _raw_modules(ver) // 8
    short = blocks - total % blocks                                       # blocks of the shorter length come first
    short_len = total // blocks - ecc_len
    divisor = _rs_divisor(ecc_len)
    parts, at = [], 0
    for i in range(blocks):
        n = short_len + (0 if i < short else 1)
        d = words[at:at + n]
        at += n
        parts.append((d, _rs_remainder(d, divisor)))
    out = []
    for i in range(short_len + 1):
        out += [d[i] for d, _ in parts if i < len(d)]
    for i in range(ecc_len):
        out += [e[i] for _, e in parts]
    return out


def _alignment(ver):
    if ver == 1:
        return []
    n = ver // 7 + 2
    size = ver * 4 + 17
    step = (ver * 4 + n * 2 + 1) // (n * 2 - 2) * 2
    return [6] + [size - 7 - i * step for i in range(n - 2, -1, -1)]


class _Grid:
    def __init__(self, ver):
        self.ver, self.size = ver, ver * 4 + 17
        self.dark = [[False] * self.size for _ in range(self.size)]       # [y][x]
        self.fixed = [[False] * self.size for _ in range(self.size)]

    def put(self, x, y, on):
        self.dark[y][x], self.fixed[y][x] = bool(on), True

    def patterns(self):
        size = self.size
        for i in range(size):
            self.put(6, i, i % 2 == 0)
            self.put(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < size and 0 <= y < size:
                        self.put(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = _alignment(self.ver)
        last = len(pos) - 1
        for i, cx in enumerate(pos):
            for j, cy in enumerate(pos):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.put(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)
        self.format(0, 'M')                                               # reserves the modules; the real bits come with the mask
        if self.ver >= 7:
            rem = self.ver
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.ver << 12 | rem
            for i in range(18):
                bit = (bits >> i) & 1
                a, b = size - 11 + i % 3, i // 3
                self.put(a, b, bit)
                self.put(b, a, bit)

    def format(self, mask, level):
        data = FORMAT_BITS[level] << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        bit = lambda i: (bits >> i) & 1
        size = self.size
        for i in range(6):
            self.put(8, i, bit(i))
        self.put(8, 7, bit(6))
        self.put(8, 8, bit(7))
        self.put(7, 8, bit(8))
        for i in range(9, 15):
            self.put(14 - i, 8, bit(i))
        for i in range(8):
            self.put(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.put(8, size - 15 + i, bit(i))
        self.put(8, size - 8, True)

    def data(self, words):
        size, i, n = self.size, 0, len(words) * 8
        right = size - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(size):
                for j in range(2):
                    x = right - j
                    y = size - 1 - vert if ((right + 1) & 2) == 0 else vert
                    if not self.fixed[y][x] and i < n:
                        self.dark[y][x] = bool((words[i >> 3] >> (7 - (i & 7))) & 1)
                        i += 1
            right -= 2

    def mask(self, m):
        f = (lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0, lambda x, y: (x + y) % 3 == 0,
             lambda x, y: (x // 3 + y // 2) % 2 == 0, lambda x, y: x * y % 2 + x * y % 3 == 0,
             lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0, lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0)[m]
        for y in range(self.size):
            for x in range(self.size):
                if not self.fixed[y][x] and f(x, y):
                    self.dark[y][x] = not self.dark[y][x]

    def penalty(self):
        size, d, total = self.size, self.dark, 0
        lines = [row for row in d] + [[d[y][x] for y in range(size)] for x in range(size)]
        for line in lines:
            run, colour = 0, None
            for v in line:                                                # five or more alike in a row
                if v == colour:
                    run += 1
                    if run == 5:
                        total += 3
                    elif run > 5:
                        total += 1
                else:
                    colour, run = v, 1
            s = ''.join('1' if v else '0' for v in line)                  # what a finder pattern looks like from the side
            for i in range(size - 10):
                if s[i:i + 11] in ('10111010000', '00001011101'):
                    total += 40
        for y in range(size - 1):                                         # 2 by 2 blocks of one colour
            for x in range(size - 1):
                if d[y][x] == d[y][x + 1] == d[y + 1][x] == d[y + 1][x + 1]:
                    total += 3
        dark = sum(v for row in d for v in row)                           # how far from half dark
        total += (abs(dark * 20 - size * size * 10) + size * size - 1) // (size * size) * 10 - 10 if dark * 2 != size * size else 0
        return max(total, 0)


def matrix(text):
    data = text.encode('utf-8') if isinstance(text, str) else bytes(text)
    for level in ('M', 'L'):
        for ver in range(1, MAX_VERSION + 1):
            if 4 + (8 if ver <= 9 else 16) + 8 * len(data) <= _data_codewords(ver, level) * 8:
                break
        else:
            continue
        break
    else:
        raise TooLong(f'{len(data)} bytes is more than a version {MAX_VERSION} code holds')
    words = _codewords(data, ver, level)
    best = None
    for m in range(8):
        g = _Grid(ver)
        g.patterns()
        g.data(words)
        g.mask(m)
        g.format(m, level)
        p = g.penalty()
        if best is None or p < best[0]:
            best = (p, g)
    return best[1].dark


def svg(text, quiet=4, size=None):
    """One path of dark modules on a white square. size: the width and height attributes in pixels (None: scales to its box)."""
    rows = matrix(text)
    n = len(rows) + 2 * quiet
    d = []
    for y, row in enumerate(rows):
        x = 0
        while x < len(row):
            if row[x]:
                start = x
                while x < len(row) and row[x]:
                    x += 1
                d.append(f'M{start + quiet} {y + quiet}h{x - start}v1h-{x - start}z')
            else:
                x += 1
    dims = f' width="{size}" height="{size}"' if size else ''
    # Its own colours, as styles: a page's rule for its icons (stroke: currentColor, fill: none, a stroke two units wide) would
    # otherwise reach in, and two units here is two whole modules. Black on white whatever the page's theme, so a camera reads it.
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}"{dims} shape-rendering="crispEdges" role="img" aria-label="QR code" style="stroke:none;fill:none">'
            f'<rect width="{n}" height="{n}" style="fill:#fff;stroke:none"/><path style="fill:#000;stroke:none" d="{"".join(d)}"/></svg>')
