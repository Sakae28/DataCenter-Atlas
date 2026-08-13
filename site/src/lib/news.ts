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
  published_at: z.string(),
  featured: z.boolean(),
  sources: z.array(SourceSchema).min(1),
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
  return { days, source };
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

export function sourceNames(story: NewsStory, max = 3): { shown: string[]; extra: number } {
  const names = story.sources.map((s) => s.name);
  return { shown: names.slice(0, max), extra: Math.max(0, names.length - max) };
}
