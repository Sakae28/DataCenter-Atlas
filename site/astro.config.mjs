// @ts-check
import { defineConfig } from 'astro/config';
import sitemap from '@astrojs/sitemap';

export default defineConfig({
  site: 'https://sakae28.github.io',
  base: '/DataCenter-Atlas',
  output: 'static',
  integrations: [sitemap()],
});
