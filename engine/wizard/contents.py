"""What a pack holds, as things a person can say yes or no to, and what a set of answers comes to.

A pack's catalog.json lists every document, saved page, video, transcript and encyclopedia with its topic, publisher
and size; its pack-manifest.tsv lists every file. From the two:

  menu(records, manifest)     the choices, each with its count and size:
                                documents by topic; encyclopedias one by one; videos by channel (the large ones) and topic
  plan(records, manifest, choice)   what a choice comes to: the catalog records kept, every file to bring as it is,
                                every video to make smaller (and how), and the size of it all

A choice:
  {'documents': ['medical', 'water', ...],            topic ids; or 'all'
   'encyclopedias': ['collections/wikipedia_....zim'], paths; or 'all'
   'videos': {'default': 'small', 'channel:Corporals Corner': 'words', 'topic:science': 'none'}}
                                                      for each group of videos: 'small' (re-encoded), 'original',
                                                      'words' (no picture: the transcript, captions and thumbnail only), 'none'

Files no record names (a saved book's pictures, a course's style sheets) go with the records they sit beside.
"""
import re

from . import video

# Topics, in the order they are offered. A record goes to the first whose words appear in its category, collection or folder.
TOPICS = [
    ('medical', 'Medicine, first aid and health', r'medic|first-aid|health|clinic|nurs|midwi|pharma|anatomy|hygiene|disease|dental|mental', True),
    ('emergency', 'Disasters, shelter and survival', r'disaster|survival|emergency|fire-and-shelter|shelter|evacuat|preparedness|hazard|knots|weather', True),
    ('water', 'Water and sanitation', r'water|sanitation|rainwater|latrine|sewage', True),
    ('food', 'Food, farming and preserving', r'agricultur|food|seed|garden|growing|farm|livestock|beekeep|fishing|orchard|crop|grain-mill|coastal-growing', True),
    ('firearms', 'Firearms and hunting', r'firearm|hunting|game-processing', False),
    ('power', 'Power, vehicles and machines', r'energy|power|generator|engine|mower|blower|vehicle|truck|\bcar\b|passenger-car|marine|equipment|hvac|pump|tractor|chainsaw|battery|solar|aerial-work', True),
    ('comms', 'Communications and navigation', r'communicat|navigation|radio|signal', True),
    ('making', 'Building, materials and making things', r'manufactur|material|metal|weld|wood|textile|leather|craft|glass|ceramic|sharpen|tool|machin|milling|refractor|workshop|production|mineral|'
                                                       r'typesett|paper|bearing|seal|measurement|laboratory|inventory|construction|masonry|infrastructure', False),
    ('aviation', 'Aviation and space', r'aviation|aircraft|space', False),
    ('science', 'Science, mathematics and engineering study', r'scien|math|physic|chemis|biolog|astronom|earth|geolog|comput|engineer|electro|circuit|control|thermo|statistic|genetic|ecolog|course', False),
    ('community', 'Community, care and teaching', r'community|education|care|school|accessib|organiz', False),
]
OTHER = ('other', 'Everything else', False)
BIG_CHANNEL = 2 * 2**30         # a publisher with this much video is offered by name
MODES = ('small', 'original', 'words', 'none')
ALWAYS = ('library-data/reader.css',)       # carried whatever is chosen; catalog.json, search.sqlite and summary.json are written afresh
LIB = 'library-data/'


def topic_of(record):
    words = ' '.join(str(record.get(k) or '') for k in ('category', 'collection')).lower() + ' ' + '/'.join(record.get('path', '').split('/')[:2]).lower()
    if record.get('path', '').startswith('courses/'):
        return 'science'
    for tid, _, rx, _ in TOPICS:
        if re.search(rx, words):
            return tid
    return OTHER[0]


def topic_title(tid):
    return next((t for i, t, _, _ in TOPICS if i == tid), OTHER[1])


def _beside(path):
    """The folder a file's companions share: a course, a saved book, a category of saved-page pictures."""
    parts = path.split('/')
    if parts[0] == 'courses' and len(parts) > 2:
        return '/'.join(parts[:2])
    if parts[0] == 'pages' and len(parts) > 3:
        return '/'.join(parts[:3])
    if parts[0] == 'assets' and len(parts) > 2:
        return 'pages/' + parts[1]          # pictures of the saved pages of one category
    return None


class Pack:
    """A pack's catalog and manifest, indexed for choosing from."""

    def __init__(self, records, manifest):
        """records: catalog.json as read. manifest: {path as in the pack ('library-data/...'): (bytes, sha256)}."""
        self.records = records
        self.manifest = manifest
        self.by_id = {r['id']: r for r in records}
        named = set()
        for r in records:
            for p in self.files_of(r):
                named.add(p)
        self.videos = [r for r in records if r['kind'] == 'video']
        by_video = {}
        for r in records:           # a video's transcript record and its thumbnail record
            if r['kind'] in ('transcript', 'metadata') and r.get('video_path'):
                by_video.setdefault(r['video_path'], []).append(r)
        self.with_video = by_video
        self.companions = {}        # folder -> [(path, bytes)] of files no record names
        for path, (size, _) in manifest.items():
            if not path.startswith(LIB) or path in named or path in ALWAYS:
                continue
            key = _beside(path[len(LIB):])
            if key:
                self.companions.setdefault(key, []).append((path, size))
        totals = {}
        for r in self.videos:
            totals[r.get('publisher') or ''] = totals.get(r.get('publisher') or '', 0) + (r.get('bytes') or 0)
        self.channels = {p for p, b in totals.items() if p and b >= BIG_CHANNEL}

    @staticmethod
    def files_of(r):
        """Every file a record names, as paths in the pack."""
        out = []
        for k in ('path', 'text_path', 'thumbnail_path', 'vtt_path'):
            if r.get(k):
                out.append(LIB + r[k])
        for c in r.get('captions') or []:
            out.append(LIB + c)
        return list(dict.fromkeys(out))

    def size(self, path):
        return self.manifest.get(path, (0, None))[0]

    def group_of(self, video):
        """The group a video is offered in: its channel if that is a large one, else its topic."""
        if video.get('publisher') in self.channels:
            return 'channel:' + video['publisher']
        return 'topic:' + topic_of(video)

    # ---------------------------------------------------------------------------------------------------- menu ----
    def menu(self):
        docs, vids, owner = {}, {}, {}
        for r in self.records:
            if r['kind'] in ('pdf', 'html', 'pptx'):
                topic = topic_of(r)
                d = docs.setdefault(topic, {'count': 0, 'bytes': 0})
                d['count'] += 1
                d['bytes'] += sum(self.size(p) for p in self.files_of(r))
                key = _beside(r['path'])
                if key:
                    owner.setdefault(key, topic)
                if r['kind'] == 'html' and r['path'].startswith('pages/'):
                    owner.setdefault('pages/' + r['path'].split('/')[1], topic)
        for key, files in self.companions.items():      # a course's or a book's other files count with its topic
            if key in owner:
                docs[owner[key]]['bytes'] += sum(s for _, s in files)
        for r in self.videos:
            g = vids.setdefault(self.group_of(r), {'count': 0, 'seconds': 0, 'bytes': 0, 'small_bytes': 0, 'words_bytes': 0, 'to_shrink': 0})
            extra = sum(self.size(p) for p in self.files_of(r)[1:]) + sum(self.size(p) for o in self.with_video.get(r['path'], []) for p in self.files_of(o))
            g['count'] += 1
            g['seconds'] += r.get('duration') or 0
            g['bytes'] += (r.get('bytes') or 0) + extra
            g['small_bytes'] += video.estimate(r) + extra
            g['words_bytes'] += extra
            g['to_shrink'] += video.profile_for(r) != 'keep'
        order = [t[0] for t in TOPICS] + [OTHER[0]]
        essential = {t[0] for t in TOPICS if t[3]}
        return {
            'documents': [dict(docs[t], id=t, title=topic_title(t), essential=t in essential) for t in order if t in docs],
            'encyclopedias': [{'id': r['path'], 'title': r['title'], 'bytes': r.get('bytes') or 0} for r in sorted(self.records, key=lambda r: r.get('bytes') or 0) if r['kind'] == 'zim'],
            'videos': ([dict(v, id=k, title=k[8:], channel=True) for k, v in sorted(vids.items(), key=lambda kv: -kv[1]['bytes']) if k.startswith('channel:')]
                       + [dict(vids['topic:' + t], id='topic:' + t, title=topic_title(t), channel=False) for t in order if 'topic:' + t in vids]),
            'always_bytes': sum(self.size(p) for p in ALWAYS) + self.size(LIB + 'search.sqlite') + self.size(LIB + 'catalog.json'),
        }

    # ---------------------------------------------------------------------------------------------------- plan ----
    def plan(self, choice):
        """What a choice comes to:
        {'records': [catalog records kept, videos marked], 'copy': [(pack path, bytes, sha256)], 'shrink': [(record, profile)],
         'bytes': estimated total, 'counts': {...}}"""
        topics = choice.get('documents', [])
        zims = choice.get('encyclopedias', [])
        modes = dict(choice.get('videos') or {})
        default = modes.get('default', 'none')
        for k, m in modes.items():
            if m not in MODES:
                raise ValueError(f'{m!r} is not a way to keep videos')
        keep, copy, shrink = [], {}, []
        counts = {'documents': 0, 'encyclopedias': 0, 'videos': 0, 'videos_small': 0, 'videos_words': 0}
        total = 0

        def bring(path):
            nonlocal total
            if path in self.manifest and path not in copy:
                size, sha = self.manifest[path]
                copy[path] = (size, sha)
                total += size
        beside = set()
        for r in self.records:
            kind = r['kind']
            if kind in ('pdf', 'html', 'pptx'):
                if topics == 'all' or topic_of(r) in topics:
                    keep.append(r)
                    counts['documents'] += 1
                    for p in self.files_of(r):
                        bring(p)
                    key = _beside(r['path'])
                    if key:
                        beside.add(key)
                    if kind == 'html':      # the pictures its category's saved pages share
                        beside.add('pages/' + (r['path'].split('/')[1] if r['path'].startswith('pages/') else str(r.get('category') or '')))
            elif kind == 'zim':
                if zims == 'all' or r['path'] in zims:
                    keep.append(r)
                    counts['encyclopedias'] += 1
                    bring(LIB + r['path'])
            elif kind == 'video':
                mode = modes.get(self.group_of(r), default)
                if mode == 'none':
                    continue
                others = self.with_video.get(r['path'], [])
                for p in self.files_of(r)[1:]:          # captions and extracted words, whatever becomes of the picture
                    bring(p)
                for o in others:
                    keep.append(o)
                    for p in self.files_of(o):
                        bring(p)
                counts['videos'] += 1
                profile = video.profile_for(r) if mode == 'small' else 'keep'
                if mode == 'words':
                    keep.append(dict(r, words_only=True))
                    counts['videos_words'] += 1
                elif profile == 'keep':
                    keep.append(r)
                    bring(LIB + r['path'])
                else:
                    keep.append(r)
                    shrink.append((r, profile))
                    counts['videos_small'] += 1
                    total += video.estimate(r, profile)
        for key in (self.companions if topics == 'all' else beside):       # every document: every file that goes with documents
            for path, _ in self.companions.get(key, []):
                bring(path)
        for path in ALWAYS:
            bring(path)
        order = {r['id']: i for i, r in enumerate(self.records)}
        keep.sort(key=lambda r: order[r['id']])     # as the pack lists them: the app shows things in the catalog's order
        # The search index is written afresh with only what is kept: about in proportion. The pack's whole one is read to make it.
        index_whole = self.size(LIB + 'search.sqlite')
        index = int(index_whole * len(keep) / max(1, len(self.records)))
        return {'records': keep, 'copy': [(p, s, h) for p, (s, h) in copy.items()], 'shrink': shrink, 'bytes': total + index, 'counts': counts,
                'index_bytes': index, 'index_whole': index_whole}


def read_manifest(text):
    """pack-manifest.tsv -> {path: (bytes, sha256)}."""
    out = {}
    for line in text.splitlines()[1:]:
        c = line.split('\t')
        if len(c) >= 3 and c[1].isdigit():
            out[c[0]] = (int(c[1]), c[2] or None)
    return out
