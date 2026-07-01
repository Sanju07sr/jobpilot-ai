import os
import json
import time
import re
import random
import requests
import smtplib
import hashlib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from bs4 import BeautifulSoup

# ── Environment variables ───────────────────────────────────────────────────
SENDER_EMAIL     = os.environ['SENDER_EMAIL']
SENDER_PASSWORD  = os.environ['SENDER_PASSWORD']
RECIPIENT_EMAIL  = os.environ['RECIPIENT_EMAIL']
OPENAI_API_KEY   = os.environ.get('OPENAI_API_KEY', '')
MIN_MATCH_PCT    = int(os.environ.get('MIN_MATCH_PCT', '55'))
SEEN_JOBS_FILE   = 'seen_jobs.json'

# ── Dynamic inputs from webapp / Make.com / workflow_dispatch ───────────────
USER_LINKEDIN_URL = os.environ.get('USER_LINKEDIN_URL', '')
USER_JOB_TITLES   = os.environ.get('USER_JOB_TITLES', '').split(',') if os.environ.get('USER_JOB_TITLES') else []
USER_LOCATIONS    = os.environ.get('USER_LOCATIONS', 'Ireland').split(',') if os.environ.get('USER_LOCATIONS') else ['Ireland']
USER_EMAIL        = os.environ.get('USER_EMAIL', os.environ.get('RECIPIENT_EMAIL', ''))
USER_CV_TEXT      = os.environ.get('USER_CV_TEXT', '')

# ── Default job title keywords (used when no user titles provided) ──────────
DEFAULT_TITLES = [
    'UiPath developer Ireland',
    'RPA developer Ireland',
    'UiPath RPA Ireland',
    'automation developer Ireland',
    'RPA engineer Ireland',
    'UiPath automation Ireland',
]

# ── Title filter: job must contain at least one of these ────────────────────
TITLE_MUST_CONTAIN = [
    'rpa', 'uipath', 'ui path', 'robotic process automation',
    'automation developer', 'automation engineer', 'process automation',
    'rpa developer', 'rpa engineer', 'rpa consultant', 'rpa analyst',
    'rpa architect', 'rpa lead', 'rpa manager',
    'blue prism', 'automation anywhere', 'power automate',
]

# ── Scoring keyword weights ─────────────────────────────────────────────────
HIGH_VALUE = [
    'uipath', 'rpa', 'robotic process automation', 'ui path',
    'automation anywhere', 'blue prism', 'power automate',
    'orchestrator', 'reframework', 'attended', 'unattended',
]
MEDIUM_VALUE = [
    'process automation', 'workflow', 'bot', 'automation developer',
    'automation engineer', 'digital transformation', 'python',
    'c#', '.net', 'sql', 'api', 'integration',
]

# ── User-agent pool ─────────────────────────────────────────────────────────
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0',
]


def get_headers():
    return {
        'User-Agent': random.choice(USER_AGENTS),
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    }


def load_seen():
    if os.path.exists(SEEN_JOBS_FILE):
        with open(SEEN_JOBS_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    with open(SEEN_JOBS_FILE, 'w') as f:
        json.dump(list(seen), f)


def is_relevant_title(title):
    t = title.lower()
    return any(kw in t for kw in TITLE_MUST_CONTAIN)


def keyword_score(title, description):
    text = (title + ' ' + description).lower()
    score = 0
    for kw in HIGH_VALUE:
        if kw in text:
            score += 15
    for kw in MEDIUM_VALUE:
        if kw in text:
            score += 7
    return min(score, 100)


def score_with_openai(title, description):
    if not OPENAI_API_KEY:
        return keyword_score(title, description)
    try:
        import openai
        openai.api_key = OPENAI_API_KEY
        cv_context = f'\nCandidate CV summary: {USER_CV_TEXT[:500]}' if USER_CV_TEXT else ''
        prompt = (
            f'Rate this job match 0-100 for a UiPath/RPA specialist.{cv_context}\n'
            f'Job title: {title}\n'
            f'Description: {description[:800]}\n'
            f'Reply with just the number.'
        )
        resp = openai.ChatCompletion.create(
            model='gpt-3.5-turbo',
            messages=[{'role': 'user', 'content': prompt}],
            max_tokens=5,
        )
        return int(re.search(r'\d+', resp.choices[0].message.content).group())
    except Exception:
        return keyword_score(title, description)


def fetch_linkedin_jobs(keyword, location='Ireland', start=0):
    keyword_enc = requests.utils.quote(keyword)
    location_enc = requests.utils.quote(location)
    url = (
        f'https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search'
        f'?keywords={keyword_enc}&location={location_enc}&start={start}'
    )
    try:
        resp = requests.get(url, headers=get_headers(), timeout=15)
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, 'html.parser')
        jobs = []
        for card in soup.find_all('li'):
            title_el   = card.find('h3', class_='base-search-card__title')
            company_el = card.find('h4', class_='base-search-card__subtitle')
            link_el    = card.find('a', class_='base-card__full-link')
            if not (title_el and link_el):
                continue
            url_val = link_el.get('href', '').split('?')[0]
            job_id  = hashlib.md5(url_val.encode()).hexdigest()[:12]
            jobs.append({
                'id':      job_id,
                'title':   title_el.get_text(strip=True),
                'company': company_el.get_text(strip=True) if company_el else 'Unknown',
                'url':     url_val,
            })
        return jobs
    except Exception as e:
        print(f'  Error fetching jobs: {e}')
        return []


def fetch_job_description(url):
    try:
        resp = requests.get(url, headers=get_headers(), timeout=15)
        soup = BeautifulSoup(resp.text, 'html.parser')
        desc = soup.find('div', class_='description__text')
        return desc.get_text(separator=' ', strip=True)[:2000] if desc else ''
    except Exception:
        return ''


def send_email(jobs, recipient_email=None):
    recipient = recipient_email if recipient_email else RECIPIENT_EMAIL
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f'JobPilot Alert: {len(jobs)} new UiPath/RPA job(s) in Ireland'
        msg['From']    = SENDER_EMAIL
        msg['To']      = recipient

        rows = ''.join([
            f"""
            <tr>
              <td style='padding:12px;border-bottom:1px solid #eee;'>
                <a href='{j["url"]}' style='color:#0a66c2;font-weight:bold;font-size:15px;text-decoration:none;'>
                  {j["title"]}
                </a><br>
                <small style='color:#555;'>{j["company"]} &nbsp;|&nbsp; Score: <b>{j["score"]}%</b></small>
              </td>
            </tr>
            """
            for j in jobs
        ])

        html = f"""
        <html><body style='font-family:Arial,sans-serif;color:#333;margin:0;padding:0;'>
          <div style='max-width:600px;margin:30px auto;border:1px solid #ddd;border-radius:8px;overflow:hidden;'>
            <div style='background:#0a66c2;padding:20px;'>
              <h2 style='color:white;margin:0;'>JobPilot Job Alert</h2>
              <p style='color:#cce0ff;margin:4px 0 0;'>Found {len(jobs)} new matching job(s)</p>
            </div>
            <table style='width:100%;border-collapse:collapse;'>{rows}</table>
            <div style='padding:16px;background:#f9f9f9;font-size:12px;color:#999;text-align:center;'>
              Automated by JobPilot &nbsp;&bull;&nbsp; {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}
            </div>
          </div>
        </body></html>
        """

        msg.attach(MIMEText(html, 'html'))
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.sendmail(SENDER_EMAIL, recipient, msg.as_string())
        print(f'  Email sent to {recipient}')
        return True
    except Exception as e:
        print(f'  Email error: {e}')
        return False


def build_search_queries():
    if USER_JOB_TITLES and USER_LOCATIONS:
        queries = []
        for loc in USER_LOCATIONS:
            for title in USER_JOB_TITLES:
                queries.append(f'{title.strip()} {loc.strip()}')
        return queries
    return DEFAULT_TITLES


def main():
    print(f'JobPilot Agent -- {datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}')
    print('Scanning LinkedIn for UiPath/RPA jobs...')

    seen   = load_seen()
    print(f'Previously seen jobs: {len(seen)}')

    queries    = build_search_queries()
    all_jobs   = []
    seen_urls  = set()

    for query in queries:
        print(f'Searching: {query}')
        time.sleep(random.uniform(2, 5))
        jobs = fetch_linkedin_jobs(query)
        print(f'  Found {len(jobs)} raw results')

        for job in jobs:
            if job['url'] in seen_urls:
                continue
            seen_urls.add(job['url'])

            if not is_relevant_title(job['title']):
                print(f'  SKIP (irrelevant title): {job["title"]}')
                continue

            if job['id'] in seen:
                print(f'  SKIP (already seen): {job["title"]}')
                continue

            all_jobs.append(job)
            print(f'  NEW job: {job["title"]} @ {job["company"]}')

    print(f'New jobs to evaluate: {len(all_jobs)}')

    jobs_to_alert = []
    for job in all_jobs:
        print(f'  Scoring: {job["title"]} @ {job["company"]}')
        desc         = fetch_job_description(job['url'])
        time.sleep(random.uniform(1, 3))
        score        = score_with_openai(job['title'], desc)
        job['score'] = score
        seen.add(job['id'])
        print(f'  Score: {score}%')
        if score >= MIN_MATCH_PCT:
            jobs_to_alert.append(job)
            print(f'  MATCH! Added to alert list.')
        else:
            print(f'  Score too low ({score}% < {MIN_MATCH_PCT}%), skipping.')

    save_seen(seen)

    if jobs_to_alert:
        jobs_to_alert.sort(key=lambda x: x['score'], reverse=True)
        print(f'Sending email alert for {len(jobs_to_alert)} job(s)...')
        send_email(jobs_to_alert, USER_EMAIL)
    else:
        print('No new jobs above threshold this run.')

    print(f'Done. Alerts: {len(jobs_to_alert)} | Total tracked: {len(seen)}')


if __name__ == '__main__':
    main()
