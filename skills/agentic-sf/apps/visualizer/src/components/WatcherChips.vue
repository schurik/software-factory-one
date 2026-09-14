<script setup lang="ts">
/**
 * The two watcher badges in the top bar.
 *
 * They answer a question no session card can: is anything actually going to
 * PICK UP the issue you just labelled. A watcher that is not running and a
 * watcher with nothing to do look identical from a terminal, and the trace UI
 * is the screen already open while you wait — so the answer belongs here.
 *
 * Never hidden when the news is bad. A missing watcher renders as a dashed,
 * muted chip rather than as nothing at all, because "no badge" is exactly the
 * silence this is meant to break.
 */
import { onUnmounted, ref } from 'vue'
import { fetchWatchers } from '../lib/api'
import type { WatcherState } from '../lib/types'

const watchers = ref<WatcherState[]>([])

/** The chip's visual state, which is not the row's status: a dead pid wins. */
function tone(w: WatcherState): 'up' | 'busy' | 'bad' | 'off' {
  if (w.status === 'unknown' || w.status === 'stopped') return 'off'
  if (w.status === 'disabled') return 'off'
  if (!w.alive) return 'bad'
  if (w.status === 'error') return 'bad'
  return w.status === 'working' ? 'busy' : 'up'
}

function label(w: WatcherState): string {
  return w.kind === 'issues' ? 'issues' : 'reviews'
}

function ago(iso: string | null): string {
  if (!iso) return 'never'
  const seconds = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000))
  if (seconds < 60) return `${seconds}s ago`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  return `${Math.floor(seconds / 3600)}h ago`
}

/** The hover text carries the detail, so the chip itself can stay one word. */
function title(w: WatcherState): string {
  const kind = w.kind === 'issues' ? 'issue watcher' : 'review watcher'
  if (w.status === 'unknown') return `${kind}: never started in this repo — run \`just up\``
  if (w.status === 'disabled') return `${kind}: off — enabled: false in the config`
  if (w.status === 'stopped') return `${kind}: stopped ${ago(w.last_poll_at)}`
  if (!w.alive) return `${kind}: not running — last wrote "${w.status}" ${ago(w.last_poll_at)}`
  const parts = [`${kind}: ${w.status}`, `last poll ${ago(w.last_poll_at)}`]
  if (w.project) parts.push(w.project)
  if (w.note) parts.push(w.note)
  return parts.join(' · ')
}

async function load(): Promise<void> {
  try {
    watchers.value = await fetchWatchers()
  } catch {
    // The api being unreachable is the page's problem, not this corner's:
    // keeping the last known chips beats flashing "unknown" on one bad poll.
  }
}

void load()
// Slower than the session poll on purpose. A watcher's own poll is measured in
// minutes, so a 10s badge is already far finer-grained than the thing it shows.
const timer = window.setInterval(load, 10_000)
onUnmounted(() => window.clearInterval(timer))
</script>

<template>
  <span class="watchers">
    <span v-for="w in watchers" :key="w.kind" class="chip" :class="tone(w)" :title="title(w)">
      <span class="dot" />
      {{ label(w) }}
    </span>
  </span>
</template>

<style scoped>
.watchers {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.chip {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 2px 11px;
  border-radius: 999px;
  border: 1px solid var(--border);
  font-size: 15px;
  color: var(--dim);
  white-space: nowrap;
  cursor: default;
}

.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: currentColor;
  flex: none;
}

.chip.up {
  color: var(--green);
  border-color: rgba(74, 222, 128, 0.4);
  background: rgba(74, 222, 128, 0.08);
}

.chip.busy {
  color: var(--blue);
  border-color: rgba(108, 182, 255, 0.4);
  background: rgba(108, 182, 255, 0.08);
}

.chip.busy .dot {
  animation: pulse 1.6s ease-in-out infinite;
}

.chip.bad {
  color: var(--red);
  border-color: rgba(255, 111, 103, 0.45);
  background: rgba(255, 111, 103, 0.08);
}

/* Off is muted and dashed rather than absent — a watcher nobody started is the
   thing worth noticing, so it still occupies its slot. */
.chip.off {
  color: var(--faint);
  border-style: dashed;
}

.chip.off .dot {
  background: transparent;
  box-shadow: inset 0 0 0 1px currentColor;
}
</style>
