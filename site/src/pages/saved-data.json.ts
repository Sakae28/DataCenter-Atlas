import type { APIRoute } from 'astro';

import { loadAllStories, formatDate, sourceDisplayName } from '../lib/news';
import { loadProjects, formatCapacity, STATUS_META } from '../lib/projects';
import { companySlugMap } from '../lib/companies';

/**
 * Compact stubs backing the /saved/ page. The browser fetches this once (on
 * demand) and filters client-side by the ids stored in localStorage
 * ("dc-favs"), so field names are kept short — 2.7k projects would otherwise
 * bloat the page HTML.
 */
export const GET: APIRoute = () => {
  const slugs = companySlugMap();
  const news = loadAllStories().map(({ story }) => ({
    id: story.id,
    t: story.title,
    d: formatDate(story.published_at.slice(0, 10)),
    s: sourceDisplayName(story.sources[0], story),
    co: (story.companies ?? []).map((c) => [c, slugs.get(c.toLowerCase()) ?? null]),
  }));
  const projects = loadProjects().projects.map((p) => ({
    id: p.id,
    n: p.name,
    o: p.operator,
    loc: p.city === p.country ? p.city : [p.city, p.country].filter(Boolean).join(', '),
    cap: formatCapacity(p).value,
    sl: STATUS_META[p.status].label,
    sc: STATUS_META[p.status].className,
  }));
  return new Response(JSON.stringify({ news, projects }), {
    headers: { 'Content-Type': 'application/json' },
  });
};
