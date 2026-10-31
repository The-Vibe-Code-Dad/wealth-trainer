"""Cached Yahoo Finance chart quotes. No account or API key required."""
import datetime as dt
import json
import logging
import time
import urllib.parse
import urllib.request
from decimal import Decimal
from app import domain as d

LOG = logging.getLogger('wealth.market')


def fetch(symbol, since):
    params = urllib.parse.urlencode({'period1':since,'period2':int(time.time()),'interval':'1d','events':'splits'})
    url = 'https://query1.finance.yahoo.com/v8/finance/chart/' + urllib.parse.quote(symbol,safe='') + '?' + params
    request = urllib.request.Request(url,headers={'User-Agent':'WealthTrainer/1.0','Accept':'application/json'})
    with urllib.request.urlopen(request,timeout=15) as response:
        payload = json.load(response)
    result = payload['chart']['result'][0]
    meta = result['meta']
    price = Decimal(str(meta['regularMarketPrice']))
    stamp = int(meta['regularMarketTime'])
    if meta['symbol'].upper() != symbol.upper() or meta.get('currency') != 'USD' or not price.is_finite() or price <= 0 or stamp > time.time()+300:
        raise ValueError('Invalid USD market quote')
    splits=[]
    for item in result.get('events',{}).get('splits',{}).values():
        ratio=Decimal(str(item['numerator']))/Decimal(str(item['denominator']))
        if not ratio.is_finite() or ratio <= 0:
            raise ValueError('Invalid split ratio')
        splits.append((int(item['date']),str(ratio)))
    return price,stamp,splits


def refresh_prices():
    with d.connect() as db:
        assets=db.execute('SELECT * FROM investments').fetchall()
        earliest=db.execute('SELECT MIN(opened) FROM positions WHERE closed IS NULL').fetchone()[0]
    since=int(time.time())-14*86400
    if earliest:
        since=min(since,int(dt.datetime.fromisoformat(earliest).timestamp())-7*86400)
    updated,failed=[],[]
    for asset in assets:
        try:
            price,stamp,splits=fetch(asset['symbol'],since)
            day=dt.datetime.fromtimestamp(stamp,d.UTC).date().isoformat()
            with d.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                previous=db.execute('SELECT MAX(quote_at) FROM prices WHERE investment=?',(asset['id'],)).fetchone()[0]
                if previous and stamp < previous:
                    raise ValueError('Quote is older than the cached quote')
                db.execute('INSERT INTO prices(investment,price,day,source,fetched,quote_at) VALUES (?,?,?,?,?,?) ON CONFLICT(investment,day) DO UPDATE SET price=excluded.price,source=excluded.source,fetched=excluded.fetched,quote_at=excluded.quote_at',
                           (asset['id'],str(price),day,'Yahoo Finance',d.now(),stamp))
                for at,ratio in splits:
                    db.execute('INSERT INTO splits(investment,at,ratio) VALUES (?,?,?) ON CONFLICT(investment,at) DO UPDATE SET ratio=excluded.ratio',(asset['id'],at,ratio))
                db.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('market_error_'+str(asset['id']),''))
                db.commit()
            updated.append(asset['symbol'])
        except Exception as exc:
            LOG.warning('Yahoo quote unavailable for %s: %s',asset['symbol'],type(exc).__name__)
            failed.append(asset['symbol'])
            with d.connect() as db:
                db.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('market_error_'+str(asset['id']),'Yahoo is temporarily unavailable; retrying automatically.'))
    with d.connect() as db:
        db.execute("INSERT INTO settings(key,value) VALUES ('market_checked',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(d.now(),))
    with d.connect() as db:
        for child in db.execute("SELECT id FROM users WHERE role='child'").fetchall():
            d.record_portfolio(db,child['id'])
    return {'updated':updated,'failed':failed}
