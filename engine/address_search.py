"""Read-only address lookup using the state indexes installed with offline maps."""
import math
import re
import sqlite3
from pathlib import Path

from engine.paths import ro_uri, visible

# Keep normalization compatible with tools/build_addresses.py.
WORDS = {
    'boulevard': 'blvd', 'avenue': 'ave', 'av': 'ave', 'street': 'st',
    'drive': 'dr', 'road': 'rd', 'place': 'pl', 'court': 'ct', 'lane': 'ln',
    'terrace': 'ter', 'circle': 'cir', 'highway': 'hwy', 'parkway': 'pkwy',
    'square': 'sq', 'north': 'n', 'south': 's', 'east': 'e', 'west': 'w',
    'mount': 'mt', 'saint': 'st',
}
STATE_NAMES = (
    'Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|'
    'District of Columbia|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|'
    'Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|'
    'Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|'
    'New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|'
    'Pennsylvania|Puerto Rico|Rhode Island|South Carolina|South Dakota|'
    'Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming'
).split('|')
STATE_CODES = 'AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA PR RI SC SD TN TX UT VT VA WA WV WI WY'.split()
STATES = dict(zip((name.lower() for name in STATE_NAMES), STATE_CODES))
ONES = 'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty'.split()
ORDINALS = 'zeroth first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth twentieth'.split()
TO_DIGITS = {word: str(number) for number, word in enumerate(ONES)}
for number, word in enumerate(ORDINALS):
    suffix = 'th' if 10 <= number % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(number % 10, 'th')
    TO_DIGITS[word] = f'{number}{suffix}'
TO_WORDS = {value: key for key, value in TO_DIGITS.items()}


def normalize(text):
    return ' '.join(WORDS.get(word, word) for word in re.sub(r'[^a-z0-9 ]+', ' ', text.lower()).split())


def is_address_query(query):
    """A standalone ZIP remains a place query. A number followed by words is an address."""
    return bool(re.match(r'^\s*\d[\w-]*\s+\S', query))


# ---- outside the US: postcodes as people write them, and a street without its house number ----
# A UK postcode is an outward part (area letters, district) and an inward part (a digit and two letters), and is as often
# typed with no space ("SW1A2AA") as with one. The place finder holds them with the space.
UK_POSTCODE = re.compile(r'\s*([A-Za-z]{1,2}\d[A-Za-z\d]?)\s*(\d[A-Za-z]{2})\s*')
HOUSE = r'\d+\s?[a-zA-Z]?(?:\s?[-/]\s?\d+\s?[a-zA-Z]?)?'       # 10, 10a, 10-12, 12/3


def postcode_form(query):
    """'sw1a2aa' -> 'SW1A 2AA'. None when the words are not a UK postcode."""
    m = UK_POSTCODE.fullmatch(query or '')
    return f'{m[1].upper()} {m[2].upper()}' if m else None


def street_only(query):
    """The words of an address with the house number taken out: '10 Downing Street, London' -> 'Downing Street London'.
    The number is written first in Britain, Ireland, France and the US, and after the street in most of Europe
    ('Hauptstraße 12, Berlin'): both are read. None when nothing but a number was written, or when there is no number."""
    parts = [p.strip() for p in (query or '').split(',')]
    first = re.sub(rf'^{HOUSE}\s+(?=\S)', '', parts[0], count=1)
    if first == parts[0]:
        first = re.sub(rf'(?<=\S)\s+{HOUSE}$', '', parts[0], count=1)
    if first == parts[0] or not re.search(r'[^\W\d]', first):
        return None
    return ' '.join(p for p in [first] + parts[1:] if p)


def street_forms(street):
    words = street.split()
    forms = [street, ' '.join(TO_DIGITS.get(word, word) for word in words),
             ' '.join(TO_WORDS.get(word, word) for word in words)]
    forms += [re.sub(r'\b(\d+)(st|nd|rd|th)\b', r'\1', form) for form in forms[:]]
    if words[0] not in ('n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw'):
        forms += [f'{direction} {form}' for form in forms[:] for direction in ('n', 's', 'e', 'w')]
    return list(dict.fromkeys(forms))


def _parts(query):
    number, remainder = query.strip().split(None, 1)
    number = number.upper()
    remainder = re.sub(r'[^a-z0-9 ]+', ' ', remainder.lower()).split()
    # A ZIP+4 ("62704-1234", or "62704 1234" once the hyphen is gone) is the same place as its five-digit ZIP, which the
    # index keeps: drop the four extra digits so the ZIP and the state before it are still read.
    if len(remainder) > 1 and re.fullmatch(r'\d{4}', remainder[-1]) and re.fullmatch(r'\d{5}', remainder[-2]):
        remainder.pop()
    postcode = ''
    if remainder and re.fullmatch(r'\d{5}', remainder[-1]):
        postcode = remainder.pop()
    state = ''
    for count in (3, 2, 1):
        if len(remainder) <= count:
            continue
        name = ' '.join(remainder[-count:])
        code = STATES.get(name) or (name.upper() if count == 1 and name.upper() in STATE_CODES else '')
        if code:
            state = code
            remainder = remainder[:-count]
            break
    return number, normalize(' '.join(remainder)).split(), state, postcode


def find_addresses(query, geo_directory, limit=8, near=None):
    """Return exact house-number matches from installed indexes, never a guessed house."""
    if not is_address_query(query):
        return []
    number, words, state, postcode = _parts(query)
    if not words:
        return []
    geo_directory = Path(geo_directory)
    paths = sorted(visible((geo_directory / 'addresses').glob('*.sqlite')))
    if state:
        paths = [path for path in paths if path.stem.upper() == state]
    legacy = geo_directory / 'addresses.sqlite'
    if not paths and legacy.is_file():
        paths = [legacy]
    connections = []
    try:
        for path in paths:
            try:
                connections.append(sqlite3.connect(ro_uri(path), uri=True))
            except sqlite3.Error:
                continue
        for count in range(len(words), 0, -1):
            street = ' '.join(words[:count])
            if all(word in set(WORDS.values()) for word in words[:count]):
                continue
            city = ' '.join(words[count:])
            rows = []
            for form in street_forms(street):
                conditions = ['num IN (?, ?)', 'norm >= ?', 'norm < ?']
                parameters = [number, number.lower(), form, form + '\uffff']
                if state:
                    conditions.append('state = ?')
                    parameters.append(state)
                if postcode:
                    conditions.append('zip = ?')
                    parameters.append(postcode)
                if city:
                    conditions.append('lower(city) LIKE ?')
                    parameters.append(city + '%')
                sql = ('SELECT num,street,city,state,zip,lat,lon,norm FROM addr WHERE '
                       + ' AND '.join(conditions) + ' ORDER BY length(norm),norm LIMIT ?')
                for connection in connections:
                    try:
                        rows.extend(connection.execute(sql, [*parameters, limit * 3]).fetchall())
                    except sqlite3.Error:
                        continue
            if rows:
                def rank(row):
                    distance = 0
                    if near:
                        distance = (row[5] - near[0]) ** 2 + ((row[6] - near[1]) * math.cos(math.radians(near[0]))) ** 2
                    return len(row[7]), distance, row[3], row[2], row[1]
                results, seen = [], set()
                for num, name, city_name, state_code, zip_code, lat, lon, normalized in sorted(rows, key=rank):
                    key = (num, normalized, lat, lon)
                    if key in seen:
                        continue
                    seen.add(key)
                    results.append({
                        'name': f'{num} {name}', 'kind': 'Street address',
                        'detail': ', '.join(part for part in (city_name, state_code, zip_code) if part),
                        'lat': lat, 'lon': lon, 'source': 'Overture Maps', 'onMap': True,
                    })
                    if len(results) == limit:
                        break
                return results
        return []
    finally:
        for connection in connections:
            connection.close()
