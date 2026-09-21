import type { APIRoute } from 'astro';

import { loadCompanies } from '../lib/companies';
import { loadAllStories } from '../lib/news';

/**
 * Per-company activity summary for the follow/alert feature. The client
 * compares each entry's `latest` against the visitor's last-seen stamp
 * (localStorage) to decide whether a followed company has new activity.
 * Regenerated on every build, so daily deploys keep it fresh.
 */
export const GET: APIRoute = () => {
  const stories = loadAllStories();
  const activity: Record<string, {
    name: string;
    /** Newest story tagged with this company (ISO), if any. */
    latestNews: string | null;
    /** Stories in the trailing 30 days (relative to the newest story). */
    news30d: number;
    /** Newest projects.json last_updated for this operator, if any. */
    latestProjectUpdate: string | null;
    /** Newest activity of either kind — the alert comparison value. */
    latest: string | null;
  }> = {};

  for (const company of loadCompanies()) {
    const names = new Set([company.name.toLowerCase(), company.operator.toLowerCase()]);
    const hits = stories.filter(({ story }) =>
      (story.companies ?? []).some((c) => names.has(c.toLowerCase())),
    );
    const latestNews = hits[0]?.story.published_at ?? null;
    let news30d = 0;
    if (latestNews) {
      const cutoff = Date.parse(latestNews) - 30 * 86400_000;
      news30d = hits.filter(({ story }) => Date.parse(story.published_at) >= cutoff).length;
    }
    const latestProjectUpdate =
      company.projects
        .map((p) => p.last_updated)
        .filter((d): d is string => !!d)
        .sort()
        .pop() ?? null;
    const latest = [latestNews, latestProjectUpdate]
      .filter((d): d is string => !!d)
      .sort()
      .pop() ?? null;
    activity[company.slug] = {
      name: company.name,
      latestNews,
      news30d,
      latestProjectUpdate,
      latest,
    };
  }

  return new Response(JSON.stringify({ generatedAt: new Date().toISOString(), companies: activity }), {
    headers: { 'Content-Type': 'application/json; charset=utf-8' },
  });
};
