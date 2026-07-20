<template>
  <div class="hd-page space-y-5">
    <div class="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
      <div class="flex items-start gap-3">
        <div
          class="flex h-10 w-10 shrink-0 items-center justify-center rounded-[8px] border border-emerald-300/25 bg-emerald-300/10 text-emerald-200"
        >
          <FlaskConical :size="21" />
        </div>
        <div>
          <h2 class="text-lg font-semibold text-white">{{ t("LoRA Training Workbench") }}</h2>
          <p class="mt-1 max-w-3xl text-xs leading-5 text-[color:var(--hd-text-muted)]">
            {{
              t(
                "Plan adapter training, generate reproducible commands, and verify the same canary and episode gates used by the registry before promotion.",
              )
            }}
          </p>
        </div>
      </div>
      <div class="flex flex-wrap gap-2">
        <button class="hd-btn hd-btn-primary" :disabled="saving" @click="createTrainingPlan">
          <Save :size="14" />
          {{ saving ? t("Saving...") : t("Save Training Plan") }}
        </button>
        <button class="hd-btn hd-btn-secondary" :disabled="loading" @click="load">
          <RefreshCw :size="14" :class="{ 'animate-spin': loading }" />
          {{ t("Refresh") }}
        </button>
        <button class="hd-btn hd-btn-secondary" @click="router.push('/lora-adapters')">
          <Archive :size="14" />
          {{ t("Open Adapter Registry") }}
        </button>
      </div>
    </div>

    <div class="grid gap-3 lg:grid-cols-4">
      <MetricCard :label="t('training mode')" :value="selectedTrainingJob ? t('selected plan') : t('plan only')" />
      <MetricCard :label="t('target adapter')" :value="shortPath(displayAdapterPath)" tone="success" />
      <MetricCard :label="t('canary minimum')" :value="String(objectiveGate('canary_min_count') || 84)" />
      <MetricCard :label="t('max fit delta')" :value="String(objectiveGate('max_fit_delta') || 0.05)" tone="warning" />
    </div>

    <div
      class="rounded-[8px] border border-amber-400/20 bg-amber-500/10 p-3 text-xs leading-5 text-amber-100"
    >
      <div class="flex items-start gap-2">
        <Lock :size="15" class="mt-0.5 shrink-0 text-amber-200" />
        <div>
          <div class="font-semibold">{{ t("GPU write actions are still locked.") }}</div>
          <div class="text-amber-100/80">
            {{
              fullRunnerEnabled
                ? t(
                    "Full adapter training is enabled for saved plans. Smoke stays isolated; full training writes the official adapter output directory and remains opt-in.",
                  )
                : t(
                    "Full adapter training is still locked. You can run an isolated one-step smoke on a saved plan to verify the ML runner without overwriting the production adapter path.",
                  )
            }}
          </div>
        </div>
      </div>
    </div>

    <div
      v-if="error"
      class="rounded-[8px] border border-rose-400/25 bg-rose-500/10 p-3 text-xs text-rose-100"
    >
      {{ error }}
    </div>

    <div class="grid gap-4 2xl:grid-cols-[minmax(360px,0.9fr)_minmax(0,1.15fr)_minmax(320px,0.75fr)]">
      <section class="hd-panel p-4">
        <div class="mb-3 flex items-center gap-2 text-sm font-semibold text-white">
          <Settings2 :size="16" class="text-emerald-200" />
          {{ t("Training Setup") }}
        </div>

        <div class="space-y-4">
          <div>
            <label class="mb-1.5 block text-xs text-[color:var(--hd-text-muted)]">{{ t("run id") }}</label>
            <input v-model="form.run_id" class="hd-input font-mono text-xs" />
          </div>
          <div>
            <label class="mb-1.5 block text-xs text-[color:var(--hd-text-muted)]">{{ t("train features") }}</label>
            <input v-model="form.train_features" class="hd-input font-mono text-xs" />
          </div>
          <div>
            <label class="mb-1.5 block text-xs text-[color:var(--hd-text-muted)]">{{ t("val features") }}</label>
            <input v-model="form.val_features" class="hd-input font-mono text-xs" />
          </div>
          <div>
            <label class="mb-1.5 block text-xs text-[color:var(--hd-text-muted)]">{{ t("canary features") }}</label>
            <input v-model="form.canary_features" class="hd-input font-mono text-xs" />
          </div>
          <div class="grid gap-3 sm:grid-cols-2">
            <div>
              <label class="mb-1.5 block text-xs text-[color:var(--hd-text-muted)]">{{ t("output root") }}</label>
              <input v-model="form.output_root" class="hd-input font-mono text-xs" />
            </div>
            <div>
              <label class="mb-1.5 block text-xs text-[color:var(--hd-text-muted)]">{{ t("voice prompt") }}</label>
              <input v-model="form.voice_prompt_relpath" class="hd-input font-mono text-xs" />
            </div>
          </div>

          <div class="grid gap-3 sm:grid-cols-3">
            <NumberField v-model="form.rank" :label="t('rank')" />
            <NumberField v-model="form.alpha" :label="t('alpha')" />
            <NumberField v-model="form.dropout" :label="t('dropout')" step="0.01" />
            <NumberField v-model="form.max_steps" :label="t('max steps')" />
            <NumberField v-model="form.save_every" :label="t('save every')" />
            <NumberField v-model="form.eval_interval" :label="t('eval interval')" />
            <NumberField v-model="form.lr" :label="t('learning rate')" step="0.00001" />
            <NumberField v-model="form.text_loss_weight" :label="t('text loss')" step="0.1" />
            <NumberField v-model="form.mel_loss_weight" :label="t('mel loss')" step="0.1" />
            <NumberField v-model="form.mel_stop_loss_weight" :label="t('stop loss')" step="0.1" />
            <NumberField v-model="form.mel_end_loss_weight" :label="t('tail loss')" step="0.1" />
            <NumberField v-model="form.mel_end_window" :label="t('tail window')" />
            <NumberField v-model="form.duration_dropout" :label="t('duration dropout')" step="0.01" />
            <NumberField v-model="form.duration_position_loss_weight" :label="t('duration position')" step="0.1" />
            <NumberField v-model="form.duration_position_loss_window" :label="t('position window')" />
            <NumberField v-model="form.duration_stop_bce_weight" :label="t('duration stop BCE')" step="0.1" />
          </div>

          <div class="grid gap-3 sm:grid-cols-2">
            <label class="flex items-center gap-2 rounded-[8px] bg-white/[0.035] px-3 py-2 text-xs text-white">
              <input v-model="form.duration_control" type="checkbox" />
              {{ t("duration control") }}
            </label>
            <label class="flex items-center gap-2 rounded-[8px] bg-white/[0.035] px-3 py-2 text-xs text-white">
              <input v-model="form.gradient_checkpointing" type="checkbox" />
              {{ t("gradient checkpointing") }}
            </label>
          </div>

          <div class="grid gap-3 sm:grid-cols-2">
            <div>
              <label class="mb-1.5 block text-xs text-[color:var(--hd-text-muted)]">{{ t("duration source") }}</label>
              <select v-model="form.duration_source" class="hd-select">
                <option value="audio">{{ t("audio") }}</option>
                <option value="target">{{ t("target") }}</option>
              </select>
            </div>
            <div>
              <label class="mb-1.5 block text-xs text-[color:var(--hd-text-muted)]">{{ t("precision") }}</label>
              <select v-model="form.precision" class="hd-select">
                <option value="fp16">fp16</option>
                <option value="fp32">fp32</option>
              </select>
            </div>
          </div>

          <button class="hd-btn hd-btn-primary w-full" :disabled="saving" @click="createTrainingPlan">
            <Save :size="14" />
            {{ saving ? t("Saving...") : t("Save Training Plan") }}
          </button>
        </div>
      </section>

      <section class="hd-panel p-4">
        <div class="mb-3 flex items-center justify-between gap-3">
          <div class="flex items-center gap-2 text-sm font-semibold text-white">
            <TerminalSquare :size="16" class="text-cyan-200" />
            {{ t("Command Plan") }}
            <span class="hd-chip hd-chip-info">
              {{ selectedTrainingJob ? t("persisted plan") : t("current form") }}
            </span>
          </div>
          <button class="hd-btn hd-btn-secondary py-1.5 text-xs" @click="copyAllCommands">
            <Copy :size="13" />
            {{ t("Copy all") }}
          </button>
        </div>

        <div class="space-y-3">
          <CommandBlock
            :title="t('1. Train adapter')"
            :description="t('Produces checkpoints, summary.json, and adapter_latest.pt under the selected run directory.')"
            :command="trainingCommand"
            @copy="copyCommand(trainingCommand)"
          />
          <CommandBlock
            :title="t('2. Run canary loop')"
            :description="t('Generates flat, split_light_vector, and split_mix candidates with the production gates.')"
            :command="canaryCommand"
            @copy="copyCommand(canaryCommand)"
          />
          <CommandBlock
            :title="t('3. Check regression gates')"
            :description="t('Fails if canary count, drift, ASR, join quality, or anchor expectations regress.')"
            :command="regressionCommand"
            @copy="copyCommand(regressionCommand)"
          />
        </div>
      </section>

      <aside class="space-y-4">
        <section class="hd-panel p-4">
          <div class="mb-3 flex items-center justify-between gap-3">
            <h3 class="text-sm font-semibold text-white">{{ t("Training Plans") }}</h3>
            <span class="hd-chip hd-chip-muted">{{ trainingJobs.length }}</span>
          </div>
          <div v-if="trainingJobs.length" class="space-y-2">
            <button
              v-for="job in trainingJobs.slice(0, 8)"
              :key="job.id"
              class="w-full rounded-[8px] border p-3 text-left transition"
              :class="
                selectedTrainingJob?.id === job.id
                  ? 'border-cyan-300/40 bg-cyan-300/10'
                  : 'border-[color:var(--hd-border-soft)] bg-black/20 hover:border-cyan-300/30'
              "
              @click="selectTrainingJob(job)"
            >
              <div class="flex items-center justify-between gap-2">
                <div class="min-w-0 truncate text-xs font-semibold text-white">{{ job.name || job.run_id }}</div>
                <span class="hd-chip shrink-0" :class="statusClass(job.status)">{{ t(job.status) }}</span>
              </div>
              <div class="mt-1 truncate font-mono text-[10px] text-[color:var(--hd-text-dim)]">
                {{ job.run_id }}
              </div>
              <div class="mt-1 text-[10px] text-[color:var(--hd-text-muted)]">
                {{ t("created") }} {{ formatTime(job.created_at) }}
              </div>
            </button>
          </div>
          <div
            v-else
            class="rounded-[8px] border border-[color:var(--hd-border-soft)] bg-black/20 p-4 text-center text-xs text-[color:var(--hd-text-dim)]"
          >
            {{ t("No training plans yet") }}
          </div>

          <div v-if="selectedTrainingJob" class="mt-4 space-y-2 text-[11px]">
            <button
              class="hd-btn hd-btn-primary mb-2 w-full"
              :disabled="runningSmoke"
              @click="runSmoke"
            >
              <RefreshCw v-if="runningSmoke" :size="14" class="animate-spin" />
              <PlayCircle v-else :size="14" />
              {{ runningSmoke ? t("Running smoke...") : t("Run 1-step smoke") }}
            </button>
            <button
              v-if="fullRunnerEnabled"
              class="hd-btn hd-btn-secondary mb-2 w-full"
              :disabled="runningFull || selectedTrainingActive"
              @click="runFull"
            >
              <RefreshCw v-if="runningFull || selectedTrainingActive" :size="14" class="animate-spin" />
              <PlayCircle v-else :size="14" />
              {{ fullRunButtonLabel }}
            </button>
            <button v-else class="hd-btn hd-btn-secondary mb-2 w-full" disabled>
              <Lock :size="14" />
              {{ t("Full training runner locked") }}
            </button>
            <div class="grid gap-2 sm:grid-cols-2">
              <button
                v-if="validationRunnerEnabled"
                class="hd-btn hd-btn-secondary w-full"
                :disabled="runningCanary || selectedTrainingActive"
                @click="runCanary()"
              >
                <RefreshCw v-if="runningCanary" :size="14" class="animate-spin" />
                <PlayCircle v-else :size="14" />
                {{ runningCanary ? t("Queueing canary...") : t("Run canary loop") }}
              </button>
              <button v-else class="hd-btn hd-btn-secondary w-full" disabled>
                <Lock :size="14" />
                {{ t("Canary runner locked") }}
              </button>
              <button
                v-if="validationRunnerEnabled"
                class="hd-btn hd-btn-secondary w-full"
                :disabled="runningCanaryReuse || selectedTrainingActive"
                @click="runCanary({ skipInference: true })"
              >
                <RefreshCw v-if="runningCanaryReuse" :size="14" class="animate-spin" />
                <PlayCircle v-else :size="14" />
                {{ runningCanaryReuse ? t("Queueing reuse...") : t("Verify existing canary") }}
              </button>
              <button v-else class="hd-btn hd-btn-secondary w-full" disabled>
                <Lock :size="14" />
                {{ t("Canary reuse locked") }}
              </button>
              <button
                v-if="validationRunnerEnabled"
                class="hd-btn hd-btn-secondary w-full"
                :disabled="runningRegression || selectedTrainingActive"
                @click="runRegression"
              >
                <RefreshCw v-if="runningRegression" :size="14" class="animate-spin" />
                <PlayCircle v-else :size="14" />
                {{ runningRegression ? t("Queueing regression...") : t("Run regression gates") }}
              </button>
              <button v-else class="hd-btn hd-btn-secondary w-full" disabled>
                <Lock :size="14" />
                {{ t("Regression runner locked") }}
              </button>
            </div>
            <div class="hd-label">{{ t("artifacts") }}</div>
            <Row :label="t('output dir')" :value="selectedTrainingJob.output_dir_relpath" />
            <ArtifactRow
              :label="t('adapter')"
              :value="selectedTrainingJob.adapter_relpath"
              :href="artifactHref(selectedTrainingJob.adapter_relpath)"
            />
            <Row :label="t('canary run')" :value="artifactValue('canary_run_dir')" />
          </div>
          <div v-if="selectedTrainingJob && artifactValue('smoke_status') !== '-'" class="mt-4 space-y-2 text-[11px]">
            <div class="hd-label">{{ t("last smoke") }}</div>
            <Row :label="t('status')" :value="t(artifactValue('smoke_status'))" />
            <Row :label="t('output dir')" :value="artifactValue('smoke_output_dir')" />
            <ArtifactRow :label="t('stdout log')" :value="artifactValue('smoke_stdout_log')" :href="artifactHref(artifactValue('smoke_stdout_log'))" />
            <ArtifactRow :label="t('stderr log')" :value="artifactValue('smoke_stderr_log')" :href="artifactHref(artifactValue('smoke_stderr_log'))" />
            <ArtifactRow :label="t('smoke adapter')" :value="artifactValue('smoke_adapter')" :href="artifactHref(artifactValue('smoke_adapter'))" />
          </div>
          <div v-if="selectedTrainingJob && artifactValue('full_status') !== '-'" class="mt-4 space-y-2 text-[11px]">
            <div class="hd-label">{{ t("full training run") }}</div>
            <Row :label="t('status')" :value="t(artifactValue('full_status'))" />
            <Row :label="t('output dir')" :value="artifactValue('full_output_dir')" />
            <ArtifactRow :label="t('stdout log')" :value="artifactValue('full_stdout_log')" :href="artifactHref(artifactValue('full_stdout_log'))" />
            <ArtifactRow :label="t('stderr log')" :value="artifactValue('full_stderr_log')" :href="artifactHref(artifactValue('full_stderr_log'))" />
            <ArtifactRow :label="t('full adapter')" :value="artifactValue('full_adapter')" :href="artifactHref(artifactValue('full_adapter'))" />
            <ArtifactRow :label="t('summary')" :value="artifactValue('full_summary')" :href="artifactHref(artifactValue('full_summary'))" />
            <ArtifactRow :label="t('metrics')" :value="artifactValue('full_metrics')" :href="artifactHref(artifactValue('full_metrics'))" />
            <Row v-if="artifactValue('full_error') !== '-'" :label="t('error')" :value="artifactValue('full_error')" />
          </div>
          <div v-if="selectedTrainingJob && artifactValue('canary_status') !== '-'" class="mt-4 space-y-2 text-[11px]">
            <div class="hd-label">{{ t("canary validation") }}</div>
            <Row :label="t('status')" :value="t(artifactValue('canary_status'))" />
            <Row :label="t('mode')" :value="artifactValue('canary_skip_inference') === 'true' ? t('reuse existing candidates') : t('full candidate generation')" />
            <div v-if="hasArtifact('canary_metric_count')" class="grid grid-cols-2 gap-2 py-1">
              <MiniStat :label="t('selected count')" :value="metricValue('canary_metric_count', 0)" />
              <MiniStat :label="t('mean drift')" :value="metricValue('canary_metric_mean_abs_drift_ms', 1, 'ms')" />
              <MiniStat :label="t('max drift')" :value="metricValue('canary_metric_max_abs_drift_ms', 1, 'ms')" />
              <MiniStat :label="t('mean ASR')" :value="metricValue('canary_metric_mean_similarity', 4)" />
              <MiniStat :label="t('min ASR')" :value="metricValue('canary_metric_min_similarity', 4)" />
              <MiniStat :label="t('join fails')" :value="metricValue('canary_metric_join_quality_fail_count', 0)" />
              <MiniStat :label="t('overrides')" :value="metricValue('canary_metric_listening_override_count', 0)" />
              <MiniStat :label="t('low ASR')" :value="metricValue('canary_metric_low_asr_samples_count', 0)" />
            </div>
            <ArtifactRow :label="t('loop summary')" :value="artifactValue('canary_loop_summary')" :href="artifactHref(artifactValue('canary_loop_summary'))" />
            <ArtifactRow :label="t('gate report')" :value="artifactValue('candidate_gate_report')" :href="artifactHref(artifactValue('candidate_gate_report'))" />
            <ArtifactRow :label="t('listening pack')" :value="artifactValue('listening_pack')" :href="artifactHref(artifactValue('listening_pack'))" />
            <ArtifactRow :label="t('human review')" :value="artifactValue('human_review_report')" :href="artifactHref(artifactValue('human_review_report'))" />
            <ArtifactRow :label="t('risk queue')" :value="artifactValue('risk_queue')" :href="artifactHref(artifactValue('risk_queue'))" />
            <ArtifactRow :label="t('stdout log')" :value="artifactValue('canary_stdout_log')" :href="artifactHref(artifactValue('canary_stdout_log'))" />
            <ArtifactRow :label="t('stderr log')" :value="artifactValue('canary_stderr_log')" :href="artifactHref(artifactValue('canary_stderr_log'))" />
            <Row v-if="artifactValue('canary_error') !== '-'" :label="t('error')" :value="artifactValue('canary_error')" />
          </div>
          <div v-if="selectedTrainingJob && artifactValue('regression_status') !== '-'" class="mt-4 space-y-2 text-[11px]">
            <div class="hd-label">{{ t("regression gates") }}</div>
            <Row :label="t('status')" :value="t(artifactValue('regression_status'))" />
            <div v-if="hasArtifact('regression_metric_count')" class="grid grid-cols-2 gap-2 py-1">
              <MiniStat :label="t('selected count')" :value="metricValue('regression_metric_count', 0)" />
              <MiniStat :label="t('mean drift')" :value="metricValue('regression_metric_mean_abs_drift_ms', 1, 'ms')" />
              <MiniStat :label="t('max drift')" :value="metricValue('regression_metric_max_abs_drift_ms', 1, 'ms')" />
              <MiniStat :label="t('mean ASR')" :value="metricValue('regression_metric_mean_similarity', 4)" />
              <MiniStat :label="t('min ASR')" :value="metricValue('regression_metric_min_similarity', 4)" />
              <MiniStat :label="t('join fails')" :value="metricValue('regression_metric_join_quality_fail_count', 0)" />
            </div>
            <ArtifactRow :label="t('report')" :value="artifactValue('regression_report')" :href="artifactHref(artifactValue('regression_report'))" />
            <ArtifactRow :label="t('stdout log')" :value="artifactValue('regression_stdout_log')" :href="artifactHref(artifactValue('regression_stdout_log'))" />
            <ArtifactRow :label="t('stderr log')" :value="artifactValue('regression_stderr_log')" :href="artifactHref(artifactValue('regression_stderr_log'))" />
            <Row v-if="artifactValue('regression_error') !== '-'" :label="t('error')" :value="artifactValue('regression_error')" />
          </div>
        </section>

        <section class="hd-panel p-4">
          <h3 class="text-sm font-semibold text-white">{{ t("Runner Readiness") }}</h3>
          <div class="mt-3 space-y-2 text-[11px]">
            <Row :label="t('smoke runner')" :value="smokeVerified ? t('verified') : t('not verified')" />
            <Row :label="t('full runner')" :value="displayToken(fullRunnerState)" />
            <Row :label="t('validation runner')" :value="displayToken(validationRunnerState)" />
            <Row :label="t('smoke isolation')" :value="display(runnerCapabilities.smoke_writes_isolated_output)" />
            <Row :label="t('adapter overwrite')" :value="productionOverwriteLocked ? t('locked') : t('allowed')" />
          </div>
          <div class="mt-3 rounded-[8px] border border-[color:var(--hd-border-soft)] bg-black/20 p-3 text-[11px] leading-5 text-[color:var(--hd-text-muted)]">
            {{
              fullRunnerEnabled
                ? t(
                    "Full training is now handled by a background runner with persisted status. Canary and episode gates still remain separate promotion steps.",
                  )
                : t(
                    "Smoke verifies the ML runner in an isolated output directory. Full training stays locked until a background job runner can persist progress and recovery state.",
                  )
            }}
          </div>
        </section>

        <section class="hd-panel p-4">
          <h3 class="text-sm font-semibold text-white">{{ t("Acceptance Playbook") }}</h3>
          <div class="mt-3 space-y-2">
            <div
              v-for="item in acceptancePlaybook"
              :key="item.title"
              class="rounded-[8px] border border-[color:var(--hd-border-soft)] bg-black/20 p-3"
            >
              <div class="text-[11px] font-semibold text-white">{{ t(item.title) }}</div>
              <div class="mt-1 text-[10px] leading-4 text-[color:var(--hd-text-muted)]">
                {{ t(item.detail) }}
              </div>
            </div>
          </div>
        </section>

        <section class="hd-panel p-4">
          <h3 class="text-sm font-semibold text-white">{{ t("Validation Timeline") }}</h3>
          <div class="mt-3 space-y-2">
            <div
              v-for="phase in validationTimeline"
              :key="phase.key"
              class="flex items-start justify-between gap-3 rounded-[8px] border border-[color:var(--hd-border-soft)] bg-black/20 p-3"
            >
              <div class="min-w-0">
                <div class="text-[11px] font-semibold text-white">{{ t(phase.title) }}</div>
                <div class="mt-1 text-[10px] leading-4 text-[color:var(--hd-text-muted)]">
                  {{ t(phase.detail) }}
                </div>
              </div>
              <span class="hd-chip shrink-0" :class="phaseStatusClass(phase.status)">
                {{ t(phase.status) }}
              </span>
            </div>
          </div>
        </section>

        <section class="hd-panel p-4">
          <h3 class="text-sm font-semibold text-white">{{ t("Current Runtime Baseline") }}</h3>
          <div class="mt-3 space-y-2 text-[11px]">
            <Row :label="t('runtime')" :value="shortPath(runtimeAdapter)" />
            <Row :label="t('canonical')" :value="shortPath(canonicalAdapter)" />
            <Row :label="t('candidate execution')" :value="runtimeSelection?.candidate_execution_enabled ? t('enabled') : t('disabled')" />
            <Row :label="t('ML execution')" :value="runtimeSelection?.ml_candidate_execution_enabled ? t('enabled') : t('disabled')" />
          </div>
          <div v-if="runtimeSelection?.notes?.length" class="mt-3 flex flex-wrap gap-1.5">
            <span v-for="note in runtimeSelection.notes" :key="note" class="hd-chip hd-chip-warning">
              {{ t(note) }}
            </span>
          </div>
        </section>

        <section class="hd-panel p-4">
          <h3 class="text-sm font-semibold text-white">{{ t("Objective Gates") }}</h3>
          <div class="mt-3 grid grid-cols-2 gap-2 text-[11px]">
            <MiniStat :label="t('canary count')" :value="display(objectiveGate('canary_min_count'))" />
            <MiniStat :label="t('mean drift')" :value="`< ${display(objectiveGate('canary_max_mean_abs_drift_ms'))}ms`" />
            <MiniStat :label="t('max drift')" :value="`< ${display(objectiveGate('canary_max_max_abs_drift_ms'))}ms`" />
            <MiniStat :label="t('mean ASR')" :value="`>= ${display(objectiveGate('canary_min_mean_asr'))}`" />
            <MiniStat :label="t('min ASR')" :value="`>= ${display(objectiveGate('canary_min_min_asr'))}`" />
            <MiniStat :label="t('fit delta')" :value="`<= ${display(objectiveGate('max_fit_delta'))}`" />
          </div>
        </section>

        <section class="hd-panel p-4">
          <h3 class="text-sm font-semibold text-white">{{ t("Training Decision") }}</h3>
          <div class="mt-3 space-y-2 text-[11px]">
            <Row :label="t('fit audit first')" :value="display(trainingPolicy('fit_audit_first'))" />
            <Row :label="t('training pack audit')" :value="display(trainingPolicy('training_pack_audit_required'))" />
            <Row :label="t('readiness required')" :value="display(trainingPolicy('training_readiness_required'))" />
            <Row :label="t('readiness tool')" :value="shortPath(String(trainingPolicy('training_readiness_tool') || ''))" />
            <Row :label="t('new source floor')" :value="display(trainingPolicy('min_new_source_ids_before_retrain'))" />
            <Row :label="t('production fit')" :value="`<= ${display(trainingPolicy('production_selected_max_fit_delta'))}`" />
            <Row :label="t('per-lecture default')" :value="display(trainingPolicy('per_lecture_training_default'))" />
            <Row :label="t('low drift only')" :value="display(trainingPolicy('low_final_drift_not_sufficient'))" />
          </div>
          <div class="mt-3 flex flex-wrap gap-1.5">
            <span v-for="item in repairOrder" :key="item" class="hd-chip hd-chip-muted">
              {{ t(item) }}
            </span>
          </div>
        </section>

        <section class="hd-panel p-4">
          <h3 class="text-sm font-semibold text-white">{{ t("Lecture 8 Reference") }}</h3>
          <div class="mt-3 grid grid-cols-2 gap-2 text-[11px]">
            <MiniStat :label="t('selected count')" :value="display(lecture08Baseline('selected_count'))" />
            <MiniStat :label="t('max fit delta')" :value="display(lecture08Baseline('max_fit_delta'))" />
            <MiniStat :label="t('mean ASR')" :value="display(lecture08Baseline('mean_asr'))" />
            <MiniStat :label="t('join fails')" :value="display(lecture08Baseline('join_quality_failures'))" />
          </div>
          <div class="mt-3 space-y-2 text-[11px]">
            <Row :label="t('status')" :value="t(String(lecture08Baseline('status') || '-'))" />
            <Row :label="t('next step')" :value="t(String(lecture08Baseline('required_next_step') || '-'))" />
            <Row :label="t('v9 status')" :value="t(String(lecture08Baseline('v9_training_status') || '-'))" />
            <Row :label="t('v10 status')" :value="t(String(lecture08Baseline('v10_training_status') || '-'))" />
            <Row :label="t('training readiness')" :value="t(String(lecture08Baseline('training_readiness_status') || '-'))" />
            <Row :label="t('strict rows')" :value="display(lecture08Baseline('strict_nonholdout_rows'))" />
            <Row :label="t('new sources')" :value="display(lecture08Baseline('new_source_ids_vs_v7'))" />
            <ArtifactRow
              :label="t('listening pack')"
              :value="String(lecture08Baseline('listening_pack_relpath') || '')"
              :href="artifactHref(String(lecture08Baseline('listening_pack_relpath') || ''))"
            />
            <ArtifactRow
              :label="t('readiness report')"
              :value="String(lecture08Baseline('training_readiness_relpath') || '')"
              :href="artifactHref(String(lecture08Baseline('training_readiness_relpath') || ''))"
            />
            <ArtifactRow
              :label="t('v9 training plan')"
              :value="String(lecture08Baseline('v9_training_plan_relpath') || '')"
              :href="artifactHref(String(lecture08Baseline('v9_training_plan_relpath') || ''))"
            />
            <ArtifactRow
              :label="t('v9 repair manifest')"
              :value="String(lecture08Baseline('v9_repair_manifest_relpath') || '')"
              :href="artifactHref(String(lecture08Baseline('v9_repair_manifest_relpath') || ''))"
            />
            <Row :label="t('v9 base features')" :value="String(lecture08Baseline('v9_base_features_relpath') || '')" />
            <Row :label="t('v9 adapter run')" :value="String(lecture08Baseline('v9_adapter_run_relpath') || '')" />
          </div>
        </section>

        <section class="hd-panel p-4">
          <h3 class="text-sm font-semibold text-white">{{ t("Dataset Contract") }}</h3>
          <div class="mt-3 space-y-2 text-[11px]">
            <Row :label="t('gold min')" :value="`${display(dataRequirement('gold_min_hours'))}h`" />
            <Row :label="t('gold target')" :value="`${display(dataRequirement('gold_target_hours'))}h`" />
            <Row :label="t('fixed holdout')" :value="display(dataRequirement('fixed_holdout_required'))" />
            <Row :label="t('human approval')" :value="display(dataRequirement('human_approval_required'))" />
          </div>
          <div class="mt-3 flex flex-wrap gap-1.5">
            <span v-for="item in requiredCoverage" :key="item" class="hd-chip hd-chip-muted">
              {{ t(item) }}
            </span>
          </div>
        </section>

        <section class="hd-panel p-4">
          <h3 class="text-sm font-semibold text-white">{{ t("Promotion Workflow") }}</h3>
          <div class="mt-3 space-y-2">
            <div
              v-for="gate in contract?.phase_gates || []"
              :key="gate.phase"
              class="rounded-[8px] border border-[color:var(--hd-border-soft)] bg-black/20 p-3"
            >
              <div class="font-mono text-[11px] text-cyan-100">{{ t(gate.phase) }}</div>
              <div class="mt-1 text-[11px] leading-5 text-[color:var(--hd-text-muted)]">
                {{ t(gate.exit) }}
              </div>
            </div>
          </div>
        </section>
      </aside>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, defineComponent, h, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import {
  Archive,
  Copy,
  ExternalLink,
  FlaskConical,
  Lock,
  PlayCircle,
  RefreshCw,
  Save,
  Settings2,
  TerminalSquare,
} from "@lucide/vue";
import {
  api,
  getApiKey,
  type LoraAdapterRuntimeSelection,
  type LoraAdapterRun,
  type LoraTrainingContract,
  type LoraTrainingJob,
  type LoraTrainingJobRequest,
} from "../api";
import { useI18n } from "../i18n";
import { toast } from "../lib/toast";

const router = useRouter();
const { t } = useI18n();
const loading = ref(false);
const saving = ref(false);
const runningSmoke = ref(false);
const runningFull = ref(false);
const runningCanary = ref(false);
const runningCanaryReuse = ref(false);
const runningRegression = ref(false);
const error = ref("");
const contract = ref<LoraTrainingContract | null>(null);
const runs = ref<LoraAdapterRun[]>([]);
const runtimeSelection = ref<LoraAdapterRuntimeSelection | null>(null);
const trainingJobs = ref<LoraTrainingJob[]>([]);
const selectedTrainingJob = ref<LoraTrainingJob | null>(null);
let hydratingFromJob = false;
let autoSelectedInitialJob = false;
let trainingPollTimer: ReturnType<typeof window.setInterval> | undefined;

const form = ref({
  run_id: "pilot-episode146-code1500-r4-step120",
  train_features: "data/training/lora_features/pilot-episode146-code1500/train.features.jsonl",
  val_features: "data/training/lora_features/pilot-episode146-code1500/val.features.jsonl",
  canary_features:
    "data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary84_user_issues_technical_v2.features.jsonl",
  output_root: "data/training/lora_runs",
  voice_prompt_relpath: "voices/test_ref.wav",
  rank: 4,
  alpha: 16,
  dropout: 0.05,
  lr: 0.0002,
  max_steps: 120,
  save_every: 20,
  eval_interval: 10,
  text_loss_weight: 0.2,
  mel_loss_weight: 0.8,
  duration_dropout: 0.1,
  mel_stop_loss_weight: 2.0,
  mel_end_loss_weight: 1.5,
  mel_end_window: 64,
  duration_stop_bce_weight: 0.0,
  duration_position_loss_weight: 0.2,
  duration_position_loss_window: 32,
  duration_control: true,
  duration_source: "target",
  gradient_checkpointing: false,
  precision: "fp16",
});

const outputDir = computed(() => joinPath(form.value.output_root, form.value.run_id));
const adapterPath = computed(() => joinPath(outputDir.value, "adapter_latest.pt"));
const canaryRunDir = computed(() => joinPath(outputDir.value, "canary84_full84_regen_v1"));
const displayAdapterPath = computed(() => selectedTrainingJob.value?.adapter_relpath || adapterPath.value);
const runtimeAdapter = computed(() => runtimeSelection.value?.adapter_relpath || "");
const canonicalAdapter = computed(() => runs.value[0]?.canonical_adapter_relpath || "");
const runnerCapabilities = computed(() => contract.value?.runner_capabilities || {});
const smokeVerified = computed(() => artifactValue("smoke_status") === "completed");
const fullRunnerState = computed(() =>
  String(runnerCapabilities.value.full_training || "locked_until_background_runner"),
);
const fullRunnerEnabled = computed(() => fullRunnerState.value === "ml_service_background_runner");
const validationRunnerState = computed(() =>
  String(runnerCapabilities.value.canary_loop || "locked_until_validation_runner"),
);
const validationRunnerEnabled = computed(() => validationRunnerState.value === "ml_service_validation_runner");
const selectedTrainingActive = computed(() =>
  selectedTrainingJob.value?.status === "queued" || selectedTrainingJob.value?.status === "running",
);
const fullRunButtonLabel = computed(() => {
  if (runningFull.value) return t("Queueing full training...");
  if (selectedTrainingJob.value?.status === "queued") return t("Full training queued");
  if (selectedTrainingJob.value?.status === "running") return t("Full training running");
  return t("Start full training");
});
const validationTimeline = computed(() => [
  {
    key: "plan",
    title: "training plan",
    detail: "Persist a reproducible adapter command set.",
    status: selectedTrainingJob.value ? "ready" : "pending",
  },
  {
    key: "smoke",
    title: "smoke check",
    detail: "One-step isolated ML runner check.",
    status: artifactValue("smoke_status") === "completed" ? "completed" : displayPhaseStatus("smoke_status"),
  },
  {
    key: "full",
    title: "full adapter training",
    detail: "Writes the official adapter output directory when explicitly unlocked.",
    status: displayPhaseStatus("full_status"),
  },
  {
    key: "canary",
    title: "canary loop",
    detail: "Candidate generation or reuse plus gate/listening pack rebuild.",
    status: displayPhaseStatus("canary_status"),
  },
  {
    key: "regression",
    title: "regression gates",
    detail: "Golden baseline, seg10569 anchor, drift, ASR, term and join gates.",
    status: displayPhaseStatus("regression_status"),
  },
  {
    key: "promotion",
    title: "promotion decision",
    detail: "Requires human review and a clean episode shadow before default use.",
    status: artifactValue("promotion_status") !== "-" ? artifactValue("promotion_status") : "pending",
  },
]);
const productionOverwriteLocked = computed(
  () => runnerCapabilities.value.production_adapter_overwrite_allowed !== true,
);
const requiredCoverage = computed(() => {
  const value = contract.value?.data_requirements?.required_coverage;
  return Array.isArray(value) ? value.map(String) : [];
});
const trainingDecisionPolicy = computed(() => contract.value?.training_decision_policy || {});
const lecture08Reference = computed(
  () => contract.value?.reference_baselines?.lecture08_highfit_repair || {},
);
const repairOrder = computed(() => {
  const value = trainingDecisionPolicy.value.repair_order;
  return Array.isArray(value) ? value.map(String) : [];
});
const acceptancePlaybook = computed(() => [
  {
    title: "When to train",
    detail: "Train only after new human-approved aligned audio changes the dataset or the current adapter fails objective/human gates.",
  },
  {
    title: "When to reuse canary",
    detail: "Use Verify existing canary to rebuild summaries, gates, and listening packs from existing candidate reports without GPU inference.",
  },
  {
    title: "When to regenerate candidates",
    detail: "Use Run canary loop when the adapter, candidate policy, split planner, or spoken-text policy changed and audio must be regenerated.",
  },
  {
    title: "Adapter and voice compatibility",
    detail: "Voice Profiles own speaker identity; LoRA adapters own model behavior. Keep both IDs in provenance and mark voice-scoped adapters explicitly.",
  },
]);

const selectedCommands = computed(() => selectedTrainingJob.value?.commands || {});

const liveTrainingCommand = computed(() => {
  const args = [
    "python",
    "tools/lora_train_indextts2_smoke.py",
    "--train-features",
    form.value.train_features,
    "--val-features",
    form.value.val_features,
    "--output-dir",
    outputDir.value,
    "--rank",
    form.value.rank,
    "--alpha",
    form.value.alpha,
    "--dropout",
    form.value.dropout,
    "--lr",
    form.value.lr,
    "--max-steps",
    form.value.max_steps,
    "--save-every",
    form.value.save_every,
    "--eval-interval",
    form.value.eval_interval,
    "--text-loss-weight",
    form.value.text_loss_weight,
    "--mel-loss-weight",
    form.value.mel_loss_weight,
    "--duration-source",
    form.value.duration_source,
    "--duration-dropout",
    form.value.duration_dropout,
    "--mel-stop-loss-weight",
    form.value.mel_stop_loss_weight,
    "--mel-end-loss-weight",
    form.value.mel_end_loss_weight,
    "--mel-end-window",
    form.value.mel_end_window,
    "--duration-stop-bce-weight",
    form.value.duration_stop_bce_weight,
    "--duration-position-loss-weight",
    form.value.duration_position_loss_weight,
    "--duration-position-loss-window",
    form.value.duration_position_loss_window,
  ];
  if (form.value.duration_control) args.push("--duration-control");
  if (form.value.gradient_checkpointing) args.push("--gradient-checkpointing");
  if (form.value.precision === "fp32") args.push("--no-fp16");
  return shellCommand(args);
});

const liveCanaryCommand = computed(() =>
  shellCommand([
    "python",
    "tools/lora_canary_candidate_loop.py",
    "--features",
    form.value.canary_features,
    "--run-dir",
    canaryRunDir.value,
    "--adapter",
    adapterPath.value,
    "--data-root",
    "data",
    "--voice-prompt-relpath",
    form.value.voice_prompt_relpath,
    "--candidate-families",
    "flat,split_light_vector,split_mix",
    "--spoken-text-policy",
    "technical-v3",
    "--duration-token-rate-policy",
    "piecewise",
    "--max-fit-delta",
    "0.05",
    "--max-drift-ms",
    "500",
    "--min-similarity",
    "0.97",
    "--target-similarity",
    "0.985",
    "--min-f0-range",
    "4.5",
    "--target-f0-range",
    "5.4",
  ]),
);

const liveRegressionCommand = computed(() =>
  shellCommand([
    "python",
    "tools/lora_canary_regression_check.py",
    "--summary",
    joinPath(canaryRunDir.value, "loop_summary.json"),
    "--gate-report",
    joinPath(canaryRunDir.value, "candidate_gate_report.json"),
    "--min-count",
    objectiveGate("canary_min_count") || 84,
    "--max-mean-drift-ms",
    objectiveGate("canary_max_mean_abs_drift_ms") || 50,
    "--max-max-drift-ms",
    objectiveGate("canary_max_max_abs_drift_ms") || 300,
    "--min-mean-similarity",
    objectiveGate("canary_min_mean_asr") || 0.99,
    "--min-min-similarity",
    objectiveGate("canary_min_min_asr") || 0.96,
    "--max-join-quality-failures",
    objectiveGate("join_quality_failures") || 0,
  ]),
);

const trainingCommand = computed(() => selectedCommands.value.train_adapter || liveTrainingCommand.value);
const canaryCommand = computed(() => selectedCommands.value.canary_loop || liveCanaryCommand.value);
const regressionCommand = computed(
  () => selectedCommands.value.regression_check || liveRegressionCommand.value,
);

async function load(showSpinner = true) {
  if (showSpinner) loading.value = true;
  error.value = "";
  try {
    const contractRequest = api.getLoraTrainingContract();
    const registryRequest = api.listLoraAdapters();
    const jobRequest = api.listLoraTrainingJobs();

    const [contractPayload, jobPayload] = await Promise.all([contractRequest, jobRequest]);
    contract.value = contractPayload;
    trainingJobs.value = jobPayload.training_jobs || [];
    if (selectedTrainingJob.value) {
      selectedTrainingJob.value =
        trainingJobs.value.find((job) => job.id === selectedTrainingJob.value?.id) || null;
    } else if (!autoSelectedInitialJob && trainingJobs.value.length > 0) {
      selectTrainingJob(trainingJobs.value[0]);
      autoSelectedInitialJob = true;
    }

    const registry = await registryRequest;
    runs.value = registry.runs || [];
    runtimeSelection.value = registry.runtime_selection || null;
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    if (showSpinner) loading.value = false;
  }
}

async function createTrainingPlan() {
  saving.value = true;
  error.value = "";
  try {
    const created = await api.createLoraTrainingJob(buildTrainingJobRequest());
    trainingJobs.value = [created, ...trainingJobs.value.filter((job) => job.id !== created.id)];
    selectedTrainingJob.value = created;
    toast.success(t("Training plan saved"));
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : String(err);
    toast.fromError(err, t("Failed to save training plan"));
  } finally {
    saving.value = false;
  }
}

async function runSmoke() {
  if (!selectedTrainingJob.value) return;
  runningSmoke.value = true;
  error.value = "";
  try {
    const payload = await api.runLoraTrainingJobSmoke(selectedTrainingJob.value.id);
    selectedTrainingJob.value = payload.training_job;
    trainingJobs.value = [
      payload.training_job,
      ...trainingJobs.value.filter((job) => job.id !== payload.training_job.id),
    ];
    toast.success(t(payload.smoke_result.status === "completed" ? "Smoke completed" : "Smoke finished with issues"));
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : String(err);
    toast.fromError(err, t("Smoke run failed"));
    await load();
  } finally {
    runningSmoke.value = false;
  }
}

async function runFull() {
  if (!selectedTrainingJob.value || !fullRunnerEnabled.value) return;
  runningFull.value = true;
  error.value = "";
  try {
    const payload = await api.startLoraTrainingJobFull(selectedTrainingJob.value.id);
    selectedTrainingJob.value = payload.training_job;
    trainingJobs.value = [
      payload.training_job,
      ...trainingJobs.value.filter((job) => job.id !== payload.training_job.id),
    ];
    toast.success(t("Full training queued"));
    updateTrainingPoll();
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : String(err);
    toast.fromError(err, t("Full training start failed"));
    await load();
  } finally {
    runningFull.value = false;
  }
}

async function runCanary(options: { skipInference?: boolean } = {}) {
  if (!selectedTrainingJob.value || !validationRunnerEnabled.value) return;
  const reuseExisting = options.skipInference === true;
  if (reuseExisting) {
    runningCanaryReuse.value = true;
  } else {
    runningCanary.value = true;
  }
  error.value = "";
  try {
    const payload = await api.startLoraTrainingJobCanary(selectedTrainingJob.value.id, {
      skip_inference: reuseExisting,
    });
    selectedTrainingJob.value = payload.training_job;
    trainingJobs.value = [
      payload.training_job,
      ...trainingJobs.value.filter((job) => job.id !== payload.training_job.id),
    ];
    toast.success(t(reuseExisting ? "Canary reuse queued" : "Canary queued"));
    updateTrainingPoll();
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : String(err);
    toast.fromError(err, t("Canary start failed"));
    await load();
  } finally {
    runningCanary.value = false;
    runningCanaryReuse.value = false;
  }
}

async function runRegression() {
  if (!selectedTrainingJob.value || !validationRunnerEnabled.value) return;
  runningRegression.value = true;
  error.value = "";
  try {
    const payload = await api.startLoraTrainingJobRegression(selectedTrainingJob.value.id);
    selectedTrainingJob.value = payload.training_job;
    trainingJobs.value = [
      payload.training_job,
      ...trainingJobs.value.filter((job) => job.id !== payload.training_job.id),
    ];
    toast.success(t("Regression queued"));
    updateTrainingPoll();
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : String(err);
    toast.fromError(err, t("Regression start failed"));
    await load();
  } finally {
    runningRegression.value = false;
  }
}

function buildTrainingJobRequest(): LoraTrainingJobRequest {
  return {
    name: form.value.run_id,
    run_id: form.value.run_id,
    source_tier: "silver",
    train_features: form.value.train_features,
    val_features: form.value.val_features,
    canary_features: form.value.canary_features,
    output_root: form.value.output_root,
    voice_prompt_relpath: form.value.voice_prompt_relpath,
    rank: form.value.rank,
    alpha: form.value.alpha,
    dropout: form.value.dropout,
    lr: form.value.lr,
    max_steps: form.value.max_steps,
    save_every: form.value.save_every,
    eval_interval: form.value.eval_interval,
    text_loss_weight: form.value.text_loss_weight,
    mel_loss_weight: form.value.mel_loss_weight,
    duration_control: form.value.duration_control,
    duration_source: form.value.duration_source,
    duration_dropout: form.value.duration_dropout,
    mel_stop_loss_weight: form.value.mel_stop_loss_weight,
    mel_end_loss_weight: form.value.mel_end_loss_weight,
    mel_end_window: form.value.mel_end_window,
    duration_stop_bce_weight: form.value.duration_stop_bce_weight,
    duration_position_loss_weight: form.value.duration_position_loss_weight,
    duration_position_loss_window: form.value.duration_position_loss_window,
    gradient_checkpointing: form.value.gradient_checkpointing,
    precision: form.value.precision,
  };
}

function selectTrainingJob(job: LoraTrainingJob) {
  hydratingFromJob = true;
  selectedTrainingJob.value = job;
  form.value = {
    run_id: job.run_id,
    train_features: withDataPrefix(job.train_features_relpath),
    val_features: withDataPrefix(job.val_features_relpath),
    canary_features: withDataPrefix(job.canary_features_relpath),
    output_root: withDataPrefix(job.output_root_relpath),
    voice_prompt_relpath: job.voice_prompt_relpath,
    rank: job.rank,
    alpha: job.alpha,
    dropout: job.dropout,
    lr: job.learning_rate,
    max_steps: job.max_steps,
    save_every: job.save_every,
    eval_interval: job.eval_interval,
    text_loss_weight: job.text_loss_weight,
    mel_loss_weight: job.mel_loss_weight,
    duration_dropout: job.duration_dropout ?? 0.1,
    mel_stop_loss_weight: job.mel_stop_loss_weight ?? 2.0,
    mel_end_loss_weight: job.mel_end_loss_weight ?? 1.5,
    mel_end_window: job.mel_end_window ?? 64,
    duration_stop_bce_weight: job.duration_stop_bce_weight ?? 0.0,
    duration_position_loss_weight: job.duration_position_loss_weight ?? 0.2,
    duration_position_loss_window: job.duration_position_loss_window ?? 32,
    duration_control: job.duration_control,
    duration_source: job.duration_source || "target",
    gradient_checkpointing: job.gradient_checkpointing,
    precision: job.precision,
  };
  window.setTimeout(() => {
    hydratingFromJob = false;
  }, 0);
}

function objectiveGate(key: string): unknown {
  return contract.value?.objective_gates?.[key];
}

function dataRequirement(key: string): unknown {
  return contract.value?.data_requirements?.[key];
}

function trainingPolicy(key: string): unknown {
  return trainingDecisionPolicy.value?.[key];
}

function lecture08Baseline(key: string): unknown {
  return lecture08Reference.value?.[key];
}

function joinPath(...parts: string[]): string {
  return parts
    .map((part) => String(part || "").replaceAll("\\", "/").replace(/^\/+|\/+$/g, ""))
    .filter(Boolean)
    .join("/");
}

function shellCommand(args: Array<string | number | boolean | unknown>): string {
  return args.map((arg) => quoteArg(String(arg))).join(" ");
}

function quoteArg(value: string): string {
  if (/^[A-Za-z0-9_./:=,+-]+$/.test(value)) return value;
  return `"${value.replaceAll('"', '\\"')}"`;
}

async function copyCommand(command: string) {
  await navigator.clipboard.writeText(command);
  toast.success(t("Command copied"));
}

async function copyAllCommands() {
  await copyCommand([trainingCommand.value, canaryCommand.value, regressionCommand.value].join("\n\n"));
}

function shortPath(value: string | undefined): string {
  if (!value) return "-";
  const parts = value.split(/[\\/]/).filter(Boolean);
  return parts.slice(-2).join("/");
}

function display(value: unknown): string {
  if (value === undefined || value === null || value === "") return "-";
  if (typeof value === "boolean") return value ? t("yes") : t("no");
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : String(value);
  return String(value);
}

function displayToken(value: string): string {
  return t(value);
}

function displayPhaseStatus(key: string): string {
  const value = artifactValue(key);
  return value === "-" ? "pending" : value;
}

function withDataPrefix(value: string | undefined): string {
  if (!value) return "";
  return value.startsWith("data/") ? value : `data/${value}`;
}

function artifactRaw(key: string): unknown {
  return selectedTrainingJob.value?.artifacts?.[key];
}

function artifactValue(key: string): string {
  const value = artifactRaw(key);
  return value === undefined || value === null || value === "" ? "-" : String(value);
}

function hasArtifact(key: string): boolean {
  return artifactValue(key) !== "-";
}

function metricValue(key: string, digits = 2, suffix = ""): string {
  const value = artifactRaw(key);
  const numeric = Number(value);
  if (Number.isFinite(numeric)) {
    return `${numeric.toFixed(digits)}${suffix ? ` ${suffix}` : ""}`;
  }
  return value === undefined || value === null || value === "" ? "-" : String(value);
}

function artifactHref(value: string | undefined): string {
  if (!value || value === "-") return "";
  const normalized = String(value).trim().replaceAll("\\", "/").replace(/^\/+/, "").replace(/^data\//, "");
  if (!normalized || !/\.[A-Za-z0-9]+$/.test(normalized)) return "";
  const url = `/data-artifacts/${normalized.split("/").map(encodeURIComponent).join("/")}`;
  const key = getApiKey();
  return key ? `${url}?api_key=${encodeURIComponent(key)}` : url;
}

function formatTime(value: string | undefined): string {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function statusClass(status: string): string {
  switch (status) {
    case "completed":
      return "hd-chip-success";
    case "failed":
    case "cancelled":
      return "hd-chip-danger";
    case "running":
    case "queued":
      return "hd-chip-warning";
    case "planned":
      return "hd-chip-info";
    default:
      return "hd-chip-muted";
  }
}

function phaseStatusClass(status: string): string {
  switch (status) {
    case "completed":
    case "ready":
    case "approved":
      return "hd-chip-success";
    case "failed":
    case "cancelled":
      return "hd-chip-danger";
    case "running":
    case "queued":
      return "hd-chip-warning";
    case "pending":
      return "hd-chip-muted";
    default:
      return "hd-chip-info";
  }
}

watch(
  form,
  () => {
    if (!hydratingFromJob) {
      if (selectedTrainingJob.value && formMatchesTrainingJob(selectedTrainingJob.value)) {
        return;
      }
      selectedTrainingJob.value = null;
    }
  },
  { deep: true },
);

watch(selectedTrainingActive, updateTrainingPoll);

function updateTrainingPoll() {
  if (selectedTrainingActive.value) {
    if (!trainingPollTimer) {
      trainingPollTimer = window.setInterval(() => {
        void load(false);
      }, 5000);
    }
    return;
  }
  if (trainingPollTimer) {
    window.clearInterval(trainingPollTimer);
    trainingPollTimer = undefined;
  }
}

function formMatchesTrainingJob(job: LoraTrainingJob): boolean {
  return (
    form.value.run_id === job.run_id &&
    form.value.train_features === withDataPrefix(job.train_features_relpath) &&
    form.value.val_features === withDataPrefix(job.val_features_relpath) &&
    form.value.canary_features === withDataPrefix(job.canary_features_relpath) &&
    form.value.output_root === withDataPrefix(job.output_root_relpath) &&
    form.value.voice_prompt_relpath === job.voice_prompt_relpath &&
    form.value.rank === job.rank &&
    form.value.alpha === job.alpha &&
    form.value.dropout === job.dropout &&
    form.value.lr === job.learning_rate &&
    form.value.max_steps === job.max_steps &&
    form.value.save_every === job.save_every &&
    form.value.eval_interval === job.eval_interval &&
    form.value.text_loss_weight === job.text_loss_weight &&
    form.value.mel_loss_weight === job.mel_loss_weight &&
    form.value.duration_dropout === (job.duration_dropout ?? 0.1) &&
    form.value.mel_stop_loss_weight === (job.mel_stop_loss_weight ?? 2.0) &&
    form.value.mel_end_loss_weight === (job.mel_end_loss_weight ?? 1.5) &&
    form.value.mel_end_window === (job.mel_end_window ?? 64) &&
    form.value.duration_stop_bce_weight === (job.duration_stop_bce_weight ?? 0.0) &&
    form.value.duration_position_loss_weight === (job.duration_position_loss_weight ?? 0.2) &&
    form.value.duration_position_loss_window === (job.duration_position_loss_window ?? 32) &&
    form.value.duration_control === job.duration_control &&
    form.value.duration_source === (job.duration_source || "target") &&
    form.value.gradient_checkpointing === job.gradient_checkpointing &&
    form.value.precision === job.precision
  );
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
              "truncate text-lg font-semibold",
              props.tone === "success"
                ? "text-emerald-200"
                : props.tone === "warning"
                  ? "text-amber-200"
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

const ArtifactRow = defineComponent({
  props: {
    label: { type: String, required: true },
    value: { type: String, required: true },
    href: { type: String, default: "" },
  },
  setup(props) {
    return () =>
      h("div", { class: "flex items-center justify-between gap-2" }, [
        h("dt", { class: "text-[color:var(--hd-text-dim)]" }, props.label),
        props.href
          ? h(
              "a",
              {
                class:
                  "flex min-w-0 items-center gap-1 truncate font-mono text-cyan-100 hover:text-cyan-50",
                href: props.href,
                target: "_blank",
                rel: "noreferrer",
              },
              [
                h("span", { class: "min-w-0 truncate" }, props.value),
                h(ExternalLink, { size: 11, class: "shrink-0" }),
              ],
            )
          : h("dd", { class: "min-w-0 truncate font-mono text-[color:var(--hd-text)]" }, props.value),
      ]);
  },
});

const NumberField = defineComponent({
  props: {
    modelValue: { type: Number, required: true },
    label: { type: String, required: true },
    step: { type: String, default: "1" },
  },
  emits: ["update:modelValue"],
  setup(props, { emit }) {
    return () =>
      h("label", { class: "block" }, [
        h("span", { class: "mb-1.5 block text-xs text-[color:var(--hd-text-muted)]" }, props.label),
        h("input", {
          class: "hd-input font-mono text-xs",
          type: "number",
          step: props.step,
          value: props.modelValue,
          onInput: (event: Event) =>
            emit("update:modelValue", Number((event.target as HTMLInputElement).value)),
        }),
      ]);
  },
});

const CommandBlock = defineComponent({
  props: {
    title: { type: String, required: true },
    description: { type: String, required: true },
    command: { type: String, required: true },
  },
  emits: ["copy"],
  setup(props, { emit }) {
    return () =>
      h("div", { class: "rounded-[8px] border border-[color:var(--hd-border-soft)] bg-black/20 p-3" }, [
        h("div", { class: "flex items-start justify-between gap-3" }, [
          h("div", [
            h("div", { class: "text-sm font-semibold text-white" }, props.title),
            h("div", { class: "mt-1 text-[11px] leading-5 text-[color:var(--hd-text-muted)]" }, props.description),
          ]),
          h(
            "button",
            {
              class: "hd-btn hd-btn-secondary shrink-0 py-1.5 text-xs",
              type: "button",
              onClick: () => emit("copy"),
            },
            t("Copy"),
          ),
        ]),
        h("pre", { class: "mt-3 overflow-auto rounded-[8px] bg-black/35 p-3 text-[11px] leading-5 text-cyan-50" }, [
          h("code", props.command),
        ]),
      ]);
  },
});

onMounted(() => {
  void load();
});
onBeforeUnmount(() => {
  if (trainingPollTimer) window.clearInterval(trainingPollTimer);
});
</script>
