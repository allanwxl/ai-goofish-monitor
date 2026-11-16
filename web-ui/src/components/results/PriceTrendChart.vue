<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'

interface TrendPoint {
  day: string
  avg_price: number | null
  median_price: number | null
}

interface SoldTrendPoint {
  day: string
  sold_count: number
  avg_price: number | null
  median_price: number | null
  estimated_count?: number
}

interface SeriesPoint {
  day: string
  avg_price: number | null
  median_price: number | null
  sold_price: number | null
  sold_count: number
  estimated_count: number
}

const props = defineProps<{
  points: TrendPoint[]
  soldPoints?: SoldTrendPoint[]
}>()
const { t } = useI18n()

const chartWidth = 720
const chartHeight = 220
const padding = 24

const svgRef = ref<SVGSVGElement | null>(null)
const chartRef = ref<HTMLElement | null>(null)
const hoveredIndex = ref<number | null>(null)
const tooltip = ref<{
  x: number
  y: number
  point: SeriesPoint
  align: 'left' | 'center' | 'right'
  placeBelow: boolean
} | null>(null)

/**
 * 均价、中位价与成交价共用一条按日期排列的横轴。
 *
 * 成交价只覆盖「判定为已售」的那几天，如果两条线各自按自己的天数排布，
 * 同一天会落在不同的 x 坐标上，叠加就失去意义，所以这里先合并日期再定位。
 */
const series = computed<SeriesPoint[]>(() => {
  const avgByDay = new Map<string, TrendPoint>()
  props.points.forEach((point) => {
    if (point.day) avgByDay.set(point.day, point)
  })

  const soldByDay = new Map<string, SoldTrendPoint>()
  ;(props.soldPoints || []).forEach((point) => {
    if (point.day) soldByDay.set(point.day, point)
  })

  const days = Array.from(new Set([...avgByDay.keys(), ...soldByDay.keys()])).sort()

  return days.map((day) => {
    const avgPoint = avgByDay.get(day)
    const soldPoint = soldByDay.get(day)
    return {
      day,
      avg_price: avgPoint?.avg_price ?? null,
      median_price: avgPoint?.median_price ?? null,
      sold_price: soldPoint?.avg_price ?? null,
      sold_count: soldPoint?.sold_count ?? 0,
      estimated_count: soldPoint?.estimated_count ?? 0,
    }
  })
})

const valueRange = computed(() => {
  const values = series.value
    .flatMap((point) => [point.avg_price, point.median_price, point.sold_price])
    .filter((value): value is number => typeof value === 'number')
  if (values.length === 0) {
    return { min: 0, max: 1 }
  }
  const min = Math.min(...values)
  const max = Math.max(...values)
  if (min === max) {
    return { min: min - 1, max: max + 1 }
  }
  return { min, max }
})

function resolveX(index: number) {
  if (series.value.length <= 1) return chartWidth / 2
  const usableWidth = chartWidth - padding * 2
  return padding + (usableWidth / (series.value.length - 1)) * index
}

function resolveY(value: number) {
  const usableHeight = chartHeight - padding * 2
  const ratio = (value - valueRange.value.min) / (valueRange.value.max - valueRange.value.min)
  return chartHeight - padding - ratio * usableHeight
}

function buildPath(values: Array<number | null>) {
  const commands = values
    .map((value, index) => {
      if (value === null || value === undefined) return null
      const prefix = index === 0 ? 'M' : 'L'
      return `${prefix} ${resolveX(index)} ${resolveY(value)}`
    })
    .filter(Boolean)
  return commands.join(' ')
}

const avgPath = computed(() => buildPath(series.value.map((point) => point.avg_price)))
const medianPath = computed(() => buildPath(series.value.map((point) => point.median_price)))
const soldPath = computed(() => buildPath(series.value.map((point) => point.sold_price)))
const areaPath = computed(() => {
  if (!avgPath.value || series.value.length === 0) return ''
  const firstX = resolveX(0)
  const lastX = resolveX(series.value.length - 1)
  const baseline = chartHeight - padding
  return `${avgPath.value} L ${lastX} ${baseline} L ${firstX} ${baseline} Z`
})

const hoveredPoint = computed(() => {
  if (hoveredIndex.value === null) return null
  return series.value[hoveredIndex.value] || null
})

const hoveredAvgY = computed(() => {
  const point = hoveredPoint.value
  if (!point || point.avg_price === null || point.avg_price === undefined) return 0
  return resolveY(point.avg_price)
})

const hoveredMedianY = computed(() => {
  const point = hoveredPoint.value
  if (!point || point.median_price === null || point.median_price === undefined) return 0
  return resolveY(point.median_price)
})

const hoveredSoldY = computed(() => {
  const point = hoveredPoint.value
  if (!point || point.sold_price === null || point.sold_price === undefined) return 0
  return resolveY(point.sold_price)
})

/**
 * 把鼠标位置换算成 SVG 用户坐标。
 *
 * SVG 用 viewBox 缩放，容器宽度又不固定（还可能留白），直接用 clientX 换算会
 * 偏移，所以走 getScreenCTM 逆矩阵，它同时处理缩放与留白。
 */
function handlePointerMove(event: PointerEvent) {
  const svg = svgRef.value
  const container = chartRef.value
  if (!svg || !container || series.value.length === 0) return

  const matrix = svg.getScreenCTM()
  if (!matrix) return

  const cursor = new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix.inverse())

  let nearest = 0
  let bestDistance = Number.POSITIVE_INFINITY
  series.value.forEach((_point, index) => {
    const distance = Math.abs(resolveX(index) - cursor.x)
    if (distance < bestDistance) {
      bestDistance = distance
      nearest = index
    }
  })

  const point = series.value[nearest]
  if (!point) return

  hoveredIndex.value = nearest

  const anchorDay = new DOMPoint(
    resolveX(nearest),
    resolveY(point.avg_price === null ? valueRange.value.min : point.avg_price),
  ).matrixTransform(matrix)
  const containerRect = container.getBoundingClientRect()

  const localX = anchorDay.x - containerRect.left
  const localY = anchorDay.y - containerRect.top

  // 靠近左右边缘时把提示框改成贴边显示，避免被卡片裁掉
  const edge = 96
  const align: 'left' | 'center' | 'right' =
    localX < edge ? 'left' : localX > containerRect.width - edge ? 'right' : 'center'

  tooltip.value = {
    x: localX,
    y: localY,
    point,
    align,
    // 贴近顶部时改为显示在数据点下方
    placeBelow: localY < 80,
  }
}

function handlePointerLeave() {
  hoveredIndex.value = null
  tooltip.value = null
}
</script>

<template>
  <div class="app-surface-subtle p-4">
    <div class="mb-3 flex flex-col gap-3 text-xs uppercase tracking-[0.22em] text-slate-500 sm:flex-row sm:items-center sm:justify-between">
      <span>Daily Price Curve</span>
      <div class="flex items-center gap-3">
        <span class="inline-flex items-center gap-1">
          <span class="h-2.5 w-2.5 rounded-full bg-sky-600" />
          {{ t('results.chart.avgPrice') }}
        </span>
        <span class="inline-flex items-center gap-1">
          <span class="h-2.5 w-2.5 rounded-full bg-amber-500" />
          {{ t('results.chart.medianPrice') }}
        </span>
        <span class="inline-flex items-center gap-1">
          <span class="h-2.5 w-2.5 rounded-full bg-rose-600" />
          {{ t('results.chart.soldPrice') }}
        </span>
      </div>
    </div>

    <div v-if="series.length === 0" class="rounded-2xl border border-dashed border-slate-200 bg-white/70 px-4 py-10 text-center text-sm text-slate-500">
      {{ t('results.chart.noTrend') }}
    </div>

    <div v-else ref="chartRef" class="relative">
      <svg
        ref="svgRef"
        :viewBox="`0 0 ${chartWidth} ${chartHeight}`"
        class="h-[220px] w-full cursor-crosshair"
        role="img"
        :aria-label="t('results.chart.noTrend')"
        @pointermove="handlePointerMove"
        @pointerdown="handlePointerMove"
        @pointerleave="handlePointerLeave"
      >
        <defs>
          <linearGradient id="avg-area-fill" x1="0%" y1="0%" x2="0%" y2="100%">
            <stop offset="0%" stop-color="#0284c7" stop-opacity="0.24" />
            <stop offset="100%" stop-color="#0284c7" stop-opacity="0" />
          </linearGradient>
        </defs>

        <g>
          <line
            v-for="index in 4"
            :key="index"
            :x1="padding"
            :x2="chartWidth - padding"
            :y1="padding + ((chartHeight - padding * 2) / 4) * (index - 1)"
            :y2="padding + ((chartHeight - padding * 2) / 4) * (index - 1)"
            stroke="#cbd5e1"
            stroke-dasharray="4 6"
          />
        </g>

        <path :d="areaPath" fill="url(#avg-area-fill)" />
        <path :d="avgPath" fill="none" stroke="#0284c7" stroke-width="4" stroke-linecap="round" />
        <path :d="medianPath" fill="none" stroke="#f59e0b" stroke-width="3" stroke-dasharray="8 6" stroke-linecap="round" />
        <path :d="soldPath" fill="none" stroke="#e11d48" stroke-width="3" stroke-linecap="round" />

        <g v-for="(point, index) in series" :key="point.day">
          <circle
            v-if="point.avg_price !== null && point.avg_price !== undefined"
            :cx="resolveX(index)"
            :cy="resolveY(point.avg_price)"
            r="5"
            fill="#0284c7"
          />
          <circle
            v-if="point.median_price !== null && point.median_price !== undefined"
            :cx="resolveX(index)"
            :cy="resolveY(point.median_price)"
            r="4"
            fill="#f59e0b"
          />
          <circle
            v-if="point.sold_price !== null && point.sold_price !== undefined"
            :cx="resolveX(index)"
            :cy="resolveY(point.sold_price)"
            r="5.5"
            fill="#e11d48"
            stroke="#ffffff"
            stroke-width="1.5"
          />
          <text
            :x="resolveX(index)"
            :y="chartHeight - 6"
            text-anchor="middle"
            fill="#64748b"
            font-size="12"
          >
            {{ point.day.slice(5) }}
          </text>
        </g>

        <g v-if="hoveredIndex !== null && hoveredPoint">
          <line
            :x1="resolveX(hoveredIndex)"
            :x2="resolveX(hoveredIndex)"
            :y1="padding"
            :y2="chartHeight - padding"
            stroke="#94a3b8"
            stroke-width="1.5"
            stroke-dasharray="3 4"
          />
          <circle
            v-if="hoveredPoint.avg_price !== null && hoveredPoint.avg_price !== undefined"
            :cx="resolveX(hoveredIndex)"
            :cy="hoveredAvgY"
            r="7"
            fill="#0284c7"
            stroke="#ffffff"
            stroke-width="2.5"
          />
          <circle
            v-if="hoveredPoint.median_price !== null && hoveredPoint.median_price !== undefined"
            :cx="resolveX(hoveredIndex)"
            :cy="hoveredMedianY"
            r="6"
            fill="#f59e0b"
            stroke="#ffffff"
            stroke-width="2.5"
          />
          <circle
            v-if="hoveredPoint.sold_price !== null && hoveredPoint.sold_price !== undefined"
            :cx="resolveX(hoveredIndex)"
            :cy="hoveredSoldY"
            r="7"
            fill="#e11d48"
            stroke="#ffffff"
            stroke-width="2.5"
          />
        </g>
      </svg>

      <div
        v-if="tooltip"
        class="pointer-events-none absolute z-10 w-max max-w-[220px] rounded-xl border border-slate-200 bg-white/95 px-3 py-2 text-xs shadow-lg backdrop-blur"
        :style="{
          left: `${tooltip.x}px`,
          top: `${tooltip.y}px`,
          transform: `translate(${
            tooltip.align === 'left' ? '0' : tooltip.align === 'right' ? '-100%' : '-50%'
          }, ${tooltip.placeBelow ? '18px' : 'calc(-100% - 14px)'})`,
        }"
        role="tooltip"
      >
        <p class="font-semibold text-slate-800">{{ tooltip.point.day }}</p>
        <div class="mt-1 flex items-center gap-1.5 text-slate-600">
          <span class="h-2 w-2 shrink-0 rounded-full bg-sky-600" />
          <span>{{ t('results.chart.avgPrice') }}</span>
          <span class="ml-auto font-semibold text-slate-900">
            {{ tooltip.point.avg_price === null ? '—' : `¥${tooltip.point.avg_price}` }}
          </span>
        </div>
        <div class="mt-0.5 flex items-center gap-1.5 text-slate-600">
          <span class="h-2 w-2 shrink-0 rounded-full bg-amber-500" />
          <span>{{ t('results.chart.medianPrice') }}</span>
          <span class="ml-auto font-semibold text-slate-900">
            {{
              tooltip.point.median_price === null || tooltip.point.median_price === undefined
                ? '—'
                : `¥${tooltip.point.median_price}`
            }}
          </span>
        </div>
        <div
          v-if="tooltip.point.sold_count > 0"
          class="mt-0.5 flex items-center gap-1.5 text-slate-600"
        >
          <span class="h-2 w-2 shrink-0 rounded-full bg-rose-600" />
          <span>{{ t('results.chart.soldPrice') }}</span>
          <span class="ml-auto font-semibold text-slate-900">
            {{
              tooltip.point.sold_price === null || tooltip.point.sold_price === undefined
                ? '—'
                : `¥${tooltip.point.sold_price}`
            }}
          </span>
        </div>
        <div
          v-if="tooltip.point.sold_count > 0"
          class="mt-0.5 flex items-center justify-between gap-2 text-[10px] text-slate-400"
        >
          <span>{{ t('results.chart.soldCount', { count: tooltip.point.sold_count }) }}</span>
          <span v-if="tooltip.point.estimated_count > 0">
            {{ t('results.chart.soldEstimated', { count: tooltip.point.estimated_count }) }}
          </span>
        </div>
      </div>
    </div>
  </div>
</template>
