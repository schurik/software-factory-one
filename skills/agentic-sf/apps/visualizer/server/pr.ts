/**
 * Live pull-request state, asked of the forge CLI at view time.
 *
 * The trace knows the url the integration phase recorded and nothing more — it
 * cannot know the PR was merged an hour after the run ended. So this is the one
 * place the visualizer looks outside its database.
 *
 * It stays an OBSERVER either way. Reading a PR's state is what this app is
 * for; what it must never become is something that acts on the forge, so this
 * module runs exactly one read-only command and has no write path at all.
 *
 * Best-effort by construction. No `gh` on PATH, no url, an unauthenticated
 * shell, no network — each returns `available: false` with a reason, and the UI
 * falls back to showing the url alone. An observability panel that goes blank
 * because a CLI is missing would be worse than one that never had the field.
 */
import type { PrStatus } from "../shared/types.ts";

/** How long an answer is reused. PR state changes on human timescales; the UI
 *  polls twice a second, and every one of those must not become a gh call. */
const TTL_MS = 60_000;
const TIMEOUT_MS = 8_000;

interface Entry {
  status: PrStatus;
  at: number;
}

const cache = new Map<string, Entry>();
/** In-flight calls, so ten cards mounting at once make one gh call, not ten. */
const inflight = new Map<string, Promise<PrStatus>>();

let cliChecked = false;
let cliPath = "";

/** Whether a forge CLI exists at all. Probed once — it does not appear later. */
function forgeCli(): string {
  if (!cliChecked) {
    cliChecked = true;
    cliPath = Bun.which("gh") ?? "";
  }
  return cliPath;
}

function unavailable(reason: string, url?: string): PrStatus {
  return { available: false, reason, url };
}

async function ask(url: string): Promise<PrStatus> {
  const cli = forgeCli();
  if (!cli) {
    return unavailable("no gh on PATH — showing the recorded url only", url);
  }

  const proc = Bun.spawn(
    [cli, "pr", "view", url, "--json",
     "state,isDraft,title,statusCheckRollup,reviewDecision"],
    { stdout: "pipe", stderr: "pipe" },
  );
  const timer = setTimeout(() => proc.kill(), TIMEOUT_MS);
  let stdout = "";
  let stderr = "";
  try {
    [stdout, stderr] = await Promise.all([
      new Response(proc.stdout).text(),
      new Response(proc.stderr).text(),
    ]);
    await proc.exited;
  } finally {
    clearTimeout(timer);
  }

  if (proc.exitCode !== 0) {
    return unavailable(
      `gh pr view failed: ${(stderr || stdout).trim().slice(-200)}`,
      url,
    );
  }

  try {
    const raw = JSON.parse(stdout) as {
      state?: string;
      isDraft?: boolean;
      title?: string;
      reviewDecision?: string;
      // Two node shapes in one list: CheckRun carries status/conclusion, and
      // StatusContext (the older commit-status API, which plenty of CI still
      // posts) carries `state`. Reading only the first shape pinned any
      // status-based CI at "checks running" forever — including a red one.
      statusCheckRollup?: { conclusion?: string; status?: string; state?: string }[];
    };
    return {
      available: true,
      url,
      state: raw.state,
      draft: raw.isDraft,
      title: raw.title,
      checks: rollup(raw.statusCheckRollup),
      review: raw.reviewDecision || undefined,
      ttl_seconds: TTL_MS / 1000,
    };
  } catch (error) {
    return unavailable(`gh pr view did not return JSON: ${String(error)}`, url);
  }
}

/**
 * One word for a list of checks. Anything unfinished dominates a green run,
 * and one failure dominates everything — a rollup that reported SUCCESS while
 * a job was still queued would be a lie with a checkmark on it.
 */
const BAD = new Set(["FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "ERROR"]);
const GOOD = new Set(["SUCCESS", "NEUTRAL", "SKIPPED"]);

function rollup(
  checks?: { conclusion?: string; status?: string; state?: string }[],
): string | undefined {
  if (!checks?.length) return undefined;
  let pending = false;
  for (const check of checks) {
    // A CheckRun answers through conclusion (once status is COMPLETED); a
    // StatusContext answers through state and has neither of the others.
    const verdict = (check.conclusion || check.state || "").toUpperCase();
    const status = (check.status ?? "").toUpperCase();
    if (BAD.has(verdict)) return "FAILURE";
    if (GOOD.has(verdict) && status !== "IN_PROGRESS" && status !== "QUEUED") continue;
    pending = true;
  }
  return pending ? "PENDING" : "SUCCESS";
}

export async function prStatus(url: string | null | undefined): Promise<PrStatus> {
  if (!url) return unavailable("this run recorded no pull request");

  const hit = cache.get(url);
  if (hit && Date.now() - hit.at < TTL_MS) return hit.status;

  const pending = inflight.get(url);
  if (pending) return pending;

  const call = ask(url)
    .then((status) => {
      cache.set(url, { status, at: Date.now() });
      return status;
    })
    .finally(() => inflight.delete(url));
  inflight.set(url, call);
  return call;
}

/** True when a forge CLI exists, so the UI can skip asking at all. */
export function forgeAvailable(): boolean {
  return forgeCli() !== "";
}
