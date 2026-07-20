<template>
  <div class="hd-page flex h-full min-h-[560px] items-center justify-center">
    <div class="max-w-xl text-center">
      <div
        class="mx-auto flex h-14 w-14 items-center justify-center rounded-[8px] border border-cyan-300/25 bg-cyan-300/10 text-cyan-200"
      >
        <Clapperboard :size="26" />
      </div>
      <h2 class="mt-5 text-xl font-semibold text-white">{{ t("选择一个任务开始观测") }}</h2>
      <p class="mt-2 text-sm leading-6 text-[color:var(--hd-text-muted)]">
        {{
          t(
            "左侧列表会显示最近的 Jobs 和 Episodes。打开任务后可以查看阶段进度、分段漂移、AI 评分、音色配置和输出 artifacts。",
          )
        }}
      </p>
      <div class="mt-6 grid grid-cols-1 gap-3 text-left sm:grid-cols-3">
        <div class="hd-panel-subtle p-3">
          <div class="hd-label">Pipeline</div>
          <div class="mt-2 text-xs text-[color:var(--hd-text-muted)]">
            {{ t("ASR、翻译、TTS、Merge 阶段状态") }}
          </div>
        </div>
        <div class="hd-panel-subtle p-3">
          <div class="hd-label">Quality</div>
          <div class="mt-2 text-xs text-[color:var(--hd-text-muted)]">
            {{ t("漂移率、AI 评分和弱段定位") }}
          </div>
        </div>
        <div class="hd-panel-subtle p-3">
          <div class="hd-label">Output</div>
          <div class="mt-2 text-xs text-[color:var(--hd-text-muted)]">
            {{ t("最终视频、字幕和中间产物") }}
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { Clapperboard } from "@lucide/vue";
import { useI18n } from "../i18n";

const { t } = useI18n();
</script>
