import os
import re
import time
import logging
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.environ['TELEGRAM_TOKEN']
TELEGRAM_CHAT_ID = os.environ['TELEGRAM_CHAT_ID']

HEADERS      = {'User-Agent': 'SEC-13F-Scanner Ahwhat15 ahwhat15@gmail.com'}
EFTS_URL     = 'https://efts.sec.gov/LATEST/search-index'
EDGAR        = 'https://www.sec.gov'
DATA         = 'https://data.sec.gov'
MIN_AUM_K    = 1_000_000   # $1B expressed in $1000 units (SEC value field is in $1000s)
CET          = ZoneInfo('Europe/Paris')


# ── Telegram ──────────────────────────────────────────────────────────────────

def _send(text: str) -> None:
    url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
    r = requests.post(url, json={
        'chat_id': TELEGRAM_CHAT_ID,
        'text': text,
        'parse_mode': 'HTML',
        'disable_web_page_preview': True,
    }, timeout=30)
    r.raise_for_status()


def send_telegram(text: str) -> None:
    """Send, splitting into ≤4000-char chunks if needed."""
    max_len = 4000
    if len(text) <= max_len:
        _send(text)
        logger.info('Telegram sent (%d chars)', len(text))
        return

    lines, chunk = text.split('\n'), ''
    for line in lines:
        if len(chunk) + len(line) + 1 > max_len:
            _send(chunk.strip())
            time.sleep(1)
            chunk = ''
        chunk += line + '\n'
    if chunk.strip():
        _send(chunk.strip())
    logger.info('Telegram sent in multiple chunks')


# ── EDGAR data fetch ───────────────────────────────────────────────────────────

def get_13f_filings(start_date: str, end_date: str) -> list[dict]:
    """Return deduplicated 13F-HR filing stubs from EFTS."""
    filings: list[dict] = []
    seen_acc: set[str]  = set()
    from_offset = 0
    page_size   = 40

    while True:
        params = {
            'q': '', 'forms': '13F-HR', 'dateRange': 'custom',
            'startdt': start_date, 'enddt': end_date,
            'from': from_offset,
        }
        try:
            r = requests.get(EFTS_URL, params=params, headers=HEADERS, timeout=30)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logger.error('EFTS request failed: %s', e)
            break

        hits = data.get('hits', {}).get('hits', [])
        if not hits:
            break

        for hit in hits:
            src = hit.get('_source', {})
            acc = src.get('accession_no', '')
            if acc and acc not in seen_acc:
                seen_acc.add(acc)
                filings.append({
                    'id':           hit.get('_id', ''),
                    'accession_no': acc,
                    'entity_name':  src.get('entity_name', ''),
                    'file_date':    src.get('file_date', ''),
                })

        total = data.get('hits', {}).get('total', {}).get('value', 0)
        from_offset += page_size
        if from_offset >= min(total, 200):
            break
        time.sleep(0.12)

    logger.info('Found %d unique 13F-HR filings', len(filings))
    return filings


def cik_from_path(filing_id: str) -> str:
    """Extract CIK from the EFTS _id path (/Archives/edgar/data/{cik}/...)."""
    parts = filing_id.strip('/').split('/')
    # parts: ['Archives','edgar','data','{cik}', ...]
    if len(parts) > 3 and parts[3].isdigit():
        return parts[3]
    return ''


def find_info_table_url(filing_id: str) -> str | None:
    """Locate the information-table XML inside a 13F filing directory."""
    dir_path = filing_id.rsplit('/', 1)[0] if '/' in filing_id else filing_id
    dir_url  = f'{EDGAR}{dir_path}/'
    try:
        r = requests.get(dir_url, headers=HEADERS, timeout=20)
        r.raise_for_status()
    except Exception as e:
        logger.debug('Dir fetch failed %s: %s', dir_url, e)
        return None

    links = re.findall(r'href="([^"]+\.xml)"', r.text, re.IGNORECASE)
    preferred = [l for l in links if any(k in l.lower() for k in ['infotable', 'information', 'holdings'])]
    if not preferred:
        preferred = [l for l in links if not any(k in l.lower() for k in ['primary', 'full', 'submission'])]
    target = (preferred or links or [None])[0]
    if not target:
        return None
    return (EDGAR + target) if target.startswith('/') else (dir_url + target)


# ── Parsing ────────────────────────────────────────────────────────────────────

def parse_holdings(xml_text: str) -> dict[str, dict]:
    """Parse a 13F information table. Returns {key: {name, value_k, shares, put_call}}."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.debug('XML parse error: %s', e)
        return {}

    ns = (root.tag.split('}')[0] + '}') if root.tag.startswith('{') else ''

    holdings: dict[str, dict] = {}
    for entry in root.iter(f'{ns}infoTable'):
        def g(tag: str) -> str:
            el = entry.find(f'{ns}{tag}')
            return (el.text or '').strip() if el is not None else ''

        cusip    = g('cusip')
        name     = g('nameOfIssuer')
        put_call = g('putCall')

        try:
            value_k = int(g('value').replace(',', ''))
        except ValueError:
            value_k = 0

        shares = 0
        shrs_el = entry.find(f'{ns}shrsOrPrnAmt')
        if shrs_el is not None:
            s = shrs_el.find(f'{ns}sshPrnamt')
            if s is not None and s.text:
                try:
                    shares = int(s.text.replace(',', ''))
                except ValueError:
                    pass

        if cusip and value_k > 0:
            key = f'{cusip}_{put_call}' if put_call else cusip
            if key in holdings:
                holdings[key]['value_k'] += value_k
                holdings[key]['shares']  += shares
            else:
                holdings[key] = {
                    'name': name, 'value_k': value_k,
                    'shares': shares, 'put_call': put_call,
                }

    return holdings


def get_prev_holdings(cik: str, current_acc: str) -> dict[str, dict]:
    """Fetch the previous quarter's 13F holdings via the submissions API."""
    try:
        r = requests.get(
            f'{DATA}/submissions/CIK{cik.zfill(10)}.json',
            headers=HEADERS, timeout=30
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.debug('Submissions fetch failed CIK %s: %s', cik, e)
        return {}

    recent     = data.get('filings', {}).get('recent', {})
    forms      = recent.get('form', [])
    accessions = recent.get('accessionNumber', [])
    curr_clean = current_acc.replace('-', '')

    prev_acc = None
    for form, acc in zip(forms, accessions):
        if form in ('13F-HR', '13F-HR/A') and acc.replace('-', '') != curr_clean:
            prev_acc = acc
            break

    if not prev_acc:
        return {}

    acc_clean = prev_acc.replace('-', '')
    dir_url   = f'{EDGAR}/Archives/edgar/data/{cik}/{acc_clean}/'
    try:
        r = requests.get(dir_url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        links = re.findall(r'href="([^"]+\.xml)"', r.text, re.IGNORECASE)
        preferred = [l for l in links if any(k in l.lower() for k in ['infotable', 'information', 'holdings'])]
        target    = (preferred or links or [None])[0]
        if not target:
            return {}
        xml_url = (EDGAR + target) if target.startswith('/') else (dir_url + target)
        r2 = requests.get(xml_url, headers=HEADERS, timeout=40)
        r2.raise_for_status()
        return parse_holdings(r2.text)
    except Exception as e:
        logger.debug('Prev holdings fetch failed: %s', e)
        return {}


# ── Analysis ───────────────────────────────────────────────────────────────────

def analyze_changes(current: dict, previous: dict) -> dict:
    new_pos: dict   = {}
    increases: dict = {}
    exits: dict     = {}

    for key, curr in current.items():
        if key not in previous:
            new_pos[key] = curr
        else:
            prev_k = previous[key]['value_k']
            curr_k = curr['value_k']
            if prev_k > 0 and curr_k > prev_k * 1.20:
                increases[key] = {**curr, 'prev_value_k': prev_k,
                                   'pct': (curr_k - prev_k) / prev_k * 100}

    for key, prev in previous.items():
        if key not in current:
            exits[key] = prev

    top_by = lambda d, k, n=5: sorted(d.values(), key=lambda x: x[k], reverse=True)[:n]
    return {
        'new':       top_by(new_pos, 'value_k'),
        'increases': sorted(increases.values(), key=lambda x: x['pct'], reverse=True)[:5],
        'exits':     top_by(exits, 'value_k'),
    }


# ── Formatting ─────────────────────────────────────────────────────────────────

def format_message(results: list[dict], date_range: str) -> str:
    lines = [
        f'<b>📊 13F Institutional Holdings — {date_range}</b>',
        f'<i>{len(results)} fund(s) with >$1B AUM filed in the last 7 days</i>\n',
    ]
    for fund in results:
        aum_m = fund['total_value_k'] / 1_000
        lines.append(f'\n<b>🏦 {fund["name"]}</b>  |  AUM: ${aum_m:,.0f}M')
        ch = fund['changes']

        if ch['new']:
            lines.append('  <b>🆕 New Positions:</b>')
            for h in ch['new'][:3]:
                pc = f' ({h["put_call"]})' if h.get('put_call') else ''
                lines.append(f'    • {h["name"]}{pc}: ${h["value_k"] / 1_000:,.1f}M')

        if ch['increases']:
            lines.append('  <b>📈 Biggest Increases:</b>')
            for h in ch['increases'][:3]:
                pc = f' ({h["put_call"]})' if h.get('put_call') else ''
                lines.append(f'    • {h["name"]}{pc}: +{h["pct"]:.0f}% → ${h["value_k"] / 1_000:,.1f}M')

        if ch['exits']:
            lines.append('  <b>🚪 Complete Exits:</b>')
            for h in ch['exits'][:3]:
                lines.append(f'    • {h["name"]}: was ${h["value_k"] / 1_000:,.1f}M')

    return '\n'.join(lines)


# ── Orchestration ──────────────────────────────────────────────────────────────

def run_scanner() -> None:
    logger.info('=== 13F scanner starting ===')
    now        = datetime.now(CET)
    end        = now.strftime('%Y-%m-%d')
    start      = (now - timedelta(days=7)).strftime('%Y-%m-%d')
    date_range = f'{start} → {end}'

    filings = get_13f_filings(start, end)
    results: list[dict] = []

    for filing in filings:
        info_url = find_info_table_url(filing['id'])
        if not info_url:
            continue

        try:
            r = requests.get(info_url, headers=HEADERS, timeout=40)
            r.raise_for_status()
            current = parse_holdings(r.text)
        except Exception as e:
            logger.warning('Holdings fetch error %s: %s', filing['entity_name'], e)
            continue

        total_k = sum(h['value_k'] for h in current.values())
        if total_k < MIN_AUM_K:
            logger.debug('%s below $1B threshold (%.0fM)', filing['entity_name'], total_k / 1_000)
            continue

        logger.info('%s: $%.0fM AUM', filing['entity_name'], total_k / 1_000)

        cik = cik_from_path(filing['id'])
        if not cik:
            # Fallback: first 10 digits of accession number
            cik = filing['accession_no'].replace('-', '')[:10].lstrip('0') or '0'

        previous = get_prev_holdings(cik, filing['accession_no'])
        if not previous:
            logger.info('No previous 13F for %s — skipping diff', filing['entity_name'])
            continue

        changes = analyze_changes(current, previous)
        if not any(changes[k] for k in ('new', 'increases', 'exits')):
            continue

        results.append({
            'name':          filing['entity_name'],
            'total_value_k': total_k,
            'changes':       changes,
        })
        time.sleep(0.5)

    results.sort(key=lambda x: x['total_value_k'], reverse=True)
    logger.info('Qualifying funds with changes: %d', len(results))

    if not results:
        send_telegram(
            '📋 <b>13F Institutional Scanner</b>\n'
            'No new 13F filings with significant changes from funds >$1B AUM in the last 7 days.'
        )
    else:
        send_telegram(format_message(results[:10], date_range))

    logger.info('=== 13F scanner done ===')


def next_run_seconds() -> float:
    """Seconds until next Sunday 19:00 CET, accounting for DST."""
    now = datetime.now(CET)
    days_until_sunday = (6 - now.weekday()) % 7
    target = (now + timedelta(days=days_until_sunday)).replace(
        hour=19, minute=0, second=0, microsecond=0
    )
    if now >= target:
        target += timedelta(days=7)
    return (target - now).total_seconds()


if __name__ == '__main__':
    while True:
        try:
            run_scanner()
        except Exception as exc:
            logger.exception('Unhandled scanner error: %s', exc)

        secs = next_run_seconds()
        logger.info('Next run in %.1f hours (Sunday 19:00 CET)', secs / 3600)
        time.sleep(secs)
