import { readdirSync, readFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { z } from 'zod';

export const REGIONS = [
  'china',
  'japan',
  'korea',
  'australia',
  'southeast-asia',
  'global',
] as const;

export type Region = (typeof REGIONS)[number];

export const REGION_LABELS: Record<Region, string> = {
  china: 'China',
  japan: 'Japan',
  korea: 'Korea',
  australia: 'Australia',
  'southeast-asia': 'Southeast Asia',
  global: 'Global',
};

const SourceSchema = z.object({
  name: z.string(),
  url: z.string().url(),
  type: z.string().optional(),
  // Real outlet name for Google News feed entries (parsed from the RSS
  // title's " - <publisher>" suffix by the pipeline).
  publisher: z.string().optional(),
});

const StorySchema = z.object({
  id: z.string(),
  title: z.string(),
  summary: z.string(),
  why_it_matters: z.string(),
  score: z.number().min(0).max(100).nullable(),
  heat: z.number().int().nonnegative(),
  regions: z.array(z.enum(REGIONS)).min(1),
  topics: z.array(z.string()).max(4),
  // Optional canonical company names mentioned in the story (max 6),
  // added by the pipeline's backfill step.
  companies: z.array(z.string()).max(6).optional(),
  published_at: z.string(),
  // "day" when the source only gives a calendar date (no time of day) —
  // the UI renders a bare date instead of a fabricated time.
  published_precision: z.enum(['day', 'time']).optional(),
  featured: z.boolean(),
  sources: z.array(SourceSchema).min(1),
  // Optional full-text fields added by the pipeline's extraction step
  // (absent or "" when extraction failed).
  content: z.string().optional(),
  content_url: z.string().url().optional(),
});

const NewsDaySchema = z.object({
  date: z.string().regex(/^\d{4}-\d{2}-\d{2}$/),
  generated_at: z.string(),
  hot: z.array(z.string()).max(5),
  stories: z.array(StorySchema),
});

export type NewsSource = z.infer<typeof SourceSchema>;
export type NewsStory = z.infer<typeof StorySchema>;
export type NewsDay = z.infer<typeof NewsDaySchema>;

const DATA_DIR = fileURLToPath(new URL('../../../data/news', import.meta.url));
const FIXTURES_DIR = fileURLToPath(new URL('../../fixtures/news', import.meta.url));

function readDayFiles(dir: string): NewsDay[] {
  if (!existsSync(dir)) return [];
  const days: NewsDay[] = [];
  for (const file of readdirSync(dir)) {
    if (!file.endsWith('.json')) continue;
    try {
      const raw = JSON.parse(readFileSync(join(dir, file), 'utf-8'));
      const parsed = NewsDaySchema.safeParse(raw);
      if (parsed.success) {
        days.push(parsed.data);
      } else {
        console.warn(`[news] skipping invalid file ${file}:`, parsed.error.issues[0]?.message);
      }
    } catch (err) {
      console.warn(`[news] failed to read ${file}:`, err);
    }
  }
  return days;
}

/**
 * Load all news days, newest date first.
 * Reads ../data/news (pipeline output); falls back to site/fixtures/news
 * for development when the pipeline has produced nothing yet.
 */
export function loadNewsDays(): { days: NewsDay[]; source: 'data' | 'fixtures' } {
  let days = readDayFiles(DATA_DIR);
  let source: 'data' | 'fixtures' = 'data';
  if (days.length === 0) {
    days = readDayFiles(FIXTURES_DIR);
    source = 'fixtures';
    if (days.length > 0) {
      console.warn('[news] ../data/news is empty or missing — using fixtures from site/fixtures/news');
    }
  }
  days.sort((a, b) => (a.date < b.date ? 1 : -1));
  // Never trust the file order: enforce newest-first within each day.
  for (const day of days) {
    day.stories.sort((a, b) => parseTime(b.published_at) - parseTime(a.published_at));
  }
  return { days, source };
}

function parseTime(iso: string): number {
  const t = Date.parse(iso);
  return Number.isNaN(t) ? 0 : t;
}

const WEEKDAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];

export function weekdayOf(date: string): string {
  // Parse as UTC noon to avoid timezone drift on the date itself.
  return WEEKDAYS[new Date(`${date}T12:00:00Z`).getUTCDay()];
}

export function formatDate(date: string): string {
  return new Date(`${date}T12:00:00Z`).toLocaleDateString('en-US', {
    year: 'numeric',
    month: 'long',
    day: 'numeric',
    timeZone: 'UTC',
  });
}

/** HH:MM in UTC from an ISO timestamp. */
export function formatTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '--:--';
  const hh = String(d.getUTCHours()).padStart(2, '0');
  const mm = String(d.getUTCMinutes()).padStart(2, '0');
  return `${hh}:${mm}`;
}

/** "August 13, 2026 · 01:57 UTC" from an ISO timestamp. */
export function formatDateTime(iso: string): string {
  if (Number.isNaN(Date.parse(iso))) return iso;
  return `${formatDate(iso.slice(0, 10))} · ${formatTime(iso)} UTC`;
}

/** "16 hours ago" style relative time from an ISO timestamp. */
export function relativeTime(iso: string, now = Date.now()): string {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '';
  const diff = Math.max(0, now - t);
  const minutes = Math.floor(diff / 60000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? '' : 's'} ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? '' : 's'} ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days} day${days === 1 ? '' : 's'} ago`;
  const months = Math.floor(days / 30);
  if (months < 12) return `${months} month${months === 1 ? '' : 's'} ago`;
  const years = Math.floor(months / 12);
  return `${years} year${years === 1 ? '' : 's'} ago`;
}

/**
 * All stories across all loaded days, newest day first.
 * Used by the [id] route's getStaticPaths. Duplicate ids are dropped
 * (first occurrence, i.e. the newest day's copy, wins).
 */
export function loadAllStories(): { story: NewsStory; dayDate: string }[] {
  const { days } = loadNewsDays();
  const seen = new Set<string>();
  const out: { story: NewsStory; dayDate: string }[] = [];
  for (const day of days) {
    for (const story of day.stories) {
      if (seen.has(story.id)) {
        console.warn(`[news] duplicate story id "${story.id}" (${day.date}) — keeping first occurrence`);
        continue;
      }
      seen.add(story.id);
      out.push({ story, dayDate: day.date });
    }
  }
  return out;
}

export function sourceNames(story: NewsStory, max = 3): { shown: string[]; extra: number } {
  const names = story.sources.map((s) => s.name);
  return { shown: names.slice(0, max), extra: Math.max(0, names.length - max) };
}

const normForCompare = (s: string) =>
  s.toLowerCase().replace(/[^a-z0-9 ]+/g, ' ').replace(/\s+/g, ' ').trim();

/**
 * Defensive display check: some pipeline-era summaries just repeat the
 * headline (Google News RSS "snippets" are the headline text). Returns true
 * only when the summary exists AND adds something beyond the title —
 * equal-after-normalization, prefix-of-either, or >0.9 token-overlap
 * summaries are treated as duplicates and should not be rendered.
 */
export function hasDistinctSummary(title: string, summary: string): boolean {
  const t = normForCompare(title);
  const s = normForCompare(summary);
  if (!s) return false;
  if (!t) return true;
  if (t === s || t.startsWith(s) || s.startsWith(t)) return false;
  const tTokens = new Set(t.split(' '));
  const sTokens = new Set(s.split(' '));
  let shared = 0;
  for (const tok of sTokens) if (tTokens.has(tok)) shared++;
  const dice = (2 * shared) / (tTokens.size + sTokens.size);
  return dice <= 0.9;
}

/** Well-known outlet brand names by hostname (fallback: the hostname). */
const DOMAIN_BRANDS: Record<string, string> = {
  'nbcnews.com': 'NBC News',
  'koreaherald.com': 'The Korea Herald',
  'koreatimes.co.kr': 'The Korea Times',
  'japantimes.co.jp': 'The Japan Times',
  'asia.nikkei.com': 'Nikkei Asia',
  'nikkei.com': 'Nikkei',
  'thestandard.com.hk': 'The Standard',
  'business.inquirer.net': 'Inquirer.net',
  'thejakartapost.com': 'The Jakarta Post',
  'en.tempo.co': 'Tempo',
  'sedaily.com': 'Seoul Economic Daily',
  'biz.chosun.com': 'Chosun Biz',
  'mk.co.kr': 'Maeil Business News',
  'datacenterdynamics.com': 'Data Center Dynamics',
  'datacenterknowledge.com': 'Data Center Knowledge',
  'w.media': 'W.Media',
  'capacitymedia.com': 'Capacity Media',
  'finance.yahoo.com': 'Yahoo Finance',
  'moomoo.com': 'Moomoo News',
  'newsonjapan.com': 'News On Japan',
  'urbanland.uli.org': 'Urban Land',
  'simplywall.st': 'Simply Wall St',
  'proactiveinvestors.com': 'Proactive Investors',
  'mlex.com': 'MLex',
  'crnasia.com': 'CRN Asia',
  'digitimes.com': 'DIGITIMES',
  'thebambooworks.com': 'Bamboo Works',
  'economictimes.indiatimes.com': 'The Economic Times',
  'marketsandmarkets.com': 'MarketsandMarkets',
  'reuters.com': 'Reuters',
  'bloomberg.com': 'Bloomberg',
  'channelnewsasia.com': 'CNA',
  'straitstimes.com': 'The Straits Times',
  'scmp.com': 'South China Morning Post',
  'zdnet.com': 'ZDNET',
  'techcrunch.com': 'TechCrunch',
};

/** Brand name for a hostname, stripping subdomains until a match. */
export function domainLabel(url: string): string {
  let host: string;
  try {
    host = new URL(url).hostname;
  } catch {
    return url;
  }
  const parts = host.replace(/^www\./, '').split('.');
  for (let i = 0; i < parts.length - 1; i++) {
    const candidate = parts.slice(i).join('.');
    if (DOMAIN_BRANDS[candidate]) return DOMAIN_BRANDS[candidate];
  }
  return host.replace(/^www\./, '');
}

/**
 * Display name for a source entry. Google News entries are named after the
 * feed query ("GN: Data Center Japan") which is meaningless to readers —
 * prefer the real outlet: the parsed publisher, or (for older files without
 * it) the brand behind the decoded full-text URL when this is the primary
 * source.
 */
export function sourceDisplayName(source: NewsSource, story?: NewsStory): string {
  if (source.publisher) return source.publisher;
  if (
    story &&
    source.name.startsWith('GN:') &&
    story.content_url &&
    story.sources[0]?.url === source.url
  ) {
    return domainLabel(story.content_url);
  }
  return source.name;
}
