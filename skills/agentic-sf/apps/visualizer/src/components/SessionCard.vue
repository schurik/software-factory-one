<script setup lang="ts">
import { computed, onMounted, onUnmounted, shallowRef, watch } from 'vue'
import type { EventRow, PrStatus, SessionSummary } from '../lib/types'
import { archiveSession, fetchEvents, fetchPrStatus } from '../lib/api'
import { axisTicks, fmtDate, fmtOffset, ts } from '../lib/format'
import { agentColor, dotColor, eventLabel } from '../lib/events'
import { hrefFor } from '../lib/router'
import StatusChip from './StatusChip.vue'
import StatChip from './StatChip.vue'
import PhaseDots from './PhaseDots.vue'

const props = defineProps<{ session: SessionSummary; nowMs: number }>()
const emit = defineEmits<{ archived: [adwId: string] }>()

// The card is an <a>; the button lives inside it, so the click must not
// navigate. Told the parent optimistically — the poll would take up to half a
// second to drop the card, and a triage click should feel instant.
async function archive(event: MouseEvent) {
  event.preventDefault()
  event.stopPropagation()
  emit('archived', props.session.adw_id)
  try {
    await archiveSession(props.session.adw_id)
  } catch {
    emit('archived', '')   // signals the parent to re-sync from the server
  }
}

// Each card tails its own event stream: one full fetch on mount, then the
// same rowid-cursor poll as the trace view — but only while the run is live.
const events = shallowRef<EventRow[]>([])
let cursor = 0
let inflight = false
let timer: ReturnType<typeof setInterval> | undefined

function stopPolling() {
  clearInterval(timer)
  timer = undefined
}

async function pull() {
  if (inflight) return
  inflight = true
  try {
    const fresh: EventRow[] = []
    let page
    do {
      // Cursor pagination is inherently sequential: each request needs the previous cursor.
      // oxlint-disable-next-line no-await-in-loop
      page = await fetchEvents(props.session.adw_id, cursor, 1000)
      cursor = Math.max(cursor, page.cursor)
      fresh.push(...page.events)
    } while (page.has_more)
    if (fresh.length) events.value = [...events.value, ...fresh]
    if (props.session.status !== 'running') stopPolling()
  } catch {
    /* the list view surfaces api errors; a card just retries next poll */
  } finally {
    inflight = false
  }
}

onMounted(() => {
  void pull()
  void pullPrStatus()
  if (props.session.status === 'running') timer = setInterval(() => void pull(), 500)
})

// A run that finishes may have just opened its PR, so ask once more when the
// url appears — but never on every poll.
watch(() => props.session.pr_url, (url) => { if (url) void pullPrStatus() })

onUnmounted(stopPolling)

watch(
  () => props.session.status,
  (status) => {
    if (status === 'running' && !timer) timer = setInterval(() => void pull(), 500)
    // On the transition out of running, one last pull drains the tail and stops the timer.
    else if (status !== 'running') void pull()
  },
)

const running = computed(() => props.session.status === 'running')

// The work item this run came from, when it came from one. `trigger` is what
// decides — an `issue_url` without it would be a row from before the column
// existed, and guessing is how a dash becomes a wrong link. The number is the
// URL's last path segment on every forge that numbers issues; when it is not a
// number the chip falls back to the word, so an unfamiliar tracker still links.
const issue = computed(() => {
  const s = props.session
  if (s.trigger !== 'issue' || !s.issue_url) return null
  const tail = s.issue_url.replace(/\/+$/, '').split('/').at(-1) ?? ''
  return { url: s.issue_url, label: /^\d+$/.test(tail) ? `#${tail}` : 'issue' }
})

// The pull request the run's branch became. The url comes from the trace, so it
// is known the moment the integration phase ran; the STATE does not, and is
// asked of the forge separately — see below.
const pr = computed(() => {
  const url = props.session.pr_url
  if (!url) return null
  const tail = url.replace(/\/+$/, '').split('/').at(-1) ?? ''
  return { url, label: /^\d+$/.test(tail) ? `PR #${tail}` : 'PR' }
})

// Live state, fetched once per card. Deliberately NOT on the 500ms poll: a PR
// changes on human timescales, the server caches for a minute anyway, and a
// list of twenty cards must not turn into forty gh calls a second.
const prState = shallowRef<PrStatus | null>(null)

async function pullPrStatus() {
  if (!pr.value) return
  prState.value = await fetchPrStatus(props.session.adw_id)
}

// One word, and the color that goes with it. Merged reads as done rather than
// as success — the run's own status already answered whether the WORK passed,
// and these are different questions.
const prPill = computed(() => {
  const state = prState.value
  if (!state?.available) return null
  if (state.draft && state.state === 'OPEN') return { text: 'draft', tone: 'dim' }
  if (state.state === 'MERGED') return { text: 'merged', tone: 'done' }
  if (state.state === 'CLOSED') return { text: 'closed', tone: 'bad' }
  if (state.checks === 'FAILURE') return { text: 'checks failed', tone: 'bad' }
  if (state.checks === 'PENDING') return { text: 'checks running', tone: 'wait' }
  if (state.review === 'CHANGES_REQUESTED') return { text: 'changes requested', tone: 'wait' }
  if (state.review === 'APPROVED') return { text: 'approved', tone: 'good' }
  if (state.state === 'OPEN') return { text: 'open', tone: 'good' }
  return null
})

const prTitle = computed(() => {
  const state = prState.value
  if (!state) return pr.value?.url ?? ''
  if (!state.available) return state.reason ?? pr.value?.url ?? ''
  return [state.title, state.state, state.checks && `checks ${state.checks.toLowerCase()}`,
          state.review].filter(Boolean).join(' · ')
})

const range = computed(() => {
  const s = props.session
  let t0 = ts(s.started_at)
  if (!Number.isFinite(t0)) {
    t0 = Math.min(...events.value.map((e) => ts(e.started_at)).filter(Number.isFinite))
  }
  if (!Number.isFinite(t0)) t0 = props.nowMs
  let t1 = running.value ? props.nowMs : ts(s.ended_at)
  if (!Number.isFinite(t1)) {
    t1 = Math.max(...events.value.map((e) => ts(e.started_at)).filter(Number.isFinite))
  }
  if (!Number.isFinite(t1)) t1 = t0 + 1000
  return { t0, span: Math.max(t1 - t0, 1000) }
})

const ticks = computed(() => axisTicks(range.value.span, 5))

interface TimelineDot {
  id: string
  xPct: number
  color: string
  title: string
  latest: boolean
}

interface TimelineRow {
  owner: string
  color: string
  title: string
  dots: TimelineDot[]
}

// Per-agent rows: events attribute to an agent through their phase's owner.
const rows = computed<TimelineRow[]>(() => {
  const owners: string[] = []
  const ownerByPhase = new Map<string, string>()
  for (const p of props.session.phases ?? []) {
    if (p.kind !== 'agent' || !p.owner) continue
    ownerByPhase.set(p.phase_id, p.owner)
    if (!owners.includes(p.owner)) owners.push(p.owner)
  }
  if (!owners.length) return []

  const { t0, span } = range.value
  const byOwner = new Map<string, TimelineDot[]>(owners.map((o) => [o, []]))
  let latest: TimelineDot | null = null
  let latestT = -Infinity

  for (const e of events.value) {
    const owner = e.phase_id ? ownerByPhase.get(e.phase_id) : undefined
    const color = dotColor(e.type)
    if (!owner || !color) continue
    const t = ts(e.started_at)
    if (!Number.isFinite(t)) continue
    const dot: TimelineDot = {
      id: e.event_id,
      xPct: Math.min(Math.max(((t - t0) / span) * 100, 0), 100),
      color,
      title: `${e.type} ${eventLabel(e)} at ${fmtOffset(t - t0)}`,
      latest: false,
    }
    byOwner.get(owner)?.push(dot)
    if (t >= latestT) {
      latestT = t
      latest = dot
    }
  }
  if (running.value && latest) latest.latest = true

  // /api/sessions embeds agents so the labels can use config colors with no
  // extra request; historical sessions return color null → fallback palette.
  return owners.map((owner, i) => {
    const info = (props.session.agents ?? []).find((a) => a.agent === owner)
    return {
      owner,
      color: agentColor(info?.color, null, i),
      title: info?.model ? `${owner} ${info.model}` : owner,
      dots: byOwner.get(owner) ?? [],
    }
  })
})

const durationMs = computed(() => {
  const s = props.session
  const start = ts(s.started_at)
  if (!Number.isFinite(start)) return NaN
  const end = running.value ? props.nowMs : ts(s.ended_at)
  return (Number.isFinite(end) ? end : props.nowMs) - start
})

// Cards are a fixed size, so the timeline region fits exactly MAX_VISIBLE_ROWS
// row slots. A roster that overflows spends one slot on the "+N more" line and
// shows MIN_VISIBLE_ROWS agents in the rest — never fewer than three, so a
// five-agent chain still reads as a chain rather than as a pair and a count.
const MAX_VISIBLE_ROWS = 4
const MIN_VISIBLE_ROWS = 3

const overflowing = computed(() => rows.value.length > MAX_VISIBLE_ROWS)

const visibleRows = computed(() =>
  overflowing.value ? rows.value.slice(0, MIN_VISIBLE_ROWS) : rows.value,
)

const hiddenRowCount = computed(() =>
  overflowing.value ? rows.value.length - MIN_VISIBLE_ROWS : 0,
)
</script>

<template>
  <!-- A div, not an anchor. HTML forbids nesting <a>, and the parser enforces
       it by CLOSING the outer one where the inner starts — so with the issue
       and PR chips inside, everything below them stopped being part of the
       card's link. The stretched-link pattern fixes both: one anchor whose
       ::after covers the card, and chips that are ordinary links above it. -->
  <div class="card" :class="session.status">
    <a class="card-link" :href="hrefFor(session.adw_id)" :aria-label="`run ${session.adw_id}`" />
    <button
      class="card-archive"
      type="button"
      title="Archive — remove this run from review"
      aria-label="Archive run"
      @click="archive"
    >
      ×
    </button>
    <span class="card-id">{{ session.adw_id }}</span>
    <span class="card-adw" :title="session.adw_name ?? ''">{{ session.adw_name ?? '—' }}</span>
    <div v-if="issue || pr" class="card-links">
    <a
      v-if="issue"
      class="card-issue"
      :href="issue.url"
      target="_blank"
      rel="noopener noreferrer"
      :title="`started from ${issue.url}`"
      >{{ issue.label }}</a
    >
    <a
      v-if="pr"
      class="card-pr"
      :href="pr.url"
      target="_blank"
      rel="noopener noreferrer"
      :title="prTitle"
    >
      {{ pr.label }}
      <span v-if="prPill" class="pr-pill" :class="prPill.tone">{{ prPill.text }}</span>
    </a>
    </div>
    <span class="card-req" :title="session.request ?? ''">{{ session.request }}</span>

    <div v-if="rows.length" class="tl">
      <div class="tl-axis">
        <span class="tl-gutter" />
        <span class="tl-scale">
          <span
            v-for="(t, i) in ticks"
            :key="i"
            class="tl-tick"
            :class="{ edge: t.pct === 0 }"
            :style="{ left: `${t.pct}%` }"
            >{{ t.label }}</span
          >
        </span>
      </div>
      <div v-for="row in visibleRows" :key="row.owner" class="tl-row">
        <span class="tl-agent" :style="{ color: row.color }" :title="row.title">{{
          row.owner
        }}</span>
        <span class="tl-track">
          <span
            v-for="dot in row.dots"
            :key="dot.id"
            class="tl-dot"
            :class="{ latest: dot.latest }"
            :style="{ left: `${dot.xPct}%`, background: dot.color }"
            :title="dot.title"
          />
        </span>
      </div>
      <div v-if="hiddenRowCount" class="tl-more dim">+{{ hiddenRowCount }} more agents</div>
    </div>
    <div v-else class="tl tl-empty faint">no agent activity yet</div>

    <div class="card-foot">
      <span class="foot-status">
        <StatusChip :status="session.status ?? 'fail'" />
        <PhaseDots :phases="session.phases ?? []" />
      </span>
      <span class="dim">{{ fmtDate(session.started_at) }}</span>
    </div>
    <div class="card-stats">
      <StatChip kind="cost" :value="session.total_cost" />
      <StatChip kind="runtime" :value="durationMs" />
      <StatChip kind="tokens" :value="session.total_tokens" />
    </div>
  </div>
</template>

<style scoped>
.card {
  /* Uniform size: the grid fixes the width, this fixes the height — content
     clamps and truncates rather than resizing the card. Grew by one 40px row
     slot when the timeline went from three to four, and by a 22px chip row
     plus its gap when provenance (issue, pull request) arrived. Runs with
     neither pay 32px of whitespace for the uniformity; a grid whose cards
     changed height with their metadata would cost more than that to read. */
  height: 452px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 20px 22px;
  position: relative;          /* anchors the archive button */
  border: 1px solid var(--border-soft);
  border-radius: 16px;
  background: var(--surface);
  color: var(--text);
  cursor: pointer;
  overflow: hidden;
  transition:
    border-color 0.18s ease,
    box-shadow 0.18s ease,
    transform 0.18s ease;
}

.card-archive {
  /* Top-right of the card, out of the text flow so nothing reflows around it. */
  position: absolute;
  top: 10px;
  right: 12px;
  width: 26px;
  height: 26px;
  padding: 0;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--dim);
  font-family: inherit;
  font-size: 20px;
  line-height: 1;
  cursor: pointer;
  opacity: 0;
  transition:
    opacity 0.15s ease,
    background 0.15s ease,
    color 0.15s ease;
}

/* Hidden until the card is hovered — 50 cards should read as runs, not as a
   wall of close buttons. Focus reveals it too, so keyboards are not excluded. */
.card:hover .card-archive,
.card-archive:focus-visible {
  opacity: 1;
}

.card-archive:hover {
  background: rgba(255, 111, 103, 0.16);
  color: #ff6f67;
}

.card:hover {
  border-color: rgba(148, 163, 255, 0.45);
  box-shadow: 0 10px 34px rgba(148, 163, 255, 0.12);
  transform: translateY(-2px);
}

.card.running {
  border-color: rgba(108, 182, 255, 0.6);
  box-shadow: 0 0 22px rgba(108, 182, 255, 0.16);
}

.card.fail {
  border-color: rgba(255, 111, 103, 0.6);
}

.card.waiting {
  border-color: rgba(232, 182, 74, 0.6);
  box-shadow: 0 0 22px rgba(232, 182, 74, 0.14);
}

/* Text rows must never absorb flex shrink — the fixed-height card squeezes
   overflow into .tl (which clips), not into the text. */
/* The card's own link, stretched over the whole card by its ::after. It stays
   a real anchor — keyboard focus, middle-click, copy-link-address all work —
   while the chips sit above it and win the click on their own area. */
.card-link {
  position: absolute;
  inset: 0;
  z-index: 0;
  border-radius: inherit;
  text-decoration: none;
}

/* Above the stretched link, so a click on a chip opens the chip's target and
   a click anywhere else opens the run. */
.card-links,
.card-archive {
  position: relative;
  z-index: 1;
}

.card-id {
  flex: none;
  font-family: var(--mono);
  font-size: 18px;
  font-weight: 700;
  color: var(--purple);
}

.card-adw {
  flex: none;
  font-family: var(--mono);
  font-size: 16px;
  color: var(--cyan);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* Where this run came from and where it went, on ONE row. Stacked, the two
   chips cost a line the fixed-height card does not have, and the footer falls
   off the bottom edge. */
.card-links {
  /* flex: none, like every other header row: the card is a column with a fixed
     height, and a row that may shrink gets crushed to nothing by it. */
  flex: none;
  display: flex;
  align-items: center;
  gap: 6px;
}

/* Amber, because it is the one thing on the card that did not come from the
   engineer at the keyboard. Sits beside the ADW name: same question — what
   produced this run — answered from the other side. */
.card-issue {
  flex: none;
  
  font-family: var(--mono);
  font-size: 14px;
  color: var(--amber);
  border: 1px solid rgba(232, 182, 74, 0.35);
  border-radius: 5px;
  padding: 1px 6px;
  text-decoration: none;
  white-space: nowrap;
}

.card-issue:hover {
  border-color: var(--amber);
  background: rgba(232, 182, 74, 0.1);
}

/* Violet, so the two provenance chips read as one row of the same kind while
   staying distinguishable at a glance: amber came in, violet went out. */
.card-pr {
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-family: var(--mono);
  font-size: 14px;
  color: var(--violet);
  border: 1px solid rgba(148, 163, 255, 0.35);
  border-radius: 5px;
  padding: 1px 6px;
  text-decoration: none;
  white-space: nowrap;
}

.card-pr:hover {
  border-color: var(--violet);
  background: rgba(148, 163, 255, 0.1);
}

/* Absent entirely when the forge could not be asked — a pill that said
   "unknown" would take the same space to say nothing. */
.pr-pill {
  font-size: 12px;
  padding: 0 5px;
  border-radius: 4px;
  background: rgba(255, 255, 255, 0.06);
}

.pr-pill.good {
  color: var(--green);
}

.pr-pill.bad {
  color: var(--red);
}

.pr-pill.wait {
  color: var(--amber);
}

.pr-pill.done {
  color: var(--purple);
}

.pr-pill.dim {
  color: var(--faint);
}

.card-req {
  flex: none;
  font-size: 16px;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tl {
  display: flex;
  flex-direction: column;
  margin-top: 4px;
  /* Fixed region: axis (28 + 6) + four 40px row slots, roster size or not.
     Four slots is what lets three agents show alongside a "+N more" line. */
  height: 194px;
  flex: none;
  overflow: hidden;
}

.tl-more {
  display: flex;
  align-items: center;
  height: 40px;
  padding-left: 96px;
  font-size: 16px;
}

.tl-axis {
  display: flex;
  align-items: flex-end;
  height: 28px;
  margin-bottom: 6px;
}

.tl-gutter,
.tl-agent {
  flex: none;
  /* Wide enough for full agent names (planner, builder, documenter). */
  width: 96px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding-right: 8px;
}

.tl-scale {
  position: relative;
  flex: 1;
  height: 100%;
  border-bottom: 1px solid var(--border);
}

.tl-tick {
  position: absolute;
  bottom: 4px;
  transform: translateX(-50%);
  font-family: var(--mono);
  font-size: 16px;
  color: var(--faint);
  white-space: nowrap;
}

.tl-tick.edge {
  transform: none;
}

.tl-row {
  display: flex;
  align-items: center;
  height: 40px;
}

.tl-agent {
  font-size: 16px;
  color: var(--dim);
}

.tl-track {
  position: relative;
  flex: 1;
  height: 100%;
  border-bottom: 1px solid var(--border-soft);
}

.tl-dot {
  position: absolute;
  top: 50%;
  width: 9px;
  height: 9px;
  border-radius: 50%;
  transform: translate(-50%, -50%);
}

.tl-dot.latest {
  width: 13px;
  height: 13px;
  box-shadow: 0 0 10px currentColor;
  animation: pulse 1.4s ease-in-out infinite;
}

.tl-empty {
  align-items: center;
  justify-content: center;
  font-size: 16px;
}

.card-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-top: auto;
  font-size: 16px;
}

.foot-status {
  display: inline-flex;
  align-items: center;
  gap: 14px;
}

.card-stats {
  display: flex;
  align-items: center;
  gap: 12px;
}
</style>
