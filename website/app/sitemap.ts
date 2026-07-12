import type { MetadataRoute } from "next";
import { SITE_URL } from "@/lib/site";

export default function sitemap(): MetadataRoute.Sitemap {
  const now = new Date();
  const landing = ["/voice", "/face", "/interpreting", "/asset-safe", "/nurture"].flatMap((slug) => [
    { url: `${SITE_URL}${slug}`, lastModified: now, changeFrequency: "weekly" as const, priority: 0.8 },
    { url: `${SITE_URL}/en${slug}`, lastModified: now, changeFrequency: "weekly" as const, priority: 0.7 },
  ]);
  const download = [
    { url: `${SITE_URL}/download`, lastModified: now, changeFrequency: "weekly" as const, priority: 0.9 },
    { url: `${SITE_URL}/en/download`, lastModified: now, changeFrequency: "weekly" as const, priority: 0.8 },
  ];
  return [
    {
      url: SITE_URL,
      lastModified: now,
      changeFrequency: "weekly",
      priority: 1,
    },
    {
      url: `${SITE_URL}/en`,
      lastModified: now,
      changeFrequency: "weekly",
      priority: 0.9,
    },
    ...download,
    ...landing,
    {
      url: `${SITE_URL}/privacy`,
      lastModified: now,
      changeFrequency: "yearly",
      priority: 0.3,
    },
    {
      url: `${SITE_URL}/en/privacy`,
      lastModified: now,
      changeFrequency: "yearly",
      priority: 0.3,
    },
    {
      url: `${SITE_URL}/terms`,
      lastModified: now,
      changeFrequency: "yearly",
      priority: 0.3,
    },
    {
      url: `${SITE_URL}/en/terms`,
      lastModified: now,
      changeFrequency: "yearly",
      priority: 0.3,
    },
  ];
}
