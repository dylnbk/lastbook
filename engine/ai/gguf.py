"""What a GGUF model file says about itself, read from its first kilobytes, and how much memory it will need.

A GGUF file opens with its metadata: general.* (name, architecture, size), then the architecture's own numbers (layers,
width, attention heads, the context it was trained for), then the tokenizer, whose word lists run to megabytes. What
decides whether a model fits a computer is all in the first part, so 64 KB is enough to judge a file that is still on
a server: read() takes as much as it is given and stops, without complaint, where the bytes run out.

  read(data)                 -> {'version', 'tensors', 'keys', 'meta': {...}, 'complete': bool}
  describe(meta, size)       -> the few facts the app shows: name, architecture, size label, quantisation, kind
  memory(meta, size, ctx)    -> bytes: weights + the context's own memory + working room
  best_context(meta, size, room, extra)  -> the largest context that fits in `room` bytes, or None
"""
import re
import struct

MAGIC = b'GGUF'
HEAD = 64 * 1024                    # enough of a remote file to judge it
SCALAR = {0: 'B', 1: 'b', 2: 'H', 3: 'h', 4: 'I', 5: 'i', 6: 'f', 7: '?', 10: 'Q', 11: 'q', 12: 'd'}
STRING, ARRAY = 8, 9
KEEP_ARRAY = 256                    # items of an array kept (a number per layer is the longest wanted); the rest are counted and passed over
CONTEXTS = (16384, 12288, 8192, 6144)       # what the assistant can work in, best first. Its prompt with sources needs about 6,000 tokens
WORKING = 600 * 2**20               # llama.cpp's compute buffers and the program itself, beyond weights and context
# general.file_type, as llama.cpp numbers it: the name people know a quantisation by
FILE_TYPE = {0: 'F32', 1: 'F16', 2: 'Q4_0', 3: 'Q4_1', 7: 'Q8_0', 8: 'Q5_0', 9: 'Q5_1', 10: 'Q2_K', 11: 'Q3_K_S', 12: 'Q3_K_M', 13: 'Q3_K_L',
             14: 'Q4_K_S', 15: 'Q4_K_M', 16: 'Q5_K_S', 17: 'Q5_K_M', 18: 'Q6_K', 19: 'IQ2_XXS', 20: 'IQ2_XS', 21: 'Q2_K_S', 22: 'IQ3_XS',
             23: 'IQ3_XXS', 24: 'IQ1_S', 25: 'IQ4_NL', 26: 'IQ3_S', 27: 'IQ3_M', 28: 'IQ2_S', 29: 'IQ2_M', 30: 'IQ4_XS', 31: 'IQ1_M', 32: 'BF16'}
QUANT = re.compile(r'(?<![A-Za-z0-9])((?:UD-)?(?:IQ|Q)\d(?:_[A-Z0-9]+)*|BF16|F16|F32|FP16)(?![A-Za-z0-9])', re.I)
SHARD = re.compile(r'-(\d{5})-of-(\d{5})\.gguf$', re.I)


class NotGGUF(ValueError):
    pass


def read(data):
    """The metadata in `data` (the start of a file). Raises NotGGUF if it is not one; otherwise returns what it could read."""
    if len(data) < 24 or data[:4] != MAGIC:
        raise NotGGUF('not a GGUF file')
    version = struct.unpack_from('<I', data, 4)[0]
    if version not in (2, 3):
        raise NotGGUF(f'GGUF version {version} is not read here')
    tensors, count = struct.unpack_from('<QQ', data, 8)
    if count > 100000:
        raise NotGGUF('not a GGUF file')
    at, meta, done = 24, {}, 0
    n = len(data)

    def string():
        nonlocal at
        if at + 8 > n:
            raise EOFError
        size = struct.unpack_from('<Q', data, at)[0]
        if size > 64 * 2**20 or at + 8 + size > n:
            raise EOFError
        s = data[at + 8:at + 8 + size].decode('utf-8', 'replace')
        at += 8 + size
        return s

    def value(kind):
        nonlocal at
        if kind in SCALAR:
            size = struct.calcsize('<' + SCALAR[kind])
            if at + size > n:
                raise EOFError
            v = struct.unpack_from('<' + SCALAR[kind], data, at)[0]
            at += size
            return v
        if kind == STRING:
            return string()
        if kind == ARRAY:
            if at + 12 > n:
                raise EOFError
            inner, items = struct.unpack_from('<IQ', data, at)
            at += 12
            if inner in SCALAR:         # numbers: their size is known without reading them
                size = struct.calcsize('<' + SCALAR[inner])
                if at + size * items > n:
                    raise EOFError
                kept = list(struct.unpack_from('<%d%s' % (min(items, KEEP_ARRAY), SCALAR[inner]), data, at))
                at += size * items
                return {'array': items, 'first': kept}
            kept = []
            for i in range(items):
                v = value(inner)
                if i < KEEP_ARRAY:
                    kept.append(v)
            return {'array': items, 'first': kept}
        raise NotGGUF(f'value type {kind}')
    try:
        for _ in range(count):
            key = string()
            if at + 4 > n:
                raise EOFError
            kind = struct.unpack_from('<I', data, at)[0]
            at += 4
            meta[key] = value(kind)
            done += 1
    except EOFError:
        pass
    return {'version': version, 'tensors': tensors, 'keys': count, 'meta': meta, 'complete': done == count, 'read': done}


def read_file(path, size=4 * 2**20):
    with open(path, 'rb') as f:
        return read(f.read(size))


def _first(v):
    """A value that some models give once and some give per layer."""
    if isinstance(v, dict):
        return max(v['first']) if v.get('first') else None
    return v


def numbers(meta):
    """The architecture's numbers the memory estimate needs, or None for any it does not give."""
    arch = meta.get('general.architecture') or ''
    g = lambda k: _first(meta.get(f'{arch}.{k}'))
    heads, embed = g('attention.head_count'), g('embedding_length')
    kv_heads = g('attention.head_count_kv') or heads
    per_head = (embed // heads) if embed and heads else None
    return {'arch': arch, 'layers': g('block_count'), 'embed': embed, 'heads': heads, 'kv_heads': kv_heads,
            'key': g('attention.key_length') or per_head, 'value': g('attention.value_length') or per_head, 'trained_context': g('context_length')}


def kind_of(meta, name=''):
    """'model', 'projector' (the file that lets a model see pictures), 'draft' (a small model that speeds a large one) or 'other'."""
    t = str(meta.get('general.type') or '').lower()
    arch = str(meta.get('general.architecture') or '').lower()
    low = name.lower()
    if t in ('mmproj', 'projector') or arch == 'clip' or 'mmproj' in low:
        return 'projector'
    if low.startswith('mtp-') or '-mtp' in low or '.mtp' in low:
        return 'draft'
    if t in ('adapter', 'vocab') or 'imatrix' in low:
        return 'other'
    return 'model'


def quant_of(meta, name=''):
    m = QUANT.search(name.replace('.gguf', ''))
    if m:
        return m.group(1).upper().replace('FP16', 'F16')
    return FILE_TYPE.get(meta.get('general.file_type'))


def describe(meta, size=None, name=''):
    num = numbers(meta)
    template = meta.get('tokenizer.chat_template')
    return {'name': meta.get('general.name') or meta.get('general.basename') or name, 'arch': num['arch'], 'size_label': meta.get('general.size_label'),
            'quant': quant_of(meta, name), 'kind': kind_of(meta, name), 'layers': num['layers'], 'trained_context': num['trained_context'],
            'license': meta.get('general.license'), 'bytes': size,
            # a chat template that speaks of tools is one the model was taught to call them with; unknown when the header read stopped before it
            'tools': (('tool' in template) if isinstance(template, str) else None)}


BATCH = 2048        # tokens llama.cpp takes at once: a layer that looks back only a window keeps the window and one batch


def context_bytes(meta, ctx):
    """Memory for the model's working context: for every token, every layer keeps a key and a value, in 16-bit floats.

    Some models (Gemma) give most layers a short window to look back through instead of the whole context, with fewer and
    smaller heads on the few layers that see everything: their headers say which layer is which, and each is counted as it is."""
    n = numbers(meta)
    arch = n['arch']
    if not (n['layers'] and n['kv_heads'] and n['key'] and n['value']):
        return None
    heads = meta.get(f'{arch}.attention.head_count_kv') or n['heads']
    pattern = meta.get(f'{arch}.attention.sliding_window_pattern')
    window = _first(meta.get(f'{arch}.attention.sliding_window'))
    key_w = _first(meta.get(f'{arch}.attention.key_length_swa')) or n['key']
    value_w = _first(meta.get(f'{arch}.attention.value_length_swa')) or n['value']

    def at(v, layer, default):
        if isinstance(v, dict):
            return v['first'][layer] if layer < len(v['first']) else (max(v['first']) if v['first'] else default)
        return default if v is None else v
    total = 0
    for layer in range(n['layers']):
        if isinstance(pattern, dict):
            windowed = bool(at(pattern, layer, False))
        elif isinstance(pattern, int) and not isinstance(pattern, bool) and pattern > 1:
            windowed = (layer + 1) % pattern != 0           # an older form: every Nth layer sees everything
        else:
            windowed = False
        windowed = windowed and bool(window)
        tokens = min(ctx, window + BATCH) if windowed else ctx
        per = (key_w + value_w) if windowed else (n['key'] + n['value'])
        total += tokens * at(heads, layer, n['kv_heads']) * per * 2
    return int(total)


def memory(meta, size, ctx, extra=0):
    """Bytes the model needs running with a context of ctx tokens. extra: the projector and draft files, held in memory whole.
    None if the header does not give the numbers (the read stopped short, or an architecture that names them otherwise)."""
    c = context_bytes(meta, ctx)
    if c is None or not size:
        return None
    return int(size + extra + c + WORKING + 0.03 * size)


def best_context(meta, size, room, extra=0):
    """(context, bytes needed) for the largest context in CONTEXTS that fits in `room` bytes and the model was trained for.
    (None, bytes needed at the smallest) when none fits; (None, None) when the header does not say."""
    trained = numbers(meta)['trained_context'] or CONTEXTS[0]
    need = None
    for ctx in CONTEXTS:
        if ctx > trained:
            continue
        need = memory(meta, size, ctx, extra)
        if need is None:
            return None, None
        if need <= room:
            return ctx, need
    return None, need


def shard(name):
    """(part, parts) for one file of a model split across several ('...-00001-of-00003.gguf'), else None."""
    m = SHARD.search(name)
    return (int(m.group(1)), int(m.group(2))) if m else None
