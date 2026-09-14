<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'
import type { SessionSummary } from '../lib/types'
import { fetchSessions } from '../lib/api'
import { ts } from '../lib/format'
import SessionCard from './SessionCard.vue'

const sessions = shallowRef<SessionSummary[]>([])
const apiError = ref<string | null>(null)
const loaded = ref(false)
const nowMs = ref(Date.now())

let timer: ReturnType<typeof setInterval> | undefined
let inflight = false

async function tick() {
  if (inflight) return
  inflight = true
  try {
    sessions.value = await fetchSessions()
    nowMs.value = Date.now()
    apiError.value = null
    loaded.value = true
  } catch (err) {
    apiError.value = err instanceof Error ? err.message : String(err)
  } finally {
    inflight = false
  }
}

onMounted(() => {
  void tick()
  timer = setInterval(() => void tick(), 500)
})

onUnmounted(() => clearInterval(timer))

/** Optimistic removal; an empty id means the write failed, so re-sync instead. */
function onArchived(adwId: string) {
  if (!adwId) {
    void tick()
    return
  }
  sessions.value = sessions.value.filter((s) => s.adw_id !== adwId)
}

const sorted = computed(() =>
  sessions.value.toSorted((a, b) => (ts(b.started_at) || 0) - (ts(a.started_at) || 0)),
)

// Two questions the list gets asked, so two filters and no more: everything,
// or only what an issue asked for. `trigger` is null on rows written before the
// column existed, which is why "issue" tests for the value rather than
// "engineer" testing for its absence — an old run is not an issue run.
type Filter = 'all' | 'issue'
const filter = ref<Filter>('all')

const issueCount = computed(() => sessions.value.filter((s) => s.trigger === 'issue').length)

// Archiving the last issue run hides the chips, and a filter left pointing at
// nothing would strand the list empty with no control to click back. The filter
// follows the data rather than the other way round.
watch(issueCount, (count) => {
  if (!count && filter.value === 'issue') filter.value = 'all'
})

const ordered = computed(() =>
  filter.value === 'issue'
    ? sorted.value.filter((s) => s.trigger === 'issue')
    : sorted.value,
)
</script>

<template>
  <div class="sessions">
    <div v-if="apiError" class="error-bar">api unreachable — retrying {{ apiError }}</div>

    <div v-if="sorted.length" class="list-head">
      <span class="dim">{{ ordered.length }} runs</span>
      <!-- Only offered once there is something to filter TO. A toggle that can
           only ever empty the list is a way to make the UI look broken. -->
      <span v-if="issueCount" class="filters">
        <button
          type="button"
          :class="{ on: filter === 'all' }"
          @click="filter = 'all'"
        >
          all
        </button>
        <button
          type="button"
          :class="{ on: filter === 'issue' }"
          title="runs a labelled issue started"
          @click="filter = 'issue'"
        >
          from issues · {{ issueCount }}
        </button>
      </span>
    </div>

    <div v-if="ordered.length" class="cards">
      <SessionCard
        v-for="s in ordered"
        :key="s.adw_id"
        :session="s"
        :now-ms="nowMs"
        @archived="onArchived"
      />
    </div>
    <div v-else-if="filter === 'issue'" class="empty-state">
      no issue-triggered runs yet — see the issues block in factory.yaml
    </div>
    <div v-else-if="loaded" class="empty-state">no sessions yet — run an ADW to see it here</div>
    <div v-else-if="!apiError" class="empty-state">loading sessions…</div>
  </div>
</template>

<style scoped>
.sessions {
  display: flex;
  flex-direction: column;
}

.list-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 16px 24px 0;
  font-size: 16px;
}

.filters {
  display: flex;
  gap: 6px;
}

.filters button {
  font-family: var(--mono);
  font-size: 14px;
  color: var(--faint);
  background: none;
  border: 1px solid var(--border);
  border-radius: 5px;
  padding: 2px 9px;
  cursor: pointer;
}

.filters button:hover {
  color: var(--dim);
  border-color: var(--border-soft);
}

.filters button.on {
  color: var(--amber);
  border-color: rgba(232, 182, 74, 0.45);
}

.cards {
  /* Uniform grid: every card the same width and (fixed in SessionCard) height,
     independent of content. */
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(460px, 1fr));
  gap: 18px;
  padding: 16px 24px 28px;
}





</style>
