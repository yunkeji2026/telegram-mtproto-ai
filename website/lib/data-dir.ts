import fs from "fs";
import os from "os";
import path from "path";

// Brand rename (yuntech-leads → hualing-leads) with zero-loss migration:
// keep reading the legacy dir if it still holds data and the new one isn't set up yet.
const LEGACY_DIR = path.join(os.homedir(), "yuntech-leads");
const PRIMARY_DIR = process.env.LEADS_DIR || path.join(os.homedir(), "hualing-leads");

function resolveDataDir(): string {
  if (process.env.LEADS_DIR) return process.env.LEADS_DIR;
  try {
    if (!fs.existsSync(PRIMARY_DIR) && fs.existsSync(LEGACY_DIR)) return LEGACY_DIR;
  } catch {
    /* fs may be unavailable in edge runtime — fall through to primary */
  }
  return PRIMARY_DIR;
}

export const DATA_DIR = resolveDataDir();

const LEGACY_ANALYTICS = path.join(os.homedir(), "yuntech-analytics");
const PRIMARY_ANALYTICS = path.join(os.homedir(), "hualing-analytics");

function resolveAnalyticsDir(): string {
  if (process.env.ANALYTICS_DIR) return process.env.ANALYTICS_DIR;
  try {
    if (!fs.existsSync(PRIMARY_ANALYTICS) && fs.existsSync(LEGACY_ANALYTICS)) return LEGACY_ANALYTICS;
  } catch {
    /* ignore */
  }
  return PRIMARY_ANALYTICS;
}

export const ANALYTICS_DIR = resolveAnalyticsDir();
