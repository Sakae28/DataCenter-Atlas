/**
 * Site-wide i18n. English is the canonical language at /; Simplified Chinese
 * lives under /zh/. Dynamic content (story titles, summaries, blurbs) is
 * translated by the pipeline into *_zh fields; UI chrome uses STRINGS below.
 */
import type { Region } from './news';

export type Lang = 'en' | 'zh';

/** URL prefix for a language's section of the site ('' or '/zh'). */
export function langPrefix(lang: Lang): string {
  return lang === 'zh' ? '/zh' : '';
}

/**
 * Localized field access: returns obj["<field>_zh"] when zh is active and
 * the translation exists, otherwise the canonical English field. Works on
 * plain objects (stories, reports, company meta).
 */
export function lf(obj: Record<string, unknown>, field: string, lang: Lang): string {
  if (lang === 'zh') {
    const zh = obj[`${field}_zh`];
    if (typeof zh === 'string' && zh.trim()) return zh;
  }
  const v = obj[field];
  return typeof v === 'string' ? v : '';
}

export const REGION_LABELS_ZH: Record<Region, string> = {
  china: '中国',
  japan: '日本',
  korea: '韩国',
  australia: '澳大利亚',
  'southeast-asia': '东南亚',
  global: '全球',
};

export const CATEGORY_LABELS_ZH: Record<string, string> = {
  'Hyperscale & Cloud': '超大规模与云',
  'Neocloud & GPU Cloud': 'Neocloud 与 GPU 云',
  'Colocation': '托管 Colocation',
  'Telco & Carrier': '电信运营商',
  'Developer & Investor': '开发商与投资方',
  'Other operators': '其他运营商',
};

export const STATUS_LABELS_ZH: Record<string, string> = {
  operational: '运营中',
  under_construction: '在建',
  announced: '已宣布',
  planned: '规划中',
  prospective: '意向扩建',
  leased: '租赁',
  unknown: '未知',
};

const en = {
  'nav.section': 'Digest',
  'nav.latest': 'Latest',
  'nav.reports': 'Reports',
  'nav.pipeline': 'Pipeline',
  'nav.companies': 'Companies',
  'nav.saved': 'Saved',
  'nav.about': 'About',
  'nav.switch': '中文',
  'footer.note': 'DataCenter Atlas is an automated news digest. Scores and summaries are AI-generated heuristics, not editorial judgment. All sources are credited and linked.',
  'footer.rss': 'RSS feed',
  'skip': 'Skip to content',
  'common.back': 'Back',
  'common.save': 'Save',
  'common.saved': 'Saved — click to remove',
  'common.follow': 'Follow',
  'common.following': 'Following',
  'common.followTip': 'Follow — get an on-site alert when this company has news or project updates',
  'common.followingTip': 'Following — click to unfollow',
  'common.viewOriginal': 'View original',
  'common.sources': 'sources',
  'common.source': 'source',
  'common.featured': 'Featured',
  'common.whyItMatters': 'Why it matters',
  'common.aiScoreTip': 'AI relevance score (0–100): how central this story is to the APAC data center industry',
  'common.alsoReported': 'Also reported by',
  'common.projectLabel': 'Project:',
  'common.story': 'story',
  'common.stories': 'stories',
  'common.website': 'Website',
  'common.newActivity': 'New activity:',
  'common.navNewActivity': 'Followed companies have new activity',
} as const;

export type StringKey = keyof typeof en;

const zh: Record<StringKey, string> = {
  'nav.section': '资讯',
  'nav.latest': '最新',
  'nav.reports': '周报/月报',
  'nav.pipeline': '项目管线',
  'nav.companies': '公司',
  'nav.saved': '收藏',
  'nav.about': '关于',
  'nav.switch': 'EN',
  'footer.note': 'DataCenter Atlas 是一个自动化新闻聚合平台。评分与摘要由 AI 生成，仅供参考，不构成编辑观点。所有来源均已署名并附链接。',
  'footer.rss': 'RSS 订阅',
  'skip': '跳到正文',
  'common.back': '返回',
  'common.save': '收藏',
  'common.saved': '已收藏 — 点击取消',
  'common.follow': '追踪',
  'common.following': '追踪中',
  'common.followTip': '追踪 — 该公司有新闻或项目更新时会在站内提醒',
  'common.followingTip': '追踪中 — 点击取消追踪',
  'common.viewOriginal': '查看原文',
  'common.sources': '个来源',
  'common.source': '个来源',
  'common.featured': '精选',
  'common.whyItMatters': '为什么重要',
  'common.aiScoreTip': 'AI 相关性评分（0–100）：衡量该新闻与亚太数据中心行业的相关程度',
  'common.alsoReported': '其他报道：',
  'common.projectLabel': '项目：',
  'common.story': '条新闻',
  'common.stories': '条新闻',
  'common.website': '官网',
  'common.newActivity': '新动态：',
  'common.navNewActivity': '追踪的公司有新动态',
};

const DICT: Record<Lang, Record<StringKey, string>> = { en, zh };

export function t(lang: Lang, key: StringKey): string {
  return DICT[lang][key] ?? en[key];
}

/** The path of the current page in the other language (for the switcher). */
export function altLangPath(pathname: string, base: string): string {
  let p = pathname.startsWith(base) ? pathname.slice(base.length) : pathname;
  if (!p.startsWith('/')) p = `/${p}`;
  return p === '/zh' || p.startsWith('/zh/') ? p.slice(3) || '/' : `/zh${p}`;
}
