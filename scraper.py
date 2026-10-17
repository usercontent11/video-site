import os,re,json,asyncio,random,argparse,logging,hashlib,datetime
from pathlib import Path
from urllib.parse import urlparse,urljoin
import httpx,psycopg
from bs4 import BeautifulSoup

BASE=os.getenv("SOURCE_ORIGIN","https://mobilexh.live").rstrip("/")
PAGES=int(os.getenv("MAX_PAGES","30"))
WORKERS=max(1,min(6,int(os.getenv("WORKERS","3"))))
DELAY=max(0.5,float(os.getenv("REQUEST_DELAY","1.5")))
logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
log=logging.getLogger("scraper")
DIAG=Path("diagnostics")
DIAG.mkdir(exist_ok=True)
DDL="""
CREATE TABLE IF NOT EXISTS categories (
 slug text PRIMARY KEY,name text NOT NULL,sort_order int NOT NULL,
 status text NOT NULL DEFAULT 'pending',last_error text,terminal_page int,
 updated_at timestamptz DEFAULT now(),completed_at timestamptz
);
CREATE TABLE IF NOT EXISTS videos (
 id text PRIMARY KEY,title text NOT NULL,video_path text NOT NULL,
 thumbnail_path text,duration_seconds int,views bigint,
 creator_name text,creator_path text,updated_at timestamptz DEFAULT now()
);
CREATE TABLE IF NOT EXISTS video_categories (
 video_id text REFERENCES videos(id),category_slug text REFERENCES categories(slug),
 first_page int NOT NULL,PRIMARY KEY(video_id,category_slug)
);
CREATE INDEX IF NOT EXISTS idx_video_categories_category ON video_categories(category_slug);
CREATE TABLE IF NOT EXISTS scrape_pages (
 category_slug text REFERENCES categories(slug),page int NOT NULL,
 item_count int NOT NULL,status text NOT NULL DEFAULT 'ok',
 updated_at timestamptz DEFAULT now(),PRIMARY KEY(category_slug,page)
);
"""
def slug(s):
 return re.sub(r"[^a-z0-9]+","-",s.lower().replace("&"," and ")).strip("-")
def path(url):
 if not url:return None,None
 u=urlparse(urljoin(BASE,url))
 return u.netloc.lower(),u.path
def seconds(s):
 try:
  nums=[int(n) for n in s.split(":")]
  if len(nums) not in (2,3):return None
  n=0
  for v in nums:n=n*60+v
  return n
 except (ValueError,AttributeError):return None
def view_count(s):
 m=re.search(r"([\d,.]+)\s*([kmb])?",s.lower())
 return int(float(m[1].replace(",",""))*{"k":1000,"m":1000000,"b":1000000000}.get(m[2],1)) if m else None
def parse_initials(html):
 soup=BeautifulSoup(html,"html.parser")
 script=soup.select_one("script#initials-script")
 if not script:return []
 raw=(script.string or script.get_text() or "").strip()
 match=re.search(r"window\.initials\s*=\s*",raw)
 if not match:return []
 try:
  state=json.loads(raw[match.end():].strip().rstrip(";"))
 except (ValueError,TypeError) as err:
  log.warning("Initial JSON unavailable: %s",err)
  return []
 cards=state.get("pagesCategoryComponent",{}).get("trendingVideoListProps",{}).get("videoThumbProps",[])
 if not isinstance(cards,list):return []
 rows=[];seen=set()
 for item in cards:
  if not isinstance(item,dict):continue
  vid=str(item.get("id") or "")
  if not vid.isdigit() or vid in seen or item.get("videoType") not in (None,"video"):continue
  host,video_path=path(item.get("pageURL"))
  if host!=urlparse(BASE).netloc or not video_path or not video_path.startswith("/videos/"):continue
  title=str(item.get("title") or "").strip()
  if not title:continue
  _,thumb_path=path(item.get("imageURL") or item.get("thumbURL"))
  duration=item.get("duration")
  duration=int(duration) if isinstance(duration,(int,float)) and duration>=0 else None
  views=item.get("views")
  views=int(views) if isinstance(views,(int,float)) and views>=0 else None
  landing=item.get("landing") or {}
  if not isinstance(landing,dict):landing={}
  creator_name=landing.get("name")
  creator_host,creator_path=path(landing.get("link"))
  if creator_host!=urlparse(BASE).netloc:creator_path=None
  rows.append((vid,title,video_path,thumb_path,duration,views,creator_name,creator_path))
  seen.add(vid)
 return rows

def parse(html):
 soup=BeautifulSoup(html,"html.parser")
 rows=[];seen=set()
 for el in soup.select("[data-video-id][data-video-type='video']"):
  vid=el.get("data-video-id","")
  link=el.select_one("a[data-role='thumb-link'][href*='/videos/']")
  if not vid.isdigit() or vid in seen or not link:continue
  host,video_path=path(link.get("href"))
  if host!=urlparse(BASE).netloc or not video_path.startswith("/videos/"):continue
  title_el=el.select_one("a.video-thumb-info__name")
  title=(title_el.get("title") or title_el.get_text(" ",strip=True)) if title_el else link.get("aria-label","")
  if not title:continue
  img=el.select_one("img[data-role='thumb-preview-img']")
  _,tp=path(img.get("src")) if img else (None,None)
  dur=el.select_one("[data-role='video-duration']")
  views=el.select_one(".video-thumb-views")
  creator=el.select_one("a.video-uploader__name[href]")
  _,cp=path(creator.get("href")) if creator else (None,None)
  rows.append((vid,title,video_path,tp,seconds(dur.get_text(strip=True)) if dur else None,view_count(views.get_text(" ",strip=True)) if views else None,creator.get_text(" ",strip=True) if creator else None,cp))
  seen.add(vid)
 return rows
def categories():
 data=json.loads(Path("category_list.json").read_text())["categories"]
 out=[];seen=set()
 for i,name in enumerate(data):
  s=slug(name)
  if s and s not in seen:out.append((s,name,i));seen.add(s)
 return out
def init(db):
 with db.transaction():
  db.execute(DDL,prepare=False)
  db.execute("ALTER TABLE categories ADD COLUMN IF NOT EXISTS terminal_page int")
  for s,n,i in categories():
   db.execute("INSERT INTO categories(slug,name,sort_order) VALUES(%s,%s,%s) ON CONFLICT(slug) DO UPDATE SET name=excluded.name,sort_order=excluded.sort_order",(s,n,i))
def save(db,s,page,items):
 with db.transaction():
  for v in items:
   db.execute("""INSERT INTO videos(id,title,video_path,thumbnail_path,duration_seconds,views,creator_name,creator_path)
   VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET
   title=excluded.title,video_path=excluded.video_path,
   thumbnail_path=COALESCE(excluded.thumbnail_path,videos.thumbnail_path),
   duration_seconds=COALESCE(excluded.duration_seconds,videos.duration_seconds),
   views=COALESCE(excluded.views,videos.views),
   creator_name=COALESCE(excluded.creator_name,videos.creator_name),
   creator_path=COALESCE(excluded.creator_path,videos.creator_path),
   updated_at=now()""",v)
   db.execute("INSERT INTO video_categories(video_id,category_slug,first_page) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",(v[0],s,page))
  db.execute("INSERT INTO scrape_pages(category_slug,page,item_count) VALUES(%s,%s,%s) ON CONFLICT(category_slug,page) DO UPDATE SET item_count=excluded.item_count,updated_at=now()",(s,page,len(items)))
def capture_response(category,page,response,reason):
 """Keep the actual failed HTML in the Actions artifact, not in Git history."""
 folder=DIAG / slug(category)
 folder.mkdir(parents=True,exist_ok=True)
 now=datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
 prefix=f"{page:04d}-{now}"
 target=folder / (prefix+".html")
 # Preserve the original response for inspection, with a size bound.
 target.write_bytes(response.content[:2_000_000])
 details={"category":category,"page":page,"time_utc":now,"url":str(response.url),
          "status_code":response.status_code,"content_type":response.headers.get("content-type"),
          "bytes":len(response.content),"sha256":hashlib.sha256(response.content).hexdigest(),
          "reason":reason,"diagnostics":diagnose(response.text)}
 (folder/(prefix+".json")).write_text(json.dumps(details,indent=2),encoding="utf-8")
 log.error("Failed HTML saved: %s",target)
 return target

def describe_initials(html):
 soup=BeautifulSoup(html,"html.parser")
 script=soup.select_one("#initials-script")
 if not script:return "initials_script=missing"
 raw=script.string or script.get_text() or ""
 m=re.search(r"window\.initials\s*=\s*",raw)
 if not m:return "initials_assignment=missing"
 try:
  obj=json.loads(raw[m.end():].strip().rstrip(";"))
  component=obj.get("pagesCategoryComponent")
  if not isinstance(component,dict):return "category_component=missing"
  props=component.get("trendingVideoListProps")
  if not isinstance(props,dict):return "trending_list_props=missing"
  cards=props.get("videoThumbProps")
  return f"videoThumbProps_type={type(cards).__name__} count={len(cards) if isinstance(cards,list) else 'n/a'}"
 except (ValueError,TypeError,AttributeError) as exc:return f"initials_parse_error={str(exc)[:80]}"

def diagnose(html):
 soup=BeautifulSoup(html,"html.parser")
 title=soup.title.get_text(" ",strip=True)[:100] if soup.title else "(none)"
 signals=" ".join(soup.stripped_strings).lower()[:5000]
 checks=("captcha","verify you are human","access denied","enable javascript","unusual traffic","just a moment","checking your browser")
 challenge=any(x in signals or x in title.lower() for x in checks)
 cards=len(soup.select("[data-video-id]"))
 links=len(soup.select("a[href*='/videos/']"))
 scripts=len(soup.select("script"))
 meta_refresh=bool(soup.select('meta[http-equiv="refresh"]'))
 body=soup.body.get_text(" ",strip=True)[:180] if soup.body else ""
 body=re.sub(r"\\s+"," ",body)
 return f"bytes={len(html)} title={title!r} video_id_nodes={cards} video_links={links} scripts={scripts} meta_refresh={meta_refresh} verification_hint={challenge} body_sample={body!r}"

async def fetch(client,s,p):
 url=f"{BASE}/categories/{s}"+(f"/{p}" if p>1 else "")
 for attempt in range(4):
  await asyncio.sleep(DELAY+random.uniform(0,.4))
  try:
   r=await client.get(url)
   if r.status_code in (403,429):
    capture_response(s,p,r,f"HTTP {r.status_code} access restriction")
    raise PermissionError(f"source returned {r.status_code}; stop")
   if r.status_code==404:
    return None  # potential end of pagination; verify below
   r.raise_for_status()
   items=parse_initials(r.text)
   if items:
    log.info("%s page %s: %d initial-state records",s,p,len(items))
   else:
    items=parse(r.text)
    if items:log.info("%s page %s: %d DOM records (fallback)",s,p,len(items))
   if not items:
    capture_response(s,p,r,"No recognized listing records")
    raise ValueError("No recognized video cards. "+diagnose(r.text)+" "+describe_initials(r.text)+f" final_url={r.url}; page NOT checkpointed")
   return items
  except PermissionError:raise
  except (httpx.HTTPError,ValueError) as e:
   if isinstance(e,ValueError) and "No recognized video cards" in str(e):
    raise  # repeated fetches of non-video pages are not useful
   if attempt==3:raise
   log.warning("%s page %s attempt %s: %s",s,p,attempt+1,e)
   await asyncio.sleep(2**attempt)
async def run(args):
 if args.mode=="test":
  sample='''<div data-video-id="30930612" data-video-type="video"><a data-role="thumb-link" href="/videos/test-xh123" aria-label="Test"></a><a class="video-thumb-info__name" title="Test"></a><div data-role="video-duration">10:42</div><div class="video-thumb-views">1.3M views</div></div>'''
  assert parse(sample)[0][4:6]==(642,1300000)
  assert slug("18 Year Old")=="18-year-old"
  fixture='<script id="initials-script">window.initials='+json.dumps({"pagesCategoryComponent":{"trendingVideoListProps":{"videoThumbProps":[{"id":12,"title":"Sample","pageURL":BASE+"/videos/sample-xhA","videoType":"video","imageURL":"https://img.example/a.webp","duration":642,"views":1300000,"landing":{"name":"Creator","link":BASE+"/creators/sample"}}]}}})+';</script>'
  parsed=parse_initials(fixture)
  assert len(parsed)==1 and parsed[0]==("12","Sample","/videos/sample-xhA","/a.webp",642,1300000,"Creator","/creators/sample")
  log.info("Parser unit test passed")
  if args.live:
   test_headers={"Cookie":"parental-control=yes"} if args.test_parental_cookie else {}
   if args.test_parental_cookie:log.info("Live test using parental-control=yes cookie (test only)")
   async with httpx.AsyncClient(timeout=35,follow_redirects=True,limits=httpx.Limits(max_connections=WORKERS),headers=test_headers) as client:
    if args.probe_batch:
     slugname=args.category or "18-year-old"
     results=await asyncio.gather(*(fetch(client,slugname,p) for p in (1,2,3)),return_exceptions=True)
     failed=False
     for page,result in enumerate(results,1):
      if isinstance(result,BaseException):
       log.error("Concurrent probe page %d: %s",page,result);failed=True
      else:
       log.info("Concurrent probe page %d: %d videos",page,len(result))
     if failed:raise RuntimeError("Concurrent probe failed: source did not return listing cards on every page")
    else:
     items=await fetch(client,args.category or "18-year-old",1)
     log.info("Live test: %s cards; no database writes",len(items))
  return
 if not 1<=PAGES<=30:raise ValueError("MAX_PAGES must be 1..30")
 with psycopg.connect(os.environ["DATABASE_URL"],autocommit=True) as db:
  init(db)
  with db.transaction():
   row=db.execute("SELECT slug,status FROM categories WHERE slug=%s FOR UPDATE" if args.category else "SELECT slug,status FROM categories WHERE status IN ('pending','running','failed') ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,sort_order LIMIT 1 FOR UPDATE SKIP LOCKED",(args.category,) if args.category else ()).fetchone()
   if not row:
    log.info("No unfinished category");return
   s,state=row
   if state=="completed":log.info("Already completed %s",s);return
   db.execute("UPDATE categories SET status='running',last_error=NULL,updated_at=now() WHERE slug=%s",(s,))
  done={x[0] for x in db.execute("SELECT page FROM scrape_pages WHERE category_slug=%s",(s,))}
  todo=[p for p in range(1,PAGES+1) if p not in done]
  log.info("%s: %s successful pages; %s remaining",s,len(done),len(todo))
  try:
   production_headers={"Cookie":"parental-control=yes"}
   log.info("Production request cookie enabled: parental-control=yes")
   async with httpx.AsyncClient(timeout=35,follow_redirects=True,limits=httpx.Limits(max_connections=WORKERS),headers=production_headers) as client:
    for i in range(0,len(todo),WORKERS):
     batch=todo[i:i+WORKERS]
     results=await asyncio.gather(*(fetch(client,s,p) for p in batch),return_exceptions=True)
     errors=[];not_found=[]
     for page,result in zip(batch,results):
      if isinstance(result,BaseException):errors.append(f"page {page}: {result}")
      elif result is None:not_found.append(page)
      else:save(db,s,page,result);log.info("%s page %d/%d: %d videos",s,page,PAGES,len(result))
     if errors:raise RuntimeError("; ".join(errors))
     if not_found:
      end=min(not_found)-1
      counts=db.execute("SELECT page,item_count FROM scrape_pages WHERE category_slug=%s AND page IN (%s,%s)",(s,end-1,end)).fetchall()
      lengths=dict(counts)
      processed=db.execute("SELECT count(*) FROM scrape_pages WHERE category_slug=%s AND page BETWEEN 1 AND %s",(s,end)).fetchone()[0]
      if end>=2 and processed==end and end in lengths and end-1 in lengths and 0<lengths[end]<lengths[end-1]:
       with db.transaction():
        db.execute("UPDATE categories SET status='completed',terminal_page=%s,completed_at=now(),last_error=NULL,updated_at=now() WHERE slug=%s",(end,s))
       log.info("COMPLETE %s: natural end detected at page %d (next page 404; last page has %d items)",s,end,lengths[end])
       return
      raise RuntimeError(f"Page {min(not_found)} returned 404, but end-of-pagination could not be verified; no completion recorded")
   count=db.execute("SELECT count(*) FROM scrape_pages WHERE category_slug=%s AND page BETWEEN 1 AND %s",(s,PAGES)).fetchone()[0]
   if count!=PAGES:raise RuntimeError(f"Only {count}/{PAGES} pages saved")
   if PAGES==30:
    with db.transaction():db.execute("UPDATE categories SET status='completed',terminal_page=%s,completed_at=now(),updated_at=now() WHERE slug=%s",(PAGES,s))
    log.info("COMPLETE %s",s)
   else:log.info("Partial run; not marking complete")
  except Exception as e:
   with db.transaction():db.execute("UPDATE categories SET status='blocked',last_error=%s,updated_at=now() WHERE slug=%s",(str(e)[:1000],s))
   raise
if __name__=="__main__":
 p=argparse.ArgumentParser()
 p.add_argument("--mode",choices=["test","production"],default="production")
 p.add_argument("--category",default="")
 p.add_argument("--live",action="store_true")
 p.add_argument("--probe-batch",action="store_true")
 p.add_argument("--test-parental-cookie",action="store_true")
 asyncio.run(run(p.parse_args()))
