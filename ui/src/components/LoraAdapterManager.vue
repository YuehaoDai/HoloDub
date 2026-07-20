<template>
  <div class="hd-page space-y-5">
    <div class="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
      <div class="flex items-start gap-3">
        <div
          class="flex h-10 w-10 shrink-0 items-center justify-center rounded-[8px] border border-cyan-300/25 bg-cyan-300/10 text-cyan-200"
        >
          <BrainCircuit :size="21" />
        </div>
        <div>
          <h2 class="text-lg font-semibold text-white">{{ t("LoRA Adapter Registry") }}</h2>
          <p class="mt-1 max-w-3xl text-xs leading-5 text-[color:var(--hd-text-muted)]">
            {{
              t(
                "Manage trained adapters as model strategy assets. Voice identity stays in Voice Profiles; adapters are promoted only after dataset, canary, episode, and human acceptance gates pass.",
              )
            }}
          </p>
        </div>
      </div>
      <div class="flex flex-wrap gap-2">
        <button class="hd-btn hd-btn-secondary shrink-0" @click="router.push('/lora-training')">
          <FlaskConical :size="14" />
          {{ t("Training Workbench") }}
        </button>
        <button class="hd-btn hd-btn-secondary shrink-0" :disabled="loading" @click="load">
          <RefreshCw :size="14" :class="{ 'animate-spin': loading }" />
          {{ t("Refresh") }}
        </button>
      </div>
    </div>

    <div class="grid grid-cols-2 gap-3 xl:grid-cols-4">
      <MetricCard :label="t('Training runs')" :value="metricCardValue(runs.length)" />
      <MetricCard :label="t('Adapter files')" :value="metricCardValue(adapters.length)" />
      <MetricCard :label="t('Evidence linked')" :value="metricCardValue(evidenceCount)" tone="success" />
      <MetricCard :label="t('Blocking gates')" :value="metricCardValue(blockingGateCount)" tone="danger" />
    </div>

    <div
      v-if="error"
      class="rounded-[8px] border border-rose-400/25 bg-rose-500/10 p-3 text-xs text-rose-100"
    >
      {{ error }}
    </div>

    <div class="grid gap-4 xl:grid-cols-[minmax(320px,0.9fr)_minmax(0,1.1fr)_minmax(320px,0.7fr)]">
      <section class="hd-panel p-4">
        <div class="mb-3 flex items-center justify-between gap-3">
          <div>
            <h3 class="text-sm font-semibold text-white">{{ t("Adapter Runs") }}</h3>
            <p class="mt-1 text-xs text-[color:var(--hd-text-dim)]">
              {{ t("Grouped by training run under") }}
              <span class="font-mono">DATA_ROOT/training/lora_runs</span>.
            </p>
          </div>
          <span class="hd-chip hd-chip-info">
            {{ registryLoaded ? t("{count} found", { count: runs.length }) : t("Loading") }}
          </span>
        </div>

        <div v-if="loading" class="space-y-2">
          <div v-for="i in 4" :key="i" class="h-28 animate-pulse rounded-[8px] bg-white/[0.04]"></div>
        </div>
        <div
          v-else-if="!runs.length"
          class="rounded-[8px] border border-[color:var(--hd-border-soft)] bg-black/20 p-8 text-center text-xs text-[color:var(--hd-text-dim)]"
        >
          {{ t("No adapter files were found yet.") }}
        </div>
        <div v-else class="space-y-2">
          <button
            v-for="run in runs"
            :key="run.run_relpath"
            type="button"
            class="w-full rounded-[8px] border p-3 text-left transition"
            :class="
              selectedRun?.run_relpath === run.run_relpath
                ? 'border-cyan-300/40 bg-cyan-300/10'
                : 'border-[color:var(--hd-border-soft)] bg-black/20 hover:border-cyan-300/25'
            "
            @click="selectedRunKey = run.run_relpath"
          >
            <div class="flex items-start justify-between gap-3">
              <div class="min-w-0">
                <div class="flex flex-wrap items-center gap-2">
                  <h4 class="truncate text-sm font-semibold text-white">{{ run.name }}</h4>
                  <span class="hd-chip" :class="statusClass(run.promotion_status)">
                    {{ displayToken(run.promotion_status) }}
                  </span>
                </div>
                <div class="mt-2 truncate font-mono text-[10px] text-[color:var(--hd-text-dim)]">
                  {{ run.run_relpath }}
                </div>
              </div>
              <ChevronRight :size="16" class="mt-1 shrink-0 text-[color:var(--hd-text-dim)]" />
            </div>

            <div class="mt-3 grid grid-cols-3 gap-2 text-[10px]">
              <MiniStat :label="t('checkpoints')" :value="String(run.checkpoint_count)" />
              <MiniStat :label="t('evidence')" :value="String(run.evidence.length)" />
              <MiniStat :label="t('blocked')" :value="String(runBlockingCount(run))" />
            </div>

            <div v-if="run.promotion_reasons.length" class="mt-3 flex flex-wrap gap-1.5">
              <span
                v-for="reason in run.promotion_reasons.slice(0, 3)"
                :key="reason"
                class="hd-chip hd-chip-warning"
              >
                {{ displayToken(reason) }}
              </span>
            </div>
          </button>
        </div>
      </section>

      <section class="hd-panel p-4">
        <div v-if="selectedRun" class="space-y-4">
          <div class="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
            <div class="min-w-0">
              <div class="flex flex-wrap items-center gap-2">
                <h3 class="truncate text-sm font-semibold text-white">{{ selectedRun.name }}</h3>
                <span class="hd-chip" :class="statusClass(selectedRun.promotion_status)">
                  {{ displayToken(selectedRun.promotion_status) }}
                </span>
              </div>
              <div class="mt-2 truncate font-mono text-[11px] text-cyan-100">
                {{ selectedRun.canonical_adapter_relpath || t("No canonical checkpoint") }}
              </div>
            </div>
            <div class="grid grid-cols-3 gap-2 text-[10px] lg:w-[260px]">
              <MiniStat :label="t('rank')" :value="display(selectedRun.training.rank)" />
              <MiniStat :label="t('steps')" :value="display(selectedRun.training.steps)" />
              <MiniStat :label="t('tier')" :value="displayToken(selectedRun.dataset.source_tier)" />
            </div>
          </div>

          <div class="grid gap-3 lg:grid-cols-3">
            <div class="rounded-[8px] bg-white/[0.035] p-3">
              <div class="hd-label mb-2">{{ t("Training") }}</div>
              <dl class="space-y-1 text-[11px]">
                <Row :label="t('train rows')" :value="String(selectedRun.dataset.train_feature_rows)" />
                <Row :label="t('val rows')" :value="String(selectedRun.dataset.val_feature_rows)" />
                <Row :label="t('test rows')" :value="String(selectedRun.dataset.test_feature_rows)" />
                <Row :label="t('duration')" :value="displayToken(String(selectedRun.training.duration_source || 'unknown'))" />
              </dl>
            </div>
            <div class="rounded-[8px] bg-white/[0.035] p-3">
              <div class="hd-label mb-2">{{ t("Quality") }}</div>
              <dl class="space-y-1 text-[11px]">
                <Row :label="t('status')" :value="displayToken(selectedRun.quality.status)" />
                <Row :label="t('mean ASR')" :value="display(selectedRun.quality.mean_asr)" />
                <Row :label="t('mean drift')" :value="metricMs(selectedRun.quality.mean_abs_drift_ms)" />
                <Row :label="t('max drift')" :value="metricMs(selectedRun.quality.max_abs_drift_ms)" />
              </dl>
            </div>
            <div class="rounded-[8px] bg-white/[0.035] p-3">
              <div class="hd-label mb-2">{{ t("Canonical") }}</div>
              <dl class="space-y-1 text-[11px]">
                <Row :label="t('role')" :value="displayToken(canonicalCheckpoint?.checkpoint_role || '-')" />
                <Row :label="t('step')" :value="display(canonicalCheckpoint?.checkpoint_step)" />
                <Row :label="t('size')" :value="formatBytes(canonicalCheckpoint?.size_bytes || 0)" />
                <Row :label="t('sha')" :value="canonicalCheckpoint?.sha256 || t('unknown')" />
              </dl>
            </div>
          </div>

          <div class="grid gap-3 lg:grid-cols-3">
            <div class="rounded-[8px] bg-white/[0.035] p-3">
              <div class="hd-label mb-2">{{ t("Adapter Identity") }}</div>
              <dl class="space-y-1 text-[11px]">
                <Row :label="t('Canonical')" :value="shortPath(selectedRun.canonical_adapter_relpath)" />
                <Row :label="t('Runtime')" :value="shortPath(runtimeCheckpoint?.adapter_relpath || runtimeSelection?.adapter_relpath)" />
                <Row :label="t('Promoted')" :value="promotedCheckpoint ? shortPath(promotedCheckpoint.adapter_relpath) : t('none')" />
              </dl>
            </div>
            <div v-if="latestCompareEvidence" class="rounded-[8px] bg-white/[0.035] p-3 lg:col-span-2">
              <div class="mb-2 flex items-center justify-between gap-2">
                <div class="hd-label">{{ t("Adapter Compare") }}</div>
                <span class="hd-chip" :class="statusClass(latestCompareEvidence.status || '')">
                  {{ displayToken(latestCompareEvidence.status || "unknown") }}
                </span>
              </div>
              <div class="grid gap-2 text-[10px] sm:grid-cols-3">
                <MiniStat :label="t('winner')" :value="metricText(latestCompareEvidence, 'winner')" />
                <MiniStat :label="t('sample count')" :value="metricText(latestCompareEvidence, 'sample_count')" />
                <MiniStat :label="t('selected audio')" :value="metricBoolText(latestCompareEvidence, 'selected_audio_identical')" />
              </div>
              <div class="mt-2 rounded-[8px] bg-black/25 p-2 text-[11px] leading-5 text-[color:var(--hd-text-muted)]">
                <span class="font-mono text-cyan-100">{{ t("decision") }}:</span>
                {{ displayToken(metricText(latestCompareEvidence, "decision")) }}
              </div>
              <div class="mt-1 truncate font-mono text-[10px] text-[color:var(--hd-text-dim)]">
                {{ latestCompareEvidence.relpath }}
              </div>
            </div>
          </div>

          <div>
            <div class="mb-2 flex items-center gap-2 text-sm font-semibold text-white">
              <ShieldCheck :size="16" class="text-cyan-200" />
              {{ t("Gate Status") }}
            </div>
            <div class="space-y-2">
              <div
                v-for="gate in selectedRun.gate_status"
                :key="gate.key"
                class="rounded-[8px] border border-[color:var(--hd-border-soft)] bg-black/20 p-3"
              >
                <div class="flex flex-wrap items-center justify-between gap-2">
                  <div class="font-mono text-[11px] text-white">{{ displayToken(gate.key) }}</div>
                  <span class="hd-chip" :class="gateClass(gate.status)">
                    {{ displayToken(gate.status) }}
                  </span>
                </div>
                <div class="mt-1 text-[11px] leading-5 text-[color:var(--hd-text-muted)]">
                  {{ displayToken(gate.reason || "no_reason") }}
                </div>
                <div v-if="gate.evidence" class="mt-1 truncate font-mono text-[10px] text-cyan-100">
                  {{ gate.evidence }}
                </div>
              </div>
            </div>
          </div>

          <div>
            <div class="mb-2 flex items-center gap-2 text-sm font-semibold text-white">
              <FileCheck2 :size="16" class="text-emerald-200" />
              {{ t("Quality Evidence") }}
            </div>
            <div
              v-if="!selectedEvidence.length"
              class="rounded-[8px] border border-[color:var(--hd-border-soft)] bg-black/20 p-4 text-xs text-[color:var(--hd-text-dim)]"
            >
              {{ t("No evidence linked yet.") }}
            </div>
            <div v-else class="space-y-2">
              <div class="flex flex-wrap items-center gap-1.5">
                <span class="mr-1 text-[10px] uppercase text-[color:var(--hd-text-dim)]">
                  {{ t("Evidence Types") }}
                </span>
                <span
                  v-for="item in evidenceTypeCounts"
                  :key="item.type"
                  class="hd-chip hd-chip-muted"
                >
                  {{ displayToken(item.type) }} {{ item.count }}
                </span>
              </div>
              <div
                v-for="item in visibleEvidence"
                :key="item.id"
                class="rounded-[8px] border border-[color:var(--hd-border-soft)] bg-black/20 p-3"
              >
                <div class="flex flex-wrap items-center gap-2">
                  <span class="hd-chip hd-chip-info">{{ displayToken(item.type) }}</span>
                  <span v-if="item.status" class="hd-chip" :class="statusClass(item.status)">
                    {{ displayToken(item.status) }}
                  </span>
                  <span class="text-[10px] text-[color:var(--hd-text-dim)]">{{ formatDate(item.modified_at) }}</span>
                </div>
                <div class="mt-2 text-[11px] leading-5 text-[color:var(--hd-text-muted)]">
                  {{ item.summary || item.relpath }}
                </div>
                <div class="mt-1 truncate font-mono text-[10px] text-[color:var(--hd-text-dim)]">
                  {{ item.relpath }}
                </div>
                <div v-if="item.review_pack_relpath" class="mt-1 truncate font-mono text-[10px] text-cyan-100">
                  {{ t("review pack") }}: {{ item.review_pack_relpath }}
                </div>
              </div>
              <button
                v-if="selectedEvidence.length > evidenceLimit"
                type="button"
                class="hd-btn hd-btn-secondary w-full justify-center"
                @click="evidenceExpanded = !evidenceExpanded"
              >
                {{ evidenceExpanded ? t("Show less evidence") : t("Show all evidence") }}
                <span class="font-mono text-[10px]">({{ selectedEvidence.length }})</span>
              </button>
            </div>
          </div>

          <div>
            <div class="mb-2 flex items-center gap-2 text-sm font-semibold text-white">
              <GitBranch :size="16" class="text-cyan-200" />
              {{ t("Checkpoints") }}
            </div>
            <div class="space-y-2">
              <div
                v-for="checkpoint in selectedRun.checkpoints"
                :key="checkpoint.id"
                class="rounded-[8px] border border-[color:var(--hd-border-soft)] bg-black/20 p-3"
              >
                <div class="flex flex-wrap items-center justify-between gap-2">
                  <div class="min-w-0 truncate font-mono text-[11px] text-white">
                    {{ checkpoint.adapter_relpath }}
                  </div>
                  <span v-if="checkpoint.is_canonical" class="hd-chip hd-chip-success">
                    {{ t("canonical") }}
                  </span>
                  <span v-if="isRuntimeAdapter(checkpoint)" class="hd-chip hd-chip-info">
                    {{ t("runtime selected") }}
                  </span>
                </div>
                <div class="mt-2 grid grid-cols-3 gap-2 text-[10px]">
                  <MiniStat :label="t('role')" :value="displayToken(checkpoint.checkpoint_role || '-')" />
                  <MiniStat :label="t('step')" :value="display(checkpoint.checkpoint_step)" />
                  <MiniStat :label="t('size')" :value="formatBytes(checkpoint.size_bytes)" />
                </div>
              </div>
            </div>
          </div>

          <div>
            <div class="mb-2 flex items-center gap-2 text-sm font-semibold text-white">
              <Lock :size="16" class="text-amber-200" />
              {{ t("Training Workflow") }}
            </div>
            <div class="grid gap-2 sm:grid-cols-2">
              <button
                v-for="action in workflowActions"
                :key="action"
                class="hd-btn hd-btn-secondary justify-start opacity-60"
                disabled
                type="button"
                :title="t('Write actions are intentionally locked until the registry read model is stable.')"
              >
                <Lock :size="13" />
                {{ t(action) }}
              </button>
            </div>
          </div>
        </div>
        <div
          v-else
          class="rounded-[8px] border border-[color:var(--hd-border-soft)] bg-black/20 p-8 text-center text-xs text-[color:var(--hd-text-dim)]"
        >
          {{ t("Select a run to inspect gates and checkpoints.") }}
        </div>
      </section>

      <aside class="space-y-4">
        <section class="hd-panel p-4">
          <h3 class="text-sm font-semibold text-white">{{ t("Runtime Selection") }}</h3>
          <p class="mt-2 text-xs leading-5 text-[color:var(--hd-text-muted)]">
            {{ t("This is the adapter path currently configured for real LoRA candidate execution.") }}
          </p>
          <div class="mt-3 space-y-2 text-[11px]">
            <Row
              :label="t('candidate execution')"
              :value="runtimeSelection?.candidate_execution_enabled ? t('enabled') : t('disabled')"
            />
            <Row
              :label="t('ML execution')"
              :value="runtimeSelection?.ml_candidate_execution_enabled ? t('enabled') : t('disabled')"
            />
            <Row
              :label="t('split planning')"
              :value="runtimeSelection?.split_planning_enabled ? t('enabled') : t('disabled')"
            />
            <Row
              :label="t('registry match')"
              :value="runtimeSelection?.matched_registry ? t('matched') : t('not matched')"
            />
          </div>
          <div class="mt-3 rounded-[8px] bg-black/25 p-2 font-mono text-[10px] leading-5 text-cyan-100">
            {{ runtimeSelection?.adapter_relpath || t("No runtime adapter configured") }}
          </div>
          <div v-if="runtimeSelection?.notes?.length" class="mt-3 flex flex-wrap gap-1.5">
            <span v-for="note in runtimeSelection.notes" :key="note" class="hd-chip hd-chip-warning">
              {{ displayToken(note) }}
            </span>
          </div>
        </section>

        <section class="hd-panel p-4">
          <h3 class="text-sm font-semibold text-white">{{ t("Adapter vs Voice") }}</h3>
          <p class="mt-2 text-xs leading-5 text-[color:var(--hd-text-muted)]">
            {{
              t(
                "A LoRA adapter is not a voice profile by default. It can still learn voice bias if trained mostly from one speaker, so production provenance must keep both IDs:",
              )
            }}
            <span class="font-mono text-cyan-100">voice_profile_id</span> {{ t("and") }}
            <span class="font-mono text-cyan-100">lora_adapter_id</span>.
          </p>
          <div class="mt-3 space-y-2 text-[11px]">
            <div class="flex items-center gap-2 text-emerald-200">
              <CheckCircle2 :size="14" />
              {{ t("Voice samples decide speaker identity.") }}
            </div>
            <div class="flex items-center gap-2 text-cyan-200">
              <SlidersHorizontal :size="14" />
              {{ t("Adapter controls pacing, duration, and domain behavior.") }}
            </div>
            <div class="flex items-center gap-2 text-amber-200">
              <TriangleAlert :size="14" />
              {{ t("Single-voice training requires compatibility marking.") }}
            </div>
          </div>
        </section>

        <section class="hd-panel p-4">
          <h3 class="text-sm font-semibold text-white">{{ t("Hard Quality Contract") }}</h3>
          <div class="mt-3 grid grid-cols-2 gap-2 text-[11px]">
            <MiniStat :label="t('canary mean drift')" value="< 50ms" />
            <MiniStat :label="t('canary max drift')" value="< 300ms" />
            <MiniStat :label="t('mean ASR')" value=">= 0.99" />
            <MiniStat :label="t('min ASR')" value=">= 0.96" />
            <MiniStat :label="t('join failures')" value="0" />
            <MiniStat :label="t('fit delta')" value="<= 0.05" />
          </div>
          <div class="mt-3 flex flex-wrap gap-1.5">
            <span
              v-for="reason in contract?.hard_rejection_reasons || []"
              :key="reason"
              class="hd-chip hd-chip-danger"
            >
              {{ displayToken(reason) }}
            </span>
          </div>
        </section>

        <section class="hd-panel p-4">
          <h3 class="text-sm font-semibold text-white">{{ t("Promotion Gates") }}</h3>
          <div class="mt-3 space-y-2">
            <div
              v-for="gate in contract?.phase_gates || []"
              :key="gate.phase"
              class="rounded-[8px] border border-[color:var(--hd-border-soft)] bg-black/20 p-3"
            >
              <div class="font-mono text-[11px] text-cyan-100">{{ displayToken(gate.phase) }}</div>
              <div class="mt-1 text-[11px] leading-5 text-[color:var(--hd-text-muted)]">
                {{ displayToken(gate.exit) }}
              </div>
            </div>
          </div>
        </section>

        <section class="hd-panel p-4">
          <h3 class="text-sm font-semibold text-white">{{ t("Naming") }}</h3>
          <p class="mt-2 font-mono text-[11px] leading-5 text-[color:var(--hd-text-muted)]">
            {{ namingPattern }}
          </p>
          <div class="mt-2 rounded-[8px] bg-cyan-300/10 p-2 font-mono text-[11px] text-cyan-100">
            {{ namingExample }}
          </div>
        </section>

        <section v-if="unlinkedEvidence.length" class="hd-panel p-4">
          <h3 class="text-sm font-semibold text-white">{{ t("Unlinked Evidence") }}</h3>
          <div class="mt-3 space-y-2">
            <div
              v-for="item in unlinkedEvidence.slice(0, 5)"
              :key="item.id"
              class="rounded-[8px] border border-[color:var(--hd-border-soft)] bg-black/20 p-3"
            >
              <div class="flex flex-wrap items-center gap-2">
                <span class="hd-chip hd-chip-warning">{{ displayToken(item.type) }}</span>
                <span v-if="item.status" class="text-[10px] text-[color:var(--hd-text-muted)]">
                  {{ displayToken(item.status) }}
                </span>
              </div>
              <div class="mt-1 truncate font-mono text-[10px] text-[color:var(--hd-text-dim)]">
                {{ item.relpath }}
              </div>
            </div>
          </div>
        </section>
      </aside>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, defineComponent, h, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import {
  BrainCircuit,
  CheckCircle2,
  ChevronRight,
  FileCheck2,
  FlaskConical,
  GitBranch,
  Lock,
  RefreshCw,
  ShieldCheck,
  SlidersHorizontal,
  TriangleAlert,
} from "@lucide/vue";
import {
  api,
  type LoraAdapter,
  type LoraAdapterEvidence,
  type LoraAdapterRuntimeSelection,
  type LoraAdapterRun,
  type LoraTrainingContract,
} from "../api";
import { useI18n } from "../i18n";

const { t } = useI18n();
const router = useRouter();
const adapters = ref<LoraAdapter[]>([]);
const runs = ref<LoraAdapterRun[]>([]);
const unlinkedEvidence = ref<LoraAdapterEvidence[]>([]);
const runtimeSelection = ref<LoraAdapterRuntimeSelection | null>(null);
const contract = ref<LoraTrainingContract | null>(null);
const selectedRunKey = ref("");
const loading = ref(false);
const registryLoaded = ref(false);
const error = ref("");
const workflowActions = [
  "Attach gold manifest",
  "Run canary regression",
  "Run episode shadow",
  "Promote opt-in",
];

const selectedRun = computed(() => {
  return runs.value.find((run) => run.run_relpath === selectedRunKey.value) || runs.value[0] || null;
});
const canonicalCheckpoint = computed(() => {
  return selectedRun.value?.checkpoints.find((checkpoint) => checkpoint.is_canonical) || null;
});
const runtimeCheckpoint = computed(() => {
  return selectedRun.value?.checkpoints.find((checkpoint) => isRuntimeAdapter(checkpoint)) || null;
});
const promotedCheckpoint = computed(() => {
  return selectedRun.value?.checkpoints.find((checkpoint) => checkpoint.promotion_status === "candidate_ready") || null;
});
const evidenceExpanded = ref(false);
const evidenceLimit = 10;
const selectedEvidence = computed(() => selectedRun.value?.evidence || []);
const visibleEvidence = computed(() =>
  evidenceExpanded.value ? selectedEvidence.value : selectedEvidence.value.slice(0, evidenceLimit),
);
const evidenceTypeCounts = computed(() => {
  const counts = new Map<string, number>();
  for (const item of selectedEvidence.value) {
    counts.set(item.type, (counts.get(item.type) || 0) + 1);
  }
  return [...counts.entries()]
    .map(([type, count]) => ({ type, count }))
    .sort((left, right) => right.count - left.count || left.type.localeCompare(right.type));
});
const latestCompareEvidence = computed(() => {
  return selectedEvidence.value.find((item) => item.type === "adapter_compare") || null;
});
const evidenceCount = computed(() => runs.value.reduce((sum, run) => sum + run.evidence.length, 0));
const blockingGateCount = computed(() =>
  runs.value.reduce((sum, run) => sum + runBlockingCount(run), 0),
);
const namingPattern = computed(() => stringRecordValue(contract.value?.naming_convention, "pattern"));
const namingExample = computed(() => stringRecordValue(contract.value?.naming_convention, "example"));

async function load() {
  loading.value = true;
  error.value = "";
  try {
    const data = await api.listLoraAdapters();
    adapters.value = data.adapters || [];
    runs.value = data.runs?.length ? data.runs : fallbackRuns(adapters.value);
    unlinkedEvidence.value = data.unlinked_evidence || [];
    runtimeSelection.value = data.runtime_selection || null;
    contract.value = data.training_contract;
    registryLoaded.value = true;
    if (!runs.value.some((run) => run.run_relpath === selectedRunKey.value)) {
      selectedRunKey.value = runs.value[0]?.run_relpath || "";
    }
  } catch (err: unknown) {
    error.value = formatLoadError(err);
  } finally {
    loading.value = false;
  }
}

function fallbackRuns(items: LoraAdapter[]): LoraAdapterRun[] {
  const groups = new Map<string, LoraAdapter[]>();
  for (const item of items) {
    const key = item.run_relpath || item.run_id;
    groups.set(key, [...(groups.get(key) || []), item]);
  }
  return [...groups.entries()].map(([runRelpath, checkpoints]) => {
    const canonical = checkpoints.find((item) => item.is_canonical) || checkpoints[0];
    return {
      id: canonical.run_id,
      name: canonical.run_id,
      run_relpath: runRelpath,
      canonical_adapter_id: canonical.id,
      canonical_adapter_relpath: canonical.adapter_relpath,
      promotion_status: canonical.promotion_status,
      promotion_reasons: canonical.promotion_reasons,
      checkpoint_count: checkpoints.length,
      checkpoints,
      evidence: [],
      gate_status: [],
      training: canonical.training,
      dataset: canonical.dataset,
      quality: canonical.quality,
      voice_compatibility: canonical.voice_compatibility,
      naming: canonical.naming,
      artifacts: canonical.artifacts,
    };
  });
}

function runBlockingCount(run: LoraAdapterRun): number {
  return run.gate_status.filter((gate) => gate.status === "fail" || gate.status === "missing").length;
}

function metricCardValue(value: number): string {
  return loading.value && !registryLoaded.value ? "..." : String(value);
}

function isRuntimeAdapter(adapter: LoraAdapter): boolean {
  return !!runtimeSelection.value?.adapter_relpath && adapter.adapter_relpath === runtimeSelection.value.adapter_relpath;
}

function statusClass(status: string): string {
  switch (status) {
    case "candidate_ready":
    case "human_accepted":
    case "shadow_pass":
      return "hd-chip-success";
    case "needs_review":
    case "warn":
    case "quality_baseline_present":
    case "smoke_complete_needs_full_canary":
      return "hd-chip-warning";
    case "pilot_only":
    case "missing":
      return "hd-chip-muted";
    case "fail":
      return "hd-chip-danger";
    default:
      return "hd-chip-info";
  }
}

function gateClass(status: string): string {
  switch (status) {
    case "pass":
      return "hd-chip-success";
    case "fail":
      return "hd-chip-danger";
    case "warn":
      return "hd-chip-warning";
    case "missing":
      return "hd-chip-muted";
    default:
      return "hd-chip-info";
  }
}

function display(value: unknown): string {
  if (value === undefined || value === null || value === "") return "-";
  if (typeof value === "boolean") return value ? t("yes") : t("no");
  if (typeof value === "number") {
    return Number.isInteger(value) ? String(value) : value.toFixed(4);
  }
  return String(value);
}

function displayToken(value: string): string {
  return t(value);
}

function metricMs(value: unknown): string {
  if (typeof value !== "number") return "-";
  return `${value.toFixed(1)}ms`;
}

function metricText(item: LoraAdapterEvidence, key: string): string {
  return display(item.metrics?.[key]);
}

function metricBoolText(item: LoraAdapterEvidence, key: string): string {
  const value = item.metrics?.[key];
  if (value === true) return t("identical");
  if (value === false) return t("different");
  return display(value);
}

function shortPath(value: string | undefined): string {
  if (!value) return "-";
  const parts = value.split(/[\\/]/).filter(Boolean);
  return parts.slice(-2).join("/");
}

function formatBytes(bytes: number): string {
  if (!bytes) return "-";
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)}MB`;
}

function formatDate(value: string): string {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function formatLoadError(err: unknown): string {
  const message = err instanceof Error ? err.message : String(err);
  if (message.includes("public base URL") || message.includes("/ui/lora/adapters")) {
    return t("Adapter API is not connected in this Vite preview. Start the Go API to load registry data.");
  }
  return message;
}

function stringRecordValue(record: Record<string, unknown> | undefined, key: string): string {
  const value = record?.[key];
  return typeof value === "string" ? value : "";
}

const MetricCard = defineComponent({
  props: {
    label: { type: String, required: true },
    value: { type: String, required: true },
    tone: { type: String, default: "info" },
  },
  setup(props) {
    return () =>
      h("div", { class: "hd-metric" }, [
        h(
          "div",
          {
            class: [
              "text-2xl font-semibold",
              props.tone === "success"
                ? "text-emerald-200"
                : props.tone === "warning"
                  ? "text-amber-200"
                  : props.tone === "danger"
                    ? "text-rose-200"
                    : "text-cyan-200",
            ],
          },
          props.value,
        ),
        h("div", { class: "mt-1 text-[10px] uppercase text-[color:var(--hd-text-dim)]" }, props.label),
      ]);
  },
});

const MiniStat = defineComponent({
  props: {
    label: { type: String, required: true },
    value: { type: String, required: true },
  },
  setup(props) {
    return () =>
      h("div", { class: "rounded-[8px] bg-white/[0.045] px-2 py-2" }, [
        h("div", { class: "text-[color:var(--hd-text-dim)]" }, props.label),
        h("div", { class: "mt-1 truncate font-mono text-white" }, props.value),
      ]);
  },
});

const Row = defineComponent({
  props: {
    label: { type: String, required: true },
    value: { type: String, required: true },
  },
  setup(props) {
    return () =>
      h("div", { class: "flex items-center justify-between gap-2" }, [
        h("dt", { class: "text-[color:var(--hd-text-dim)]" }, props.label),
        h("dd", { class: "min-w-0 truncate font-mono text-[color:var(--hd-text)]" }, props.value),
      ]);
  },
});

onMounted(load);
</script>
